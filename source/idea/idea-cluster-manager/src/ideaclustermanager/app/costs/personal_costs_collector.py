"""Leader-owned personal cost collection, independent of metrics export settings."""

import json
import os
import threading
import time

import arrow

from ideadatamodel import (
    ListUsersRequest,
    GetMyCostsResult,
    MyCostsMonth,
    MyCostsAmount,
    MyCostsDaily,
    MyCostsCoverage,
    SocaAmount,
    GetMyCostsSummaryResult,
    MyCostsAi,
    MyCostsAiProject,
    MyCostsJobs,
    MyCostsDesktops,
    constants,
    locale,
)
from ideaclustermanager.app.costs.monthly_costs_service import (
    MonthlyCostsService,
    period_bounds,
    JOBS_NOTE,
    DESKTOP_NOTE,
    AI_NOTE,
)
from ideaclustermanager.app.costs.my_costs_service import MyCostsService
from ideaclustermanager.app.costs.personal_costs_store import SYSTEM
from ideaclustermanager.app.costs.storage_rates import daily_storage_rate
from ideaclustermanager.app.metrics.storage_metrics_service import normalize_user
from ideasdk.filesystem.filesystem_helper import FileSystemHelper
from ideaclustermanager.app.filesystem.storage_usage import walk_home

FACETS = ('jobs', 'desktops', 'desktop_disks', 'shared_storage', 'ai')
STORAGE_NOTE = (
    'Rate estimate × dated byte share: provisioned SSD, throughput, IOPS above the '
    'included 3 per GB, and capacity-pool bytes; EFS by storage class, excluding '
    'throughput and requests. With a default user quota rule, users without files '
    'count as zero. Unattributed quota bytes remain unassigned. Historical days '
    'without a dated share are missing.'
)
DISKS_NOTE = (
    'Owned provisioned size × GB-month rate × calendar-month fraction, including stopped '
    'and retained disks and observed intervals before deletion. Inventory before collection, '
    'unobserved gaps, snapshots, extra IOPS and throughput are excluded.'
)
NOTES = (JOBS_NOTE, DESKTOP_NOTE, DISKS_NOTE, STORAGE_NOTE, AI_NOTE)


