"""Legacy calendar-month calculations reused by the scheduled personal collector.

Personal API routes use the durable store; the process-local compatibility methods
below are not used by the request path.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import arrow

from ideadatamodel import (
    constants,
    exceptions,
    locale,
    GetMyCostsResult,
    GetCostTickerResult,
    GetMyCostsSummaryResult,
    MyCostsAmount,
    MyCostsMonth,
    MyCostsDisk,
    MyCostsStorageShare,
    MyCostsJobs,
    MyCostsJobGroup,
    SocaAmount,
    ListUsersRequest,
)
from ideasdk.aws.ec2_price_list import get_ec2_price_list
from ideaclustermanager.app.costs.my_costs_service import MyCostsService, SESSION_FIELDS
from ideaclustermanager.app.filesystem.storage_usage import StorageUsageService
from ideaclustermanager.app.costs.storage_rates import daily_storage_rate
from ideaclustermanager.app.metrics.storage_metrics_service import normalize_user

CACHE_SECONDS = 3600
PERIODS = frozenset({'wtd', 'mtd', 'qtd', 'ytd'})
JOBS_NOTE = 'Compute estimates for jobs completed this month; disks and scratch storage are excluded.'
DESKTOP_NOTE = 'Recorded session time × the instance on-demand rate. Stop and restart history may be incomplete.'
DISKS_NOTE = (
    'Disk size × monthly price × the fraction of this calendar month the disk has existed. '
    'Stopped desktops keep paying for disks. Current inventory; deleted disks, snapshots, '
    'extra IOPS and throughput are excluded.'
)
STORAGE_NOTE = (
    "Each file system's daily rate estimate × your share of measured bytes. "
    'ONTAP includes provisioned SSD, throughput, IOPS above 3 per GB and capacity-pool '
    'bytes; EFS includes storage classes, excluding throughput and requests. '
    'With a default user quota rule, users without files count as zero. '
    'Complete home-folder measurements are used when volume usage is absent. '
    'The latest usage share is also used for last month.'
)
AI_NOTE = (
    "Your share of each project's tokens × its AI spend in the same calendar month."
)


def period_bounds(period, now, timezone):
    """Return a calendar-period start and as-of instant in the cluster timezone."""
    local_now = arrow.get(now).to(timezone)
    if period == 'wtd':
        start = local_now.floor('week')  # Arrow weeks begin on Monday.
    elif period == 'qtd':
        first_month = ((local_now.month - 1) // 3) * 3 + 1
        start = local_now.replace(
            month=first_month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    elif period == 'ytd':
        start = local_now.floor('year')
    else:
        start = local_now.floor('month')
    return start, local_now


class MonthlyCostsService(MyCostsService):
    def __init__(self, context):
        super().__init__(context)
        self._lock = threading.Lock()
        self._cache = {}
        self._ticker_cache = {}
        self._pending = set()
        self._workers = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix='personal-costs'
        )
        # Shared reads (including failures) are cached across all users of this process.
        self._reads = {}
        self._read_lock = threading.Lock()

    def get_costs(self, username):
        if not username:
            raise exceptions.unauthorized_access()
        timezone = self.context.config().get_string('cluster.timezone', 'UTC') or 'UTC'
        month = arrow.utcnow().to(timezone).format('YYYY-MM')
        key = (username, month)
        with self._lock:
            cached = self._cache.get(key)
            if key not in self._pending and (
                cached is None or time.monotonic() - cached[0] >= CACHE_SECONDS
            ):
                self._pending.add(key)
                self._workers.submit(self._refresh, key)
            if cached:
                result = cached[1].model_copy(deep=True)
                if key in self._pending:
                    result.state = 'refreshing'
                return result
        return GetMyCostsResult(currency=locale.get_currency_code(), state='computing')

    def get_ticker(self, username):
        config = self.context.config()
        if not config.get_bool('cluster-manager.web_portal.cost_ticker.enabled', False):
            return GetCostTickerResult(enabled=False)
        if not username:
            raise exceptions.unauthorized_access()
        period = config.get_string(
            'cluster-manager.web_portal.cost_ticker.period', 'mtd'
        )
        if period not in PERIODS:
            period = 'mtd'

        # The billboard and MTD ticker are two projections of the same cached result.
        if period == 'mtd':
            costs = self.get_costs(username)
            if costs.current is None:
                return GetCostTickerResult(enabled=True, period='MTD')
            return GetCostTickerResult(
                enabled=True,
                period='MTD',
                total=costs.current.total,
                currency=costs.currency,
                as_of=costs.refreshed_at,
            )

        timezone = config.get_string('cluster.timezone', 'UTC') or 'UTC'
        start, now = period_bounds(period, arrow.utcnow(), timezone)
        key = (username, period, start.isoformat())
        with self._lock:
            cached = self._ticker_cache.get(key)
            if key not in self._pending and (
                cached is None or time.monotonic() - cached[0] >= CACHE_SECONDS
            ):
                self._pending.add(key)
                self._workers.submit(self._refresh_ticker, key, start, now)
            if cached:
                return cached[1].model_copy(deep=True)
        return GetCostTickerResult(enabled=True, period=period.upper())

    def _refresh_ticker(self, key, start, end):
        try:
            period = self._month(key[0], start, end)
            result = GetCostTickerResult(
                enabled=True,
                period=key[1].upper(),
                total=period.total,
                currency=locale.get_currency_code(),
                as_of=end.isoformat(),
            )
        except Exception:
            self.logger.exception('Personal cost ticker refresh failed')
            result = None
        with self._lock:
            if result is not None:
                self._ticker_cache[key] = (time.monotonic(), result)
            self._pending.discard(key)

    def _refresh(self, key):
        try:
            timezone = (
                self.context.config().get_string('cluster.timezone', 'UTC') or 'UTC'
            )
            now = arrow.utcnow().to(timezone)
            start = now.floor('month')
            result = GetMyCostsResult(
                currency=locale.get_currency_code(),
                state='ready',
                refreshed_at=now.isoformat(),
                current=self._month(key[0], start, now),
                previous=self._month(key[0], start.shift(months=-1), start),
            )
        except Exception:
            self.logger.exception('Personal cost refresh failed')
            result = GetMyCostsResult(
                currency=locale.get_currency_code(), state='error'
            )
        with self._lock:
            old = self._cache.get(key)
            if result.state == 'error' and old and old[1].current:
                result = old[1].model_copy(deep=True)
                result.state = 'stale'
            self._cache[key] = (time.monotonic(), result)
            self._pending.discard(key)
            # Bound old calendar windows without evicting active refreshes.
            self._cache = {
                k: v
                for k, v in self._cache.items()
                if k[1] == key[1] or k in self._pending
            }

    def _remember(self, key, read):
        # Serialize shared misses so concurrent users do not pay for duplicate CE calls.
        with self._read_lock:
            cached = self._reads.get(key)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1]
            try:
                value = read()
            except Exception:
                self.logger.exception('Personal cost source unavailable')
                value = None
            self._reads[key] = (time.monotonic(), value)
            return value

    def _convert(self, amount, unit='USD'):
        currency = locale.get_currency_code()
        if currency == unit:
            return float(amount)
        if unit == 'USD':
            rate = self.context.config().get_float(
                'cluster.costs.usd_exchange_rate', None
            )
            if rate is not None and rate > 0:
                return float(amount) * rate
        raise ValueError('No conversion to the cluster currency is configured')

    def _billing(self, start, end, filters, groups):
        # CE End is exclusive. Include today's provisional billing, but never tomorrow's usage.
        end_date = (
            end.ceil('day').shift(microseconds=1).format('YYYY-MM-DD')
            if end != end.floor('day')
            else end.format('YYYY-MM-DD')
        )
        request = dict(
            TimePeriod={'Start': start.format('YYYY-MM-DD'), 'End': end_date},
            Granularity='MONTHLY',
            Metrics=['UnblendedCost'],
            Filter={
                'And': [
                    {
                        'Tags': {
                            'Key': constants.IDEA_TAG_CLUSTER_NAME,
                            'Values': [self.context.cluster_name()],
                        }
                    },
                    *filters,
                ]
            },
            GroupBy=groups,
        )

        def read():
            if self.context.aws().aws_partition() != 'aws':
                return None
            values = {}
            while True:
                response = (
                    self.context.aws().cost_explorer().get_cost_and_usage(**request)
                )
                for period in response.get('ResultsByTime', []):
                    for group in period.get('Groups', []):
                        keys = tuple(key.split('$', 1)[-1] for key in group['Keys'])
                        metric = group['Metrics']['UnblendedCost']
                        values[keys] = values.get(keys, 0) + self._convert(
                            metric['Amount'], metric['Unit']
                        )
                token = response.get('NextPageToken')
                if not token:
                    return values
                request['NextPageToken'] = token

        return self._remember(('billing', json.dumps(request, sort_keys=True)), read)

    def _ai_project(self, username, project, start_date, end_date):
        # Reuse token apportionment, but bind spend to these exact dates, not 30 days.
        reader = MyCostsService(self.context)

        def spend(project_name):
            values = self._billing(
                arrow.get(start_date),
                arrow.get(end_date).shift(days=1),
                [
                    {
                        'Tags': {
                            'Key': constants.IDEA_TAG_PROJECT,
                            'Values': [project_name],
                        }
                    }
                ],
                [{'Type': 'DIMENSION', 'Key': 'SERVICE'}],
            )
            if values is None:
                return None
            return SocaAmount(
                amount=sum(
                    value
                    for (service,), value in values.items()
                    if 'bedrock' in service.lower()
                )
            )

        reader._project_spend = spend
        return reader._ai_project(username, project, start_date, end_date)

    def _month(self, username, start, end):
        last_day = (
            end.shift(microseconds=-1).format('YYYY-MM-DD')
            if end == end.floor('month')
            else end.format('YYYY-MM-DD')
        )
        jobs = self._compute_jobs(username, start, end)
        desktops = self._desktops(username, start, end)
        # Instance price helpers return USD; never relabel it as another currency.
        try:
            desktops.cost = self._convert(desktops.cost or 0)
            for session in desktops.sessions or []:
                if session.cost is not None:
                    session.cost = self._convert(session.cost)
        except ValueError:
            desktops.is_unavailable = True
        ai = self._ai(username, start.format('YYYY-MM-DD'), last_day)
        disks, disk_unavailable = self._disks(username, start, end)
        storage, storage_unavailable = self._storage(username, start, end)

        def line(cost, missing, note):
            return MyCostsAmount(
                cost=None if missing else round(cost or 0, 4),
                status='unavailable' if missing else 'ready',
                note=note,
            )

        result = MyCostsMonth(
            start_date=start.format('YYYY-MM-DD'),
            end_date=last_day,
            jobs=line(
                jobs.cost,
                jobs.is_unavailable or jobs.cost_unavailable or jobs.unpriced_jobs,
                JOBS_NOTE,
            ),
            desktops=line(
                desktops.cost,
                desktops.is_unavailable or desktops.unpriced_sessions,
                DESKTOP_NOTE,
            ),
            ai=line(
                ai.cost,
                ai.is_unavailable or any(p.cost_unavailable for p in ai.projects or []),
                AI_NOTE,
            ),
            desktop_disks=line(
                sum(d.cost or 0 for d in disks), disk_unavailable, DISKS_NOTE
            ),
            shared_storage=line(
                sum(s.cost or 0 for s in storage), storage_unavailable, STORAGE_NOTE
            ),
            disks=disks,
            storage=storage,
            details=GetMyCostsSummaryResult(
                username=username,
                window='calendar_month',
                start_date=start.format('YYYY-MM-DD'),
                end_date=last_day,
                currency=locale.get_currency_code(),
                jobs=jobs,
                desktops=desktops,
                ai=ai,
            ),
        )
        if any(s.status == 'no_usage_data' for s in storage):
            result.shared_storage.status = 'no_usage_data'
        lines = [
            result.jobs,
            result.desktops,
            result.ai,
            result.desktop_disks,
            result.shared_storage,
        ]
        result.incomplete = any(item.cost is None for item in lines)
        # An incomplete total remains a labelled subtotal, never a misleading zero.
        result.total = (
            round(sum(item.cost for item in lines if item.cost is not None), 4)
            if any(item.cost is not None for item in lines)
            else None
        )
        return result

    def _desktop_hits(self, username, start_ms, end_ms, **kwargs):
        if not username:
            raise exceptions.unauthorized_access()
        index = self.context.config().get_string(
            'virtual-desktop-controller.opensearch.dcv_session.alias'
        )
        if not index:
            return None
        # A long-running or stopped session need not have been updated this month.
        body = {
            'size': 500,
            '_source': SESSION_FIELDS,
            'sort': [{'idea_session_id.raw': 'asc'}],
            'query': {
                'bool': {
                    'filter': [
                        {'term': {'owner.raw': username}},
                        {'range': {'created_on': {'lte': end_ms}}},
                    ]
                }
            },
        }
        hits = []
        while True:
            response = self._search(index, body)
            if response is None:
                return None
            page = response.get('hits', {}).get('hits', [])
            hits.extend(page)
            if len(page) < body['size']:
                return hits
            body['search_after'] = page[-1]['sort']

    def _compute_jobs(self, username, start, end):
        try:
            index = self._jobs_index()
            if not index:
                return MyCostsJobs(is_unavailable=True)
            body = {
                'size': 500,
                'sort': [{'end_time': 'desc'}, {'job_id': 'desc'}],
                'query': {
                    'bool': {
                        'filter': [
                            {'term': {'owner.raw': username}},
                            {
                                'range': {
                                    'end_time': {
                                        'gte': start.isoformat(),
                                        'lt': end.isoformat(),
                                    }
                                }
                            },
                        ]
                    }
                },
                '_source': [
                    'job_id',
                    'name',
                    'queue',
                    'project',
                    'end_time',
                    'estimated_bom_cost',
                ],
            }
            rows = []
            groups = {'project': {}, 'queue': {}}
            cost, count, missing = 0.0, 0, 0
            while True:
                response = self._search(index, body)
                if response is None:
                    return MyCostsJobs(is_unavailable=True)
                hits = response.get('hits', {}).get('hits', [])
                for hit in hits:
                    source = hit['_source']
                    bom = source.get('estimated_bom_cost') or {}
                    items = bom.get('line_items')
                    unavailable = items is None or bom.get('price_unavailable', False)
                    compute = [
                        item for item in items or [] if item.get('service') == 'aws.ec2'
                    ]
                    amount = 0.0
                    for item in compute:
                        price = item.get('total_price') or {}
                        if price.get('amount') is None:
                            unavailable = True
                        else:
                            amount += self._convert(
                                price['amount'], price.get('unit') or 'USD'
                            )
                    row = self._job(source)
                    row.cost, row.cost_unavailable = round(amount, 4), unavailable
                    if len(rows) < 20:
                        rows.append(row)
                    count += 1
                    missing += int(unavailable)
                    cost += amount
                    for key in groups:
                        name = source.get(key) or 'Unassigned'
                        group = groups[key].setdefault(
                            name, MyCostsJobGroup(name=name, job_count=0, cost=0)
                        )
                        group.job_count += 1
                        group.cost += amount
                if len(hits) < body['size']:
                    break
                body['search_after'] = hits[-1]['sort']
            return MyCostsJobs(
                job_count=count,
                cost=round(cost, 4),
                unpriced_jobs=missing,
                estimated=True,
                recent_jobs=rows,
                by_project=list(groups['project'].values()),
                by_queue=list(groups['queue'].values()),
            )
        except Exception:
            self.logger.exception('Job compute costs unavailable')
            return MyCostsJobs(is_unavailable=True)

    def _inventory(self, username):
        ec2 = self.context.aws().ec2()
        filters = [
            {
                'Name': f'tag:{constants.IDEA_TAG_CLUSTER_NAME}',
                'Values': [self.context.cluster_name()],
            },
            {'Name': f'tag:{constants.IDEA_TAG_JOB_OWNER}', 'Values': [username]},
            {
                'Name': f'tag:{constants.IDEA_TAG_NODE_TYPE}',
                'Values': [constants.NODE_TYPE_DCV_HOST],
            },
        ]
        instances = {}
        for page in ec2.get_paginator('describe_instances').paginate(Filters=filters):
            for reservation in page.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    instances[instance['InstanceId']] = instance
        volumes = {}
        ids = list(instances)
        for offset in range(0, len(ids), 100):
            for page in ec2.get_paginator('describe_volumes').paginate(
                Filters=[
                    {
                        'Name': 'attachment.instance-id',
                        'Values': ids[offset : offset + 100],
                    }
                ]
            ):
                for volume in page.get('Volumes', []):
                    volumes[volume['VolumeId']] = volume
        # Retained, detached desktop volumes carry the same provisioning tags.
        for page in ec2.get_paginator('describe_volumes').paginate(Filters=filters):
            for volume in page.get('Volumes', []):
                volumes[volume['VolumeId']] = volume
        return instances, list(volumes.values())

    def _volume_price(self, volume_type):
        def read():
            region = self.context.config().get_string('cluster.aws.region')
            if self.context.aws().aws_partition() == 'aws':
                try:
                    token = None
                    while True:
                        request = dict(
                            ServiceCode='AmazonEC2',
                            Filters=[
                                {
                                    'Type': 'TERM_MATCH',
                                    'Field': 'regionCode',
                                    'Value': region,
                                },
                                {
                                    'Type': 'TERM_MATCH',
                                    'Field': 'volumeApiName',
                                    'Value': volume_type,
                                },
                                {
                                    'Type': 'TERM_MATCH',
                                    'Field': 'productFamily',
                                    'Value': 'Storage',
                                },
                            ],
                        )
                        if token:
                            request['NextToken'] = token
                        response = self.context.aws().pricing().get_products(**request)
                        for raw in response.get('PriceList', []):
                            product = json.loads(raw) if isinstance(raw, str) else raw
                            for term in (
                                product.get('terms', {}).get('OnDemand', {}).values()
                            ):
                                for dimension in term.get(
                                    'priceDimensions', {}
                                ).values():
                                    if dimension.get('unit') == 'GB-Mo':
                                        return self._convert(
                                            dimension['pricePerUnit']['USD']
                                        )
                        token = response.get('NextToken')
                        if not token:
                            break
                except Exception:
                    self.logger.warning(
                        'Disk price lookup unavailable; trying the public price list'
                    )
            price = get_ec2_price_list(region, self.logger).get_volume_price(
                volume_type
            )
            return self._convert(price) if price is not None else None

        return self._remember(('volume-price', volume_type), read)

    def _disks(self, username, start, end):
        inventory = self._remember(
            ('inventory', username), lambda: self._inventory(username)
        )
        if inventory is None:
            return [], True
        instances, volumes = inventory
        rows = []
        for volume in volumes:
            created = arrow.get(volume['CreateTime'])
            disk_start = max(start, created)
            seconds = max(0, (end - disk_start).total_seconds())
            if seconds == 0:
                continue
            instance = next(
                (
                    instances[a['InstanceId']]
                    for a in volume.get('Attachments', [])
                    if a.get('InstanceId') in instances
                ),
                {},
            )
            tags = {
                tag['Key']: tag['Value']
                for tag in instance.get('Tags', volume.get('Tags', []))
            }
            rate = self._volume_price(volume['VolumeType'])
            cost = None
            if rate is not None:
                # A GB-month rate is prorated within each calendar month. This keeps
                # week, quarter and year totals consistent across unequal month lengths.
                fraction = 0.0
                cursor = disk_start
                while cursor < end:
                    month_start = cursor.floor('month')
                    month_end = month_start.shift(months=1)
                    segment_end = min(end, month_end)
                    fraction += (segment_end - cursor).total_seconds() / (
                        month_end - month_start
                    ).total_seconds()
                    cursor = segment_end
                cost = volume['Size'] * rate * fraction
            rows.append(
                MyCostsDisk(
                    volume_id=volume['VolumeId'],
                    desktop=tags.get('Name', 'Retained desktop disk'),
                    state='running'
                    if instance.get('State', {}).get('Name') == 'running'
                    else 'stopped',
                    size_gb=volume['Size'],
                    volume_type=volume['VolumeType'],
                    gb_month_rate=rate,
                    cost=None if cost is None else round(cost, 4),
                    status='unavailable' if cost is None else 'ready',
                )
            )
        return rows, any(row.cost is None for row in rows)

    def _home_usage(self):
        if getattr(self.context, 'storage_usage', None) is None:
            self.context.storage_usage = StorageUsageService(self.context)
        users = {}
        cursor = None
        measured_at = time.time()
        while True:
            result = self.context.accounts.list_users(
                ListUsersRequest(paginator={'cursor': cursor, 'page_size': 100})
            )
            for user in result.listing or []:
                # Do not divide by a sample of users who happened to open the browser.
                usage = self.context.storage_usage.measure_for_costs(user.username)
                if usage.get('state') != 'ready' or usage.get('partial'):
                    return None
                users[normalize_user(user.username, None)] = usage['total']['bytes']
            cursor = result.paginator.cursor if result.paginator else None
            if not cursor:
                return dict(users=users, measured_at=measured_at)

    def _storage(self, username, start, end):
        try:
            config = self.context.config()
            entries = config.get_config('shared-storage', default={}) or {}
            if hasattr(entries, 'as_plain_ordered_dict'):
                entries = entries.as_plain_ordered_dict()
            filesystems = {}
            for name, entry in entries.items():
                if not isinstance(entry, dict) and not hasattr(entry, 'get'):
                    continue
                provider = entry.get('provider')
                if provider not in (
                    'efs',
                    'fsx_netapp_ontap',
                    'fsx_lustre',
                    'fsx_windows_file_server',
                ):
                    continue
                fs_id = (entry.get(provider) or {}).get('file_system_id')
                if not fs_id:
                    continue
                filesystem = filesystems.setdefault(
                    fs_id, dict(names=[], provider=provider)
                )
                filesystem['names'].append(name)
            if not filesystems:
                return [], False
            collector = getattr(self.context, 'storage_metrics', None)
            snapshots = collector.usage_by_filesystem() if collector else {}
            cache = self._remember(('storage-rates',), dict)
            rows = []
            for fs_id, filesystem in filesystems.items():
                snapshot = snapshots.get(fs_id)
                # Older measurements must not silently masquerade as current usage.
                if (
                    snapshot
                    and time.time() - float(snapshot.get('measured_at', 0)) > 86400
                ):
                    snapshot = None
                if not snapshot or not snapshot.get('users'):
                    snapshot = (
                        self._remember(('home-usage',), self._home_usage)
                        if 'data' in filesystem['names']
                        else None
                    )
                users = (snapshot or {}).get('users', {})
                used = users.get(
                    normalize_user(username, None),
                    0 if (snapshot or {}).get('zero_when_absent') else None,
                )
                total = (snapshot or {}).get('total_bytes', sum(users.values()))
                share = used / total if used is not None and total > 0 else None
                amount = 0
                cursor = start
                while cursor < end:
                    daily = daily_storage_rate(
                        self.context,
                        filesystem['provider'],
                        fs_id,
                        cursor,
                        capacity_pool_bytes=(snapshot or {}).get(
                            'capacity_pool_bytes', 0
                        ),
                        cache=cache,
                    )
                    if daily is None:
                        amount = None
                        break
                    next_day = cursor.floor('day').shift(days=1)
                    segment_end = min(next_day, end)
                    amount += daily * (segment_end - cursor).total_seconds() / 86400
                    cursor = segment_end
                cost = (
                    amount * share if amount is not None and share is not None else None
                )
                if cost is not None:
                    try:
                        cost = self._convert(cost)
                    except ValueError:
                        cost = None
                rows.append(
                    MyCostsStorageShare(
                        filesystem=', '.join(filesystem['names']),
                        used_bytes=used,
                        share=share,
                        measured_at=(snapshot or {}).get('measured_at'),
                        cost=None if cost is None else round(cost, 4),
                        status='no_usage_data'
                        if share is None
                        else ('unavailable' if cost is None else 'ready'),
                        note=STORAGE_NOTE,
                    )
                )
            return rows, any(row.cost is None for row in rows)
        except Exception:
            self.logger.exception('Shared storage costs unavailable')
            return [], True