class DailyCostsCalculator(MonthlyCostsService):
    """Keep the existing compute, runtime, disk proration and token apportionment rules.

    Shared billing queries fetch a whole calendar month at DAILY granularity once;
    day calculations select those source values rather than dividing monthly totals.
    """

    def __init__(self, context, store):
        super().__init__(context)
        self.store = store
        self.source_times = {}
        self._used_sources = set()
        self._reported = set()
        self.inventory_day = None
        self.inventory_subject = None
        self.inventory_value = None
        self._run_reads = {}
        self._inventory_all = None
        self._inventory_as_of = None

    def begin(self):
        self._run_reads.clear()
        self._reads.pop(('storage-rates',), None)
        self._reported = set()
        self._inventory_all = None
        self._inventory_as_of = None
        self._reads = {
            key: value
            for key, value in self._reads.items()
            if time.monotonic() - value[0] < 86400
        }

        self.source_times = {
            key: value for key, value in self.source_times.items() if key in self._reads
        }

    def _remember(self, key, read):
        duration = 86400 if key[0] in ('volume-price', 'instance-price') else 3600
        cached = self._reads.get(key)
        if cached and time.monotonic() - cached[0] < duration:
            return cached[1]
        return super()._remember(key, read)

    def _cache_source(self, key, read):
        if key not in self._run_reads:
            if len(self._run_reads) >= 64:
                self._run_reads.pop(next(iter(self._run_reads)))
            self._run_reads[key] = read()
        return self._run_reads[key]

    def _search(self, index, body):
        key = ('search', index, json.dumps(body, sort_keys=True))
        return self._cache_source(
            key, lambda: super(DailyCostsCalculator, self)._search(index, body)
        )

    def _billing(self, start, end, filters, groups):
        month_start = start.floor('month')
        month_end = min(
            month_start.shift(months=1),
            arrow.utcnow().ceil('day').shift(microseconds=1),
        )
        request = dict(
            TimePeriod={
                'Start': month_start.format('YYYY-MM-DD'),
                'End': month_end.format('YYYY-MM-DD'),
            },
            Granularity='DAILY',
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
        key = ('daily-billing', json.dumps(request, sort_keys=True))
        self._used_sources.add(key)
        cached = self._reads.get(key)
        if not cached or time.monotonic() - cached[0] >= 21600:
            values = None
            try:
                if self.context.aws().aws_partition() == 'aws':
                    values = {}
                    query = dict(request)
                    while True:
                        response = (
                            self.context.aws()
                            .cost_explorer()
                            .get_cost_and_usage(**query)
                        )
                        for period in response.get('ResultsByTime', []):
                            day = period['TimePeriod']['Start']
                            entries = values.setdefault(day, {})
                            for group in period.get('Groups', []):
                                keys = tuple(k.split('$', 1)[-1] for k in group['Keys'])
                                metric = group['Metrics']['UnblendedCost']
                                entries[keys] = entries.get(keys, 0) + self._convert(
                                    metric['Amount'], metric['Unit']
                                )
                        if not response.get('NextPageToken'):
                            break
                        query['NextPageToken'] = response['NextPageToken']
            except Exception:
                self.logger.exception('Daily billing unavailable')
                values = None
            self._reads[key] = (time.monotonic(), values)
            self.source_times[key] = arrow.utcnow().isoformat()
        values = self._reads[key][1]
        if values is None:
            return None
        selected = {}
        cursor = start.floor('day')
        while cursor < end:
            day = cursor.format('YYYY-MM-DD')
            if day not in values:
                return None
            for keys, value in values[day].items():
                selected[keys] = selected.get(keys, 0) + value
            cursor = cursor.shift(days=1)
        return selected

    def _ai_project(self, username, project, start_date, end_date):
        try:
            return self._priced_ai_project(username, project, start_date, end_date)
        except Exception as error:
            if isinstance(error, ValueError):
                # An authored data condition (no billing, no token denominator), not a fault.
                self.logger.warning(f'Daily project attribution unavailable: {error}')
            else:
                self.logger.exception('Daily project attribution unavailable')
            return MyCostsAiProject(
                project_id=project.project_id,
                project_name=project.name,
                cost=0,
                cost_unavailable=True,
                invocations=0,
                total_tokens=0,
                by_model=[],
            )

    def _priced_ai_project(self, username, project, start_date, end_date):
        # The existing reader omits users with zero tokens. Check the denominator and
        # billing first so missing attribution, including spend without tokens, survives.
        key = ('tokens', project.project_id, start_date, end_date)
        rows = self._cache_source(
            key, lambda: self._usage_rows(project.project_id, start_date, end_date)
        )
        if rows is None:
            raise ValueError('Token attribution unavailable')
        spend = self._billing(
            arrow.get(start_date),
            arrow.get(end_date).shift(days=1),
            [{'Tags': {'Key': constants.IDEA_TAG_PROJECT, 'Values': [project.name]}}],
            [{'Type': 'DIMENSION', 'Key': 'SERVICE'}],
        )
        if spend is None:
            raise ValueError('Daily AI billing unavailable')
        amount = sum(
            value for (service,), value in spend.items() if 'bedrock' in service.lower()
        )
        if amount and self._total_tokens(rows) <= 0:
            raise ValueError('AI spend has no token denominator')
        reader = MyCostsService(self.context)
        reader._usage_rows = lambda *args: rows
        reader._project_spend = lambda *args: SocaAmount(amount=amount)
        return reader._ai_project(username, project, start_date, end_date)

    def _inventory(self, username):
        if self.inventory_subject == username:
            return self.inventory_value
        return super()._inventory(username)

    def _user_inventory(self, username):
        if (
            self._inventory_as_of
            and (arrow.utcnow() - self._inventory_as_of).total_seconds() >= 900
        ):
            self._inventory_all = None
            self._inventory_as_of = None
        if self._inventory_all is False:
            return None
        if self._inventory_all is None:
            self._inventory_all = False
            ec2 = self.context.aws().ec2()
            filters = [
                {
                    'Name': f'tag:{constants.IDEA_TAG_CLUSTER_NAME}',
                    'Values': [self.context.cluster_name()],
                },
                {
                    'Name': f'tag:{constants.IDEA_TAG_NODE_TYPE}',
                    'Values': [constants.NODE_TYPE_DCV_HOST],
                },
            ]
            instances, volumes = {}, {}
            for page in ec2.get_paginator('describe_instances').paginate(
                Filters=filters
            ):
                for reservation in page.get('Reservations', []):
                    for instance in reservation.get('Instances', []):
                        instances[instance['InstanceId']] = instance
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
            for page in ec2.get_paginator('describe_volumes').paginate(Filters=filters):
                for volume in page.get('Volumes', []):
                    volumes[volume['VolumeId']] = volume
            self._inventory_all = (instances, volumes)
            self._inventory_as_of = arrow.utcnow()

        def owner(item):
            return next(
                (
                    t['Value']
                    for t in item.get('Tags', [])
                    if t['Key'] == constants.IDEA_TAG_JOB_OWNER
                ),
                None,
            )

        instances, volumes = self._inventory_all
        mine = {
            key: instance
            for key, instance in instances.items()
            if owner(instance) == username
        }
        disks = [
            v
            for v in volumes.values()
            if owner(v) == username
            or any(a.get('InstanceId') in mine for a in v.get('Attachments', []))
        ]
        return mine, disks

    def capture_inventory(self, username, now):
        # Retain each observed day. No current inventory is used to fabricate history.
        try:
            value = self._user_inventory(username)
        except Exception:
            self.logger.exception('Desktop inventory unavailable')
            value = None
        if value is not None:
            instances, volumes = value
            now = (self._inventory_as_of or now).to(now.tzinfo)
            self._run_reads = {
                key: value
                for key, value in self._run_reads.items()
                if key[:2] != ('disk-history', username)
            }
            self.store.put_source(
                username,
                'inventory:' + now.format('YYYY-MM-DD') + ':' + str(now.int_timestamp),
                {
                    'as_of': now.isoformat(),
                    'instances': {
                        key: {
                            'State': item.get('State', {}),
                            'Tags': item.get('Tags', []),
                        }
                        for key, item in instances.items()
                    },
                    'volumes': [
                        {
                            key: arrow.get(v[key]).isoformat()
                            if key == 'CreateTime'
                            else v[key]
                            for key in (
                                'VolumeId',
                                'VolumeType',
                                'Size',
                                'CreateTime',
                                'Attachments',
                                'Tags',
                            )
                            if key in v
                        }
                        for v in volumes
                    ],
                },
            )

    def disk_day(self, username, start, end):
        snapshots = []
        for day in (start.shift(days=-1), start):
            key = ('disk-history', username, day.format('YYYY-MM-DD'))
            if key not in self._run_reads:
                if len(self._run_reads) >= 64:
                    self._run_reads.pop(next(iter(self._run_reads)))
                self._run_reads[key] = [
                    self.store.resolve_source(username, json.loads(row['payload']))
                    for row in self.store.records(
                        username, 'inventory:' + day.format('YYYY-MM-DD') + ':'
                    )
                ]
            snapshots.extend(self._run_reads[key])
        snapshots.sort(key=lambda item: arrow.get(item['as_of']).float_timestamp)
        amounts, missing, dates = [], 0, []
        for index, snapshot in enumerate(snapshots):
            observed = arrow.get(snapshot['as_of'])
            lower = max(start, observed)
            # Evidence has a bounded interval. Missed sweeps never extend an inventory
            # indefinitely, and a deleted disk retains the earlier observed intervals.
            upper = min(end, observed.shift(minutes=15))
            if index + 1 < len(snapshots):
                upper = min(upper, arrow.get(snapshots[index + 1]['as_of']))
            if lower >= upper:
                continue
            self.inventory_subject = username
            self.inventory_value = (snapshot['instances'], snapshot['volumes'])
            self._reads.pop(('inventory', username), None)
            rows, unavailable = self._disks(username, lower, upper)
            known = [row.cost for row in rows if row.cost is not None]
            if known or not rows:
                amounts.append(sum(known))
            missing += sum(row.cost is None for row in rows)
            dates.append(snapshot['as_of'])
        result = self._line(
            sum(amounts),
            not amounts,
            True,
            DISKS_NOTE
            + ' Dated observations cover at most 15 minutes each; gaps are unknown.',
            missing,
        )
        result.source_as_of = (
            max(dates, key=lambda value: arrow.get(value).float_timestamp)
            if dates
            else None
        )
        return result

    def _line(
        self,
        amount,
        missing=False,
        partial=False,
        reason='',
        missing_prices=0,
        inferred=0,
    ):
        return MyCostsAmount(
            cost=None if missing else round(amount or 0, 4),
            status='unavailable' if missing else ('partial' if partial else 'ready'),
            reason=reason,
            coverage=MyCostsCoverage(
                missing_prices=missing_prices, inferred_intervals=inferred
            ),
        )

    def _report(self, facet, error):
        # One entry per distinct failure per run: a systemic fault otherwise logs once per user-day.
        key = (facet, type(error).__name__, str(error)[:120])
        if key in self._reported:
            return
        self._reported.add(key)
        if isinstance(error, ValueError):
            self.logger.warning(f'Personal facet {facet} unavailable: {error}')
        else:
            self.logger.exception(f'Personal facet {facet} source unavailable')

    def day(self, username, start, end):
        lines = {}
        for facet in FACETS:
            self._used_sources = set()
            try:
                if facet == 'jobs':
                    value = self._compute_jobs(username, start, end)
                    missing = value.unpriced_jobs or 0
                    lines[facet] = self._line(
                        value.cost,
                        value.is_unavailable
                        or (
                            missing > 0
                            and missing == value.job_count
                            and not value.cost
                        ),
                        bool(missing),
                        'Completed-job compute; missing prices remain excluded.',
                        missing,
                    )
                elif facet == 'desktops':
                    value = self._desktops(username, start, end)
                    missing = value.unpriced_sessions or 0
                    inferred = sum(
                        bool(s.stop_time_estimated) for s in value.sessions or []
                    )
                    lines[facet] = self._line(
                        self._convert(value.cost or 0),
                        value.is_unavailable
                        or (missing > 0 and missing == value.session_count),
                        bool(missing or inferred),
                        DESKTOP_NOTE,
                        missing,
                        inferred,
                    )
                elif facet == 'desktop_disks':
                    lines[facet] = self.disk_day(username, start, end)
                elif facet == 'shared_storage':
                    lines[facet] = self.storage_day(username, start, end)
                else:
                    value = self._ai(
                        username, start.format('YYYY-MM-DD'), start.format('YYYY-MM-DD')
                    )
                    projects = value.projects or []
                    unavailable = bool(projects) and all(
                        p.cost_unavailable for p in projects
                    )
                    lines[facet] = self._line(
                        value.cost,
                        value.is_unavailable or unavailable,
                        any(p.cost_unavailable for p in projects),
                        AI_NOTE,
                    )
            except Exception as error:
                self._report(facet, error)
                reason = (
                    str(error)
                    if isinstance(error, ValueError) and str(error)
                    else 'Source unavailable.'
                )
                lines[facet] = self._line(None, True, reason=reason)
            dates = [
                self.source_times[key]
                for key in self._used_sources
                if key in self.source_times
            ]
            if lines[facet].source_as_of:
                dates.append(lines[facet].source_as_of)
            if dates:
                lines[facet].source_as_of = min(
                    dates, key=lambda value: arrow.get(value).float_timestamp
                )
        self.inventory_subject = None
        self.inventory_value = None
        self._reads.pop(('inventory', username), None)
        return lines

    def capture_storage(self, usernames, now):
        metrics = getattr(self.context, 'storage_metrics', None)
        snapshots = metrics.usage_by_filesystem() if metrics else {}
        entries = self.context.config().get_config('shared-storage', default={}) or {}
        if 'data' in entries:
            # A complete scan is useful measured evidence even when the allocation pool
            # cannot be separated from system space. It does not price the entire bill.
            homes = self._remember(('home-usage',), self._home_usage)
            if homes and all(
                normalize_user(user, None) in homes['users'] for user in usernames
            ):
                entry = entries['data']
                fs_id = (entry.get(entry.get('provider', None), None) or {}).get(
                    'file_system_id', None
                )
                mount = entry.get('mount_dir', None)
                if (
                    fs_id
                    and mount
                    and self._homes_on_filesystem(usernames, mount, fs_id)
                ):
                    day = (
                        arrow.get(homes['measured_at'])
                        .to(now.tzinfo)
                        .format('YYYY-MM-DD')
                    )
                    self.store.put_source(
                        SYSTEM, f'home-measurement:{day}:{fs_id}', homes
                    )
                    if entry.get('provider', None) == 'efs' and self._filesystem_root(
                        mount, fs_id
                    ):
                        root = self._remember(
                            ('filesystem-usage', fs_id), lambda: walk_home(mount)
                        )
                        if (
                            root
                            and root.get('state') == 'ready'
                            and not root.get('partial')
                            and root['total']['bytes'] > 0
                            and arrow.get(root['measured_at'])
                            .to(now.tzinfo)
                            .format('YYYY-MM-DD')
                            == day
                        ):
                            snapshots[fs_id] = dict(
                                filesystem_id=fs_id,
                                complete=True,
                                measured_at=min(
                                    homes['measured_at'], root['measured_at']
                                ),
                                users=homes['users'],
                                total_bytes=root['total']['bytes'],
                                allocation_pool='Home directories; other filesystem bytes remain unassigned',
                            )
        # Persist evidence by its actual measurement date, never by the requested month.
        for fs_id, snapshot in snapshots.items():
            measured = arrow.get(snapshot.get('measured_at', 0))
            snapshot = dict(snapshot)
            users = dict(snapshot.get('users', {}))
            if snapshot.get('zero_when_absent'):
                for username in usernames:
                    users.setdefault(normalize_user(username, None), 0)
            snapshot['users'] = users
            complete = (
                snapshot.get('complete') is True
                and snapshot.get('filesystem_id') == fs_id
                and all(normalize_user(u, None) in users for u in usernames)
                and 0 <= (now - measured).total_seconds() <= 86400
            )
            denominator = snapshot.get('total_bytes', 0)
            if (
                complete
                and denominator > 0
                and all(value >= 0 for value in users.values())
                and sum(users.values()) <= denominator
            ):
                self.store.put_source(
                    SYSTEM,
                    f'share:{measured.to(now.tzinfo).format("YYYY-MM-DD")}:{fs_id}',
                    snapshot,
                )

    @staticmethod
    def _filesystem_root(mount, fs_id):
        try:
            with open('/proc/mounts', encoding='utf-8') as stream:
                entries = [row.split() for row in stream if len(row.split()) >= 3]
            # A subdirectory mount cannot establish the filesystem denominator, nor
            # can a walk crossing another mounted filesystem. TLS proxy mounts without
            # a verifiable filesystem identity remain unavailable.
            return any(
                fs_id in row[0].split(':', 1)[0].split('.')
                and row[0].endswith(':/')
                and row[1] == mount
                for row in entries
            ) and not any(row[1].startswith(mount.rstrip('/') + '/') for row in entries)
        except OSError:
            return False

    def _homes_on_filesystem(self, usernames, mount, fs_id):
        try:
            # Config alone cannot establish which file system is actually mounted.
            with open('/proc/mounts', encoding='utf-8') as stream:
                mounted = any(
                    fs_id in row.split()[0] and row.split()[1] == mount
                    for row in stream
                    if len(row.split()) >= 2
                )
            if not mounted:
                return False
            for username in usernames:
                home = FileSystemHelper(self.context, username).get_user_home()
                if os.path.commonpath(
                    [os.path.realpath(home), os.path.realpath(mount)]
                ) != os.path.realpath(mount):
                    return False
                if os.stat(home).st_dev != os.stat(mount).st_dev:
                    return False
            return True
        except (OSError, ValueError):
            return False

    def storage_day(self, username, start, end):
        entries = self.context.config().get_config('shared-storage', default={}) or {}
        filesystems = {}
        missing_mapping = False
        for entry in entries.values():
            if not hasattr(entry, 'get'):
                continue
            provider = entry.get('provider', None)
            if provider not in (
                'efs',
                'fsx_netapp_ontap',
                'fsx_lustre',
                'fsx_windows_file_server',
            ):
                continue
            fs_id = (entry.get(provider, None) or {}).get('file_system_id', None)
            if fs_id:
                filesystems[fs_id] = provider
            else:
                missing_mapping = True
        if not filesystems:
            return self._line(0, missing_mapping)
        cache = self._remember(('storage-rates',), dict)
        amounts, dates = [], []
        missing_prices = 0
        for fs_id, provider in filesystems.items():
            snapshot = self.store.resolve_source(
                SYSTEM,
                self.store.get(SYSTEM, f'share:{start.format("YYYY-MM-DD")}:{fs_id}'),
            )
            used = (snapshot or {}).get('users', {}).get(normalize_user(username, None))
            daily = daily_storage_rate(
                self.context,
                provider,
                fs_id,
                start,
                capacity_pool_bytes=(snapshot or {}).get('capacity_pool_bytes', 0),
                cache=cache,
            )
            missing_prices += daily is None
            if snapshot and used is not None and daily is not None:
                try:
                    amount = self._convert(daily * used / snapshot['total_bytes'])
                except ValueError as error:
                    self._report('shared_storage', error)
                    continue
                amounts.append(amount)
                dates.append(arrow.get(snapshot['measured_at']).isoformat())
        result = self._line(
            sum(amounts),
            not amounts,
            missing_mapping or len(amounts) != len(filesystems),
            STORAGE_NOTE,
            missing_prices=missing_prices,
        )
        if amounts and result.status == 'ready':
            result.status = 'estimated_share'
        result.source_as_of = (
            min(dates, key=lambda value: arrow.get(value).float_timestamp)
            if dates
            else None
        )
        return result

    def month(self, username, start, end):
        daily = {facet: [] for facet in FACETS}
        metadata = {facet: [] for facet in FACETS}
        cursor = start
        while cursor < end:
            lines = self.day(username, cursor, min(cursor.shift(days=1), end))
            for facet, line in lines.items():
                daily[facet].append(
                    MyCostsDaily(
                        date=cursor.format('YYYY-MM-DD'),
                        day=cursor.day,
                        amount=line.cost,
                        status=line.status,
                    )
                )
                metadata[facet].append(line)
            cursor = cursor.shift(days=1)
        facets = {}
        for facet, note in zip(FACETS, NOTES):
            points = daily[facet]
            known = [point.amount for point in points if point.amount is not None]
            missing = len(points) - len(known)
            partial = missing or any(p.status == 'partial' for p in points)
            status = (
                'unavailable'
                if not known
                else (
                    'partial'
                    if partial
                    else (
                        'estimated_share'
                        if any(p.status == 'estimated_share' for p in points)
                        else 'ready'
                    )
                )
            )
            dates = [line.source_as_of for line in metadata[facet] if line.source_as_of]
            amount = round(sum(known), 4) if known else None
            facets[facet] = MyCostsAmount(
                cost=amount,
                amount=amount,
                status=status,
                note=note,
                reason=' '.join(
                    sorted({line.reason for line in metadata[facet] if line.reason})
                ),
                daily=points,
                source_as_of=max(
                    dates, key=lambda value: arrow.get(value).float_timestamp
                )
                if dates
                else end.isoformat(),
                coverage=MyCostsCoverage(
                    known_days=len(known),
                    missing_days=missing,
                    missing_prices=sum(
                        line.coverage.missing_prices for line in metadata[facet]
                    ),
                    inferred_intervals=sum(
                        line.coverage.inferred_intervals for line in metadata[facet]
                    ),
                ),
            )
        known = [line.cost for line in facets.values() if line.cost is not None]
        return MyCostsMonth(
            start_date=start.format('YYYY-MM-DD'),
            end_date=end.shift(microseconds=-1).format('YYYY-MM-DD')
            if end == end.floor('day')
            else end.format('YYYY-MM-DD'),
            total=round(sum(known), 4) if known else None,
            incomplete=any(
                line.status in ('partial', 'unavailable') for line in facets.values()
            ),
            **facets,
        )


class PersonalCostsCollector:
    def __init__(self, context, store, calculator=None):
        self.context, self.store = context, store
        self.calculator = calculator or DailyCostsCalculator(context, store)
        self.logger = context.logger('personal-costs-collector')
        self._stop = threading.Event()
        self._thread = None
        self._startup = True
        self._requests_thread = None
        self._requests_lock = threading.Lock()
        self._refresh_users = set()

    def start(self):
        self._requests_thread = threading.Thread(
            target=self._minute_checks, name='personal-costs-requests', daemon=True
        )
        self._requests_thread.start()
        self._thread = threading.Thread(
            target=self._run, name='personal-costs-collector', daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._requests_thread:
            self._requests_thread.join(timeout=5)
        if self._thread:
            self._thread.join(timeout=30)
        self.calculator._workers.shutdown(wait=False)

    def check_requests(self):
        if not self.context.is_leader():
            return
        pending = {user for user in self.users() if self.store.get(user, 'refresh')}
        with self._requests_lock:
            self._refresh_users.update(pending)
        self.store.put(SYSTEM, 'heartbeat', {'as_of': arrow.utcnow().isoformat()})

    def _minute_checks(self):
        while not self._stop.is_set():
            try:
                self.check_requests()
            except Exception:
                self.logger.exception('Personal refresh request check failed')
            self._stop.wait(60)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                self.logger.exception(
                    'Personal cost collection failed; retaining published values'
                )
            self._stop.wait(60)

    def users(self):
        cursor = None
        while True:
            result = self.context.accounts.list_users(
                ListUsersRequest(paginator={'cursor': cursor, 'page_size': 100})
            )
            yield from (user.username for user in result.listing or [])
            cursor = result.paginator.cursor if result.paginator else None
            if not cursor:
                break

    def check(self):
        if not self.context.is_leader():
            return
        lock = self.context.distributed_lock()
        lock.acquire(key='personal-costs-collector')
        try:
            if self.context.is_leader():
                self._check_leader()
        finally:
            lock.release(key='personal-costs-collector')

    @staticmethod
    def retain_known(candidate, previous):
        if previous is None or candidate.start_date != previous.start_date:
            return False
        retained = False
        for facet in FACETS:
            line, old = getattr(candidate, facet), getattr(previous, facet)
            old_days = {
                point.date: point for point in old.daily if point.amount is not None
            }
            restored = False
            for index, point in enumerate(line.daily):
                if point.amount is None and point.date in old_days:
                    saved_day = old_days[point.date].model_copy(deep=True)
                    saved_day.status = 'partial'
                    line.daily[index] = saved_day
                    restored = True
            if restored:
                line.cost = line.amount = round(
                    sum(
                        point.amount for point in line.daily if point.amount is not None
                    ),
                    4,
                )
                line.status = 'partial'
                line.reason = (
                    'Source unavailable during refresh; retaining previously known daily amounts. '
                    + line.reason
                )
                dates = [
                    value for value in (line.source_as_of, old.source_as_of) if value
                ]
                line.source_as_of = (
                    min(dates, key=lambda value: arrow.get(value).float_timestamp)
                    if dates
                    else None
                )
                if line.coverage:
                    line.coverage.known_days = sum(
                        point.amount is not None for point in line.daily
                    )
                    line.coverage.missing_days = sum(
                        point.amount is None for point in line.daily
                    )
                retained = True
            if line.cost is None and old.cost is not None:
                saved = old.model_copy(deep=True)
                recorded = {point.date for point in saved.daily}
                saved.daily.extend(
                    point for point in line.daily if point.date not in recorded
                )
                saved.status = 'partial'
                saved.reason = (
                    'Source unavailable during refresh; retaining the previous known amounts. '
                    + old.reason
                )
                if saved.coverage:
                    saved.coverage.missing_days = sum(
                        point.amount is None for point in saved.daily
                    )
                setattr(candidate, facet, saved)
                retained = True
        values = [getattr(candidate, facet).cost for facet in FACETS]
        candidate.total = (
            round(sum(v for v in values if v is not None), 4)
            if any(v is not None for v in values)
            else None
        )
        candidate.incomplete = any(
            getattr(candidate, facet).status in ('partial', 'unavailable')
            for facet in FACETS
        )
        return retained

    def _check_leader(self):
        now = arrow.utcnow()
        state = self.store.get(SYSTEM, 'state') or {}
        users = list(self.users())
        requests = {user: self.store.get(user, 'refresh') for user in users}
        due = self._startup or now >= arrow.get(state.get('next_run', now.isoformat()))
        selected = users if due else [user for user in users if requests[user]]
        state['heartbeat'] = now.isoformat()
        self.store.put(SYSTEM, 'state', state)
        if not selected:
            return
        self._startup = False
        started = time.monotonic()
        state['run_started'] = now.isoformat()
        state['next_run'] = now.shift(minutes=15).isoformat()
        self.store.put(SYSTEM, 'state', state)
        timezone = self.context.config().get_string('cluster.timezone', 'UTC') or 'UTC'
        local = now.to(timezone)
        start = local.floor('month')
        self.calculator.begin()
        try:
            self.calculator.capture_storage(users, local)
        except Exception:
            self.logger.exception(
                'Storage scan unavailable; collecting the remaining facets'
            )
        for index, username in enumerate(selected):
            if self._stop.is_set() or not self.context.is_leader():
                return
            local = arrow.utcnow().to(timezone)
            start = local.floor('month')
            request = self.store.get(username, 'refresh')
            try:
                try:
                    self.calculator.capture_inventory(username, local)
                except Exception:
                    self.logger.exception(
                        'Inventory unavailable; collecting the remaining facets'
                    )
                current = self.calculator.month(username, start, local)
                previous = self.calculator.month(
                    username, start.shift(months=-1), start
                )
                old_head = self.store.get(username, 'head')
                old = (
                    GetMyCostsResult(
                        **self.store.projection(username, old_head, 'costs')
                    )
                    if old_head
                    else None
                )
                retained_current = self.retain_known(
                    current, old.current if old else None
                )
                retained_previous = self.retain_known(
                    previous, old.previous if old else None
                )
                dates = [
                    getattr(month, facet).source_as_of
                    for month in (current,)
                    for facet in FACETS
                    if getattr(month, facet).source_as_of
                    and getattr(month, facet).cost is not None
                ]
                result = GetMyCostsResult(
                    currency=locale.get_currency_code(),
                    state='stale' if retained_current or retained_previous else 'ready',
                    timezone=timezone,
                    refreshed_at=min(
                        dates, key=lambda value: arrow.get(value).float_timestamp
                    )
                    if dates
                    else local.isoformat(),
                    current=current,
                    previous=previous,
                )
                # Compatibility keeps its trailing window, independently of calendar months.
                try:
                    summary = MyCostsService(self.context).get_summary(username)
                except Exception:
                    summary = GetMyCostsSummaryResult(
                        username=username,
                        window='last_30_days',
                        ai=MyCostsAi(is_unavailable=True),
                        jobs=MyCostsJobs(is_unavailable=True),
                        desktops=MyCostsDesktops(is_unavailable=True),
                    )
                tickers = {}
                configured = self.context.config().get_string(
                    'cluster-manager.web_portal.cost_ticker.period', 'mtd'
                )
                if configured in ('wtd', 'qtd', 'ytd'):
                    lower, upper = period_bounds(configured, local, timezone)
                    pieces = []
                    while lower < upper:
                        right = min(lower.floor('month').shift(months=1), upper)
                        pieces.append(self.calculator.month(username, lower, right))
                        lower = right
                    known = [p.total for p in pieces if p.total is not None]
                    tickers[configured] = dict(
                        total=round(sum(known), 4) if known else None,
                        incomplete=any(p.incomplete for p in pieces),
                    )
                if not self.context.is_leader():
                    return
                self.context.distributed_lock().assert_held(
                    key='personal-costs-collector'
                )
                self.store.publish(username, result, summary, tickers)
                self.store.acknowledge(username, request)
            except Exception:
                self.logger.exception(
                    'Personal costs unavailable for a user; retaining generation'
                )
            with self._requests_lock:
                queued = sorted(self._refresh_users)
                self._refresh_users.clear()
            for user in queued:
                if self.store.get(user, 'refresh'):
                    # Prioritize requests found by the independent minute checker.
                    # Already acknowledged records cannot cause duplicate calculations.
                    if user in selected[index + 1 :]:
                        del selected[selected.index(user, index + 1)]
                    selected.insert(index + 1, user)
            state['heartbeat'] = arrow.utcnow().isoformat()
            self.store.put(SYSTEM, 'state', state)
        state['last_duration'] = max(1, int(time.monotonic() - started))
        state.pop('run_started', None)
        state['next_run'] = max(arrow.utcnow(), now.shift(minutes=15)).isoformat()
        self.store.put(SYSTEM, 'state', state)
