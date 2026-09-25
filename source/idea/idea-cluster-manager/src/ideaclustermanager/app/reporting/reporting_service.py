"""Selected-period estimates with explicit source and metric coverage."""

import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ideadatamodel import (
    MetricCoverage,
    ReportingRow,
    ReportingPeriod,
    ReportingSummary,
    ListReportingRowsResult,
    ReportingPaginator,
    exceptions,
    locale,
)
from .reporting_sources import ReportingSources, number, timestamp, subject
from .snapshot_store import (
    SnapshotStore,
    FACETS,
    BUILD_SECONDS,
    MAX_ROWS,
    check_deadline,
    fail,
    sort_rows,
    too_large,
)
from .csv_export import export_csv

HOUR = Decimal(3600)
ACTIVITY = (
    'job_count',
    'node_hours',
    'requested_walltime_hours',
    'elapsed_hours',
    'efficiency_pct',
    'desktop_hours',
    'idle_stops',
)


def coverage(status, reason='', **kwargs):
    return MetricCoverage(status=status, reason=reason, **kwargs).model_dump(
        mode='json'
    )


def combine(values, reason=''):
    if not values:
        return coverage('unavailable', reason or 'No recorded coverage.')
    active = [value for value in values if value['status'] != 'not_applicable']
    statuses = {value['status'] for value in active}
    if not active:
        status = 'not_applicable'
    elif statuses == {'unavailable'}:
        status = 'unavailable'
    elif 'unavailable' in statuses or 'partial' in statuses:
        status = 'partial'
    elif 'estimated' in statuses:
        status = 'estimated'
    else:
        status = 'ready'
    times = [
        value['source_as_of']
        for value in values
        if value.get('source_as_of') and timestamp(value['source_as_of']) is not None
    ]
    starts = [
        value['available_start'] for value in values if value.get('available_start')
    ]
    ends = [value['available_end'] for value in values if value.get('available_end')]
    return coverage(
        status,
        reason or ' '.join(sorted({v['reason'] for v in values if v.get('reason')})),
        source_as_of=min(times, key=timestamp) if times else None,
        freshness_spread_seconds=max(map(timestamp, times)) - min(map(timestamp, times))
        if times
        else None,
        available_start=min(starts) if starts else None,
        available_end=max(ends) if ends else None,
        **{
            key: sum(v.get(key, 0) for v in values)
            for key in (
                'missing_days',
                'missing_records',
                'eligible_count',
                'total_count',
            )
        },
    )


def known_sum(values):
    known = [Decimal(str(value)) for value in values if value is not None]
    return sum(known, Decimal(0)) if known else None


def resolve_period(request, timezone_name, now):
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise exceptions.invalid_params('Invalid cluster timezone') from None
    local = now.astimezone(zone)
    today = local.date()
    if request.period != 'custom' and (
        request.start_date is not None or request.end_date is not None
    ):
        raise exceptions.invalid_params('Dates are accepted only for a custom period')
    if request.period == 'custom':
        try:
            if not all(
                isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)
                for value in (request.start_date, request.end_date)
            ):
                raise ValueError()
            first, last = (
                date.fromisoformat(request.start_date),
                date.fromisoformat(request.end_date),
            )
        except (ValueError, TypeError):
            raise exceptions.invalid_params('Custom dates must be YYYY-MM-DD') from None
        if first > last or last > today or (last - first).days >= 366:
            raise exceptions.invalid_params(
                'Custom period must be ordered, not future, and at most 366 days'
            )
    elif request.period == 'last_30_days':
        first, last = today - timedelta(days=29), today
    elif request.period == 'this_month':
        first, last = today.replace(day=1), today
    else:
        last = today.replace(day=1) - timedelta(days=1)
        first = last.replace(day=1)
    start = datetime.combine(first, datetime.min.time(), zone)
    exclusive = datetime.combine(last + timedelta(days=1), datetime.min.time(), zone)
    end = min(exclusive, local)
    return ReportingPeriod(
        period=request.period,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        start=start.isoformat(),
        end=end.isoformat(),
        provisional=last == today,
    )


def walltime(value):
    if not isinstance(value, str):
        return number(value)
    if value.isdigit():
        return number(value)
    match = re.fullmatch(r'(?:(\d+)-)?(\d+):(\d{2}):(\d{2})', value)
    if not match:
        return None
    days, hours, minutes, seconds = [int(part or 0) for part in match.groups()]
    if minutes >= 60 or seconds >= 60:
        return None
    return Decimal(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def job_values(job, currency):
    params = job.get('params') or {}
    requested = walltime(params.get('walltime'))
    start, end = timestamp(job.get('start_time')), timestamp(job.get('end_time'))
    elapsed = number(job.get('total_time_secs'))
    if elapsed is None and start is not None and end is not None and end >= start:
        elapsed = Decimal(str(end - start))
    started = start is not None and end is not None and start <= end
    matched = (
        started and requested is not None and requested > 0 and elapsed is not None
    )
    nodes = number(params.get('nodes'))
    bom = job.get('estimated_bom_cost') or {}
    lines = [
        line for line in bom.get('line_items') or [] if line.get('service') == 'aws.ec2'
    ]
    prices = []
    for line in lines:
        price = line.get('total_price') or {}
        amount = number(price.get('amount'))
        if price.get('unit') != currency:
            amount = None
        if amount is None and not price:
            unit_price = line.get('unit_price') or {}
            rate, quantity = (
                number(unit_price.get('amount')),
                number(line.get('quantity')),
            )
            if (
                unit_price.get('unit') == currency
                and line.get('unit') in ('per hour', 'per second', 'per minute')
                and rate is not None
                and quantity is not None
            ):
                amount = rate * quantity
        prices.append(amount)
    cost = (
        known_sum(prices)
        if lines
        and all(price is not None for price in prices)
        and not bom.get('price_unavailable')
        else None
    )
    return dict(
        cost=cost,
        requested=requested / HOUR if matched else None,
        elapsed=elapsed / HOUR if matched else None,
        matched=bool(matched),
        node_hours=nodes * elapsed / HOUR
        if started and nodes is not None and elapsed is not None
        else None,
    )


def new_row(key, label, project_id=None):
    row = ReportingRow(key=key, label=label, project_id=project_id).model_dump()
    row['spend_by_facet'] = dict.fromkeys(FACETS)
    row['coverage'] = {
        key: coverage('unavailable', 'No recorded data.')
        for key in ('spend_total', *FACETS, *ACTIVITY)
    }
    row['coverage']['idle_stops'] = coverage(
        'unavailable', 'No durable idle-stop reason events.'
    )
    return row


def selected_money(projection, facet, period, currency, timezone_name):
    costs = projection.get('costs')
    if not costs:
        return None, coverage(
            'unavailable',
            'Collecting stored projection.'
            if projection.get('state') == 'collecting'
            else 'Stored projection unavailable.',
        )
    if costs.get('currency') != currency or costs.get('timezone') != timezone_name:
        return None, coverage(
            'unavailable',
            'Stored currency or timezone does not match reporting configuration.',
        )
    points, source_times, reasons = {}, [], []
    for month in ('previous', 'current'):
        line = (costs.get(month) or {}).get(facet) or {}
        for point in line.get('daily') or []:
            points[point['date']] = point
        if line.get('source_as_of'):
            source_times.append(line['source_as_of'])
        if line.get('reason'):
            reasons.append(line['reason'])
    dates = []
    cursor = date.fromisoformat(period.start_date)
    while cursor <= date.fromisoformat(period.end_date):
        dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    selected = [points.get(day, {}) for day in dates]
    amounts = [
        number(point.get('amount'))
        if point.get('status') not in ('unavailable', 'not_applicable')
        else None
        for point in selected
    ]
    known = [day for day, amount in zip(dates, amounts) if amount is not None]
    missing = len(dates) - len(known)
    stale = projection.get('state') == 'stale'
    status = (
        'unavailable'
        if not known
        else 'partial'
        if missing or stale or any(p.get('status') == 'partial' for p in selected)
        else 'estimated'
    )
    if stale:
        reasons.append('Retained values are stale after a source failure.')
    if missing:
        reasons.append('Selected historical dates are missing.')
    if period.provisional:
        reasons.append('Current day is provisional.')
    times = [value for value in source_times if timestamp(value) is not None]
    return known_sum(amounts), coverage(
        status,
        ' '.join(sorted(set(reasons))) or 'Recorded estimates and allocations.',
        available_start=min(known) if known else None,
        available_end=max(known) if known else None,
        source_as_of=min(times, key=timestamp)
        if times
        else (projection.get('head') or {}).get('as_of'),
        missing_days=missing,
        eligible_count=len(known),
        total_count=len(dates),
    )


class ReportingService:
    def __init__(self, context, store=None, sources=None):
        self.context = context
        self.store = store or SnapshotStore(context)
        self.sources = sources or ReportingSources(context)
        self._builds = ThreadPoolExecutor(max_workers=2, thread_name_prefix='reporting')

    def get_summary(self, actor, request, authorize):
        if not authorize():
            raise exceptions.unauthorized_access()
        deadline = time.monotonic() + BUILD_SECONDS
        future = self._builds.submit(
            self._get_summary, actor, request, authorize, deadline
        )
        try:
            return future.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError:
            future.cancel()
            fail(
                'REPORT_TIMEOUT',
                'Report build timed out. Retry with a narrower period.',
            )

    def _get_summary(self, actor, request, authorize, deadline):
        check_deadline(deadline)
        timezone_name = self.context.config().get_string(
            'cluster.timezone', required=True
        )
        currency = locale.get_currency_code()
        if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
            raise exceptions.invalid_params('Invalid reporting currency configuration')
        now = datetime.now(timezone.utc)
        period = resolve_period(request, timezone_name, now)
        data = self.sources.read(period, deadline)
        tables, tiles, covers, warnings = self.build(
            data, period, currency, timezone_name, deadline
        )
        times = [
            value['source_as_of']
            for value in covers.values()
            if value.get('source_as_of')
            and timestamp(value['source_as_of']) is not None
        ]
        summary = dict(
            period=period.model_dump(mode='json'),
            currency=currency,
            timezone=timezone_name,
            as_of=min(times, key=timestamp) if times else None,
            tiles=tiles,
            coverage=covers,
            warnings=warnings,
        )
        if not authorize():
            raise exceptions.unauthorized_access()
        summary = self.store.publish(actor, summary, tables, deadline)
        return ReportingSummary.model_validate(summary)

    @staticmethod
    def build(data, period, currency, timezone_name, deadline):
        users = {
            key: new_row(key, label)
            for key, label in data['users'].items()
            if subject(key)
        }
        projects = {
            key: new_row(key, label, key) for key, label in data['projects'].items()
        }
        warnings = list(data['warnings'])
        for key, row in users.items():
            check_deadline(deadline)
            for facet in FACETS:
                row['spend_by_facet'][facet], row['coverage'][facet] = selected_money(
                    data['projections'].get(key, dict(state='collecting')),
                    facet,
                    period,
                    currency,
                    timezone_name,
                )
                if (
                    facet == 'jobs'
                    and data['coverage'].get('jobs') == 'not_applicable'
                    or facet in ('desktops', 'desktop_disks')
                    and data['coverage'].get('desktops') == 'not_applicable'
                ):
                    if row['spend_by_facet'][facet] is None:
                        row['coverage'][facet] = coverage(
                            'not_applicable', 'Module is not deployed.'
                        )
            row['spend_total'] = known_sum(row['spend_by_facet'].values())
            row['coverage']['spend_total'] = combine(
                [row['coverage'][facet] for facet in FACETS]
            )

        def project_for(source):
            project_id = source.get('project_id')
            raw = source.get('project')
            if isinstance(raw, dict):
                project_id = project_id or raw.get('project_id')
                raw = raw.get('name')
            if not project_id and raw:
                project_id = data.get('project_names', {}).get(raw)
                if not project_id:
                    key = f'!historical-name:{raw}'
                    return projects.setdefault(
                        key, new_row(key, f'Historical project name: {raw}')
                    )
            key = str(project_id) if project_id else '!unassigned'
            label = (
                source.get('project_title')
                or source.get('project_name')
                or project_id
                or 'Unassigned'
            )
            row = projects.setdefault(
                key, new_row(key, str(label), str(project_id) if project_id else None)
            )
            if source.get('project_name') or source.get('project_title'):
                row['label'] = str(label)
            return row

        start, end = timestamp(period.start), timestamp(period.end)
        jobs = {}
        for hit in sorted(
            data['jobs'],
            key=lambda hit: (str(hit.get('_index', '')), str(hit.get('_id', ''))),
        ):
            job = hit.get('_source', {})
            identity = job.get('job_uid') or hit.get('_id')
            if not identity or not subject(job.get('owner')):
                continue
            jobs[identity] = job
        user_jobs, project_jobs = {}, {}
        for job in jobs.values():
            check_deadline(deadline)
            project = project_for(job)
            ended = timestamp(job.get('end_time'))
            if (
                job.get('state')
                not in (
                    'finished',
                    'exit',
                    'success',
                    'failed',
                    'failure',
                    'cancelled',
                    'canceled',
                )
                or ended is None
                or not start <= ended < end
            ):
                continue
            values = job_values(job, currency)
            user_jobs.setdefault(job['owner'], []).append(values)
            project_jobs.setdefault(project['key'], []).append(values)
        job_status = data['coverage'].get('jobs', 'unavailable')
        for collection, grouped in ((users, user_jobs), (projects, project_jobs)):
            for key, row in collection.items():
                entries = grouped.get(key, [])
                readable = job_status not in ('unavailable', 'not_applicable')
                matched = [entry for entry in entries if entry['matched']]
                row['job_count'] = len(entries) if readable else None
                row['requested_walltime_hours'] = known_sum(
                    entry['requested'] for entry in matched
                )
                row['elapsed_hours'] = known_sum(entry['elapsed'] for entry in matched)
                row['efficiency_pct'] = (
                    100 * row['elapsed_hours'] / row['requested_walltime_hours']
                    if matched
                    else None
                )
                row['node_hours'] = known_sum(entry['node_hours'] for entry in entries)
                if readable and not entries:
                    row['node_hours'] = Decimal(0)
                for metric in ACTIVITY[:5]:
                    eligible = (
                        len(entries)
                        if metric == 'job_count'
                        else sum(entry['node_hours'] is not None for entry in entries)
                        if metric == 'node_hours'
                        else len(matched)
                    )
                    status = (
                        job_status
                        if not readable
                        else 'unavailable'
                        if row[metric] is None
                        else 'partial'
                        if job_status == 'partial' or eligible < len(entries)
                        else 'estimated'
                        if metric == 'node_hours'
                        else 'ready'
                    )
                    row['coverage'][metric] = coverage(
                        status,
                        'Completed records; efficiency uses matched started jobs only.',
                        eligible_count=eligible,
                        total_count=len(entries),
                        missing_records=len(entries) - eligible,
                    )
                if collection is projects:
                    row['spend_by_facet']['jobs'] = known_sum(
                        entry['cost'] for entry in entries
                    )
                    if readable and not entries:
                        row['spend_by_facet']['jobs'] = Decimal(0)
                    priced = sum(entry['cost'] is not None for entry in entries)
                    row['coverage']['jobs'] = coverage(
                        job_status
                        if not readable
                        else 'unavailable'
                        if entries and not priced
                        else 'partial'
                        if priced < len(entries) or job_status == 'partial'
                        else 'estimated',
                        'Recorded job-compute estimates only.',
                        eligible_count=priced,
                        total_count=len(entries),
                        missing_records=len(entries) - priced,
                    )
                    for facet in FACETS[1:]:
                        row['coverage'][facet] = coverage(
                            'unavailable', 'No recorded project split.'
                        )
                    row['spend_total'] = row['spend_by_facet']['jobs']
                    row['coverage']['spend_total'] = combine(
                        [row['coverage'][facet] for facet in FACETS]
                    )
        sessions = {}
        for hit in data['desktops']:
            source = hit.get('_source', {})
            identity = source.get('idea_session_id')
            if not identity or not subject(source.get('owner')):
                continue
            rank = (
                bool(hit.get('_history')),
                timestamp(source.get('updated_on') or source.get('deleted_on')) or 0,
                str(hit.get('_id', '')),
            )
            if identity not in sessions or rank > sessions[identity][0]:
                sessions[identity] = (rank, source)
        user_desktops, project_desktops = {}, {}
        for _, source in sessions.values():
            check_deadline(deadline)
            project = project_for(source)
            created = timestamp(source.get('created_on'))
            stopped = timestamp(source.get('stopped_on'))
            if stopped is None:
                if source.get('state') in (
                    'PROVISIONING',
                    'CREATING',
                    'INITIALIZING',
                    'READY',
                    'RESUMING',
                ):
                    stopped = end
                else:
                    stopped = timestamp(
                        source.get('deleted_on') or source.get('updated_on')
                    )
            if created is not None and (
                created >= end or stopped is not None and stopped <= start
            ):
                continue
            hours = (
                Decimal(str(max(0, min(stopped, end) - max(created, start)))) / HOUR
                if created is not None and stopped is not None and stopped >= created
                else None
            )
            user_desktops.setdefault(source['owner'], []).append(hours)
            project_desktops.setdefault(project['key'], []).append(hours)
        desktop_status = combine(
            [
                coverage(data['coverage'].get(name, 'unavailable'))
                for name in ('desktops', 'desktop_history')
            ]
        )['status']
        for collection, grouped in (
            (users, user_desktops),
            (projects, project_desktops),
        ):
            for key, row in collection.items():
                entries = grouped.get(key, [])
                readable = desktop_status not in ('unavailable', 'not_applicable')
                row['desktop_hours'] = (
                    known_sum(entries) if entries else Decimal(0) if readable else None
                )
                eligible = sum(value is not None for value in entries)
                row['coverage']['desktop_hours'] = coverage(
                    desktop_status
                    if not readable
                    else 'partial'
                    if desktop_status == 'partial' or eligible < len(entries)
                    else 'estimated',
                    'Creation-to-stop overlap; stop/restart gaps are unrecoverable.',
                    eligible_count=eligible,
                    total_count=len(entries),
                    missing_records=len(entries) - eligible,
                )
        total = new_row('total', 'Recorded spend')
        for facet in FACETS:
            total['spend_by_facet'][facet] = known_sum(
                row['spend_by_facet'][facet] for row in users.values()
            )
            total['coverage'][facet] = combine(
                [row['coverage'][facet] for row in users.values()]
            )
        total['spend_total'] = known_sum(total['spend_by_facet'].values())
        total['coverage']['spend_total'] = combine(
            [total['coverage'][facet] for facet in FACETS]
        )
        for metric in ACTIVITY:
            if metric != 'efficiency_pct':
                total[metric] = known_sum(row[metric] for row in users.values())
            total['coverage'][metric] = combine(
                [row['coverage'][metric] for row in users.values()]
            )
        if total['requested_walltime_hours']:
            total['efficiency_pct'] = (
                100 * total['elapsed_hours'] / total['requested_walltime_hours']
            )
        facet_rows = []
        for facet in FACETS:
            row = new_row(facet, facet.replace('_', ' ').title())
            row['spend_by_facet'][facet] = total['spend_by_facet'][facet]
            row['spend_total'] = total['spend_by_facet'][facet]
            row['coverage'][facet] = total['coverage'][facet]
            row['coverage']['spend_total'] = total['coverage'][facet]
            for metric in ACTIVITY:
                applicable = (
                    facet == 'jobs'
                    and metric in ACTIVITY[:5]
                    or facet == 'desktops'
                    and metric in ACTIVITY[5:]
                )
                row[metric] = total[metric] if applicable else None
                row['coverage'][metric] = (
                    total['coverage'][metric]
                    if applicable
                    else coverage('not_applicable')
                )
            facet_rows.append(row)
        unallocated = new_row('!unallocated', 'Unallocated to project')
        for facet in FACETS[1:]:
            unallocated['spend_by_facet'][facet] = total['spend_by_facet'][facet]
            unallocated['coverage'][facet] = total['coverage'][facet]
        unallocated['spend_total'] = known_sum(unallocated['spend_by_facet'].values())
        unallocated['coverage']['spend_total'] = combine(
            [unallocated['coverage'][facet] for facet in FACETS[1:]]
        )
        if unallocated['spend_total'] is not None:
            projects[unallocated['key']] = unallocated
        priced_projects = [
            row
            for row in projects.values()
            if row['project_id']
            and row['spend_by_facet']['jobs'] is not None
            and row['coverage']['jobs']['eligible_count'] > 0
        ]
        top = (
            sort_rows(priced_projects, 'jobs', True)[0]
            if priced_projects
            else new_row('top_project', 'Top project unavailable')
        )
        warnings.append(
            'Top project: by recorded job-compute spend; other facets unallocated'
        )
        raw_job_spend = known_sum(
            entry['cost'] for entries in project_jobs.values() for entry in entries
        )
        difference = new_row(
            'job_spend_difference', 'Projection minus recorded job-compute spend'
        )
        if total['spend_by_facet']['jobs'] is not None and raw_job_spend is not None:
            difference['spend_total'] = total['spend_by_facet']['jobs'] - raw_job_spend
        difference['coverage']['spend_total'] = coverage(
            'partial' if difference['spend_total'] is not None else 'unavailable',
            'Signed timing, coverage and billing-basis difference; not consumption or project allocation.',
        )
        tables = dict(
            user=list(users.values()), project=list(projects.values()), facet=facet_rows
        )
        if any(len(rows) > MAX_ROWS for rows in tables.values()):
            too_large()
        check_deadline(deadline)
        covers = dict(total['coverage'])
        covers.update(
            {
                f'source_{key}': coverage(status)
                for key, status in data['coverage'].items()
            }
        )
        projections = list(data['projections'].values())
        if projections:
            covers['source_projections'] = combine(
                [
                    coverage(
                        'ready'
                        if value.get('state') == 'ready'
                        else 'partial'
                        if value.get('state') == 'stale'
                        else 'unavailable',
                        'Collecting stored projection.'
                        if value.get('state') == 'collecting'
                        else '',
                        eligible_count=int(value.get('costs') is not None),
                        total_count=1,
                        missing_records=int(value.get('costs') is None),
                    )
                    for value in projections
                ]
            )
        head_times = [
            (projection.get('head') or {}).get('as_of')
            for projection in data['projections'].values()
        ]
        head_times = [value for value in head_times if timestamp(value) is not None]
        covers['source_projections'].update(
            source_as_of=min(head_times, key=timestamp) if head_times else None,
            freshness_spread_seconds=max(map(timestamp, head_times))
            - min(map(timestamp, head_times))
            if head_times
            else None,
        )
        return (
            tables,
            dict(total=total, top_project=top, job_spend_difference=difference),
            covers,
            warnings,
        )

    def list_rows(self, actor, request, authorize):
        metadata = self.store.lookup(request.snapshot_id, actor, authorize)
        rows = sort_rows(
            self.store.rows(request.snapshot_id, metadata, request.table),
            request.sort_by,
            request.descending,
        )
        binding = dict(
            actor=actor,
            snapshot_id=request.snapshot_id,
            table=request.table,
            sort_by=request.sort_by,
            descending=request.descending,
        )
        offset = self.store.offset(metadata, binding, request.paginator.cursor)
        if offset > len(rows):
            raise exceptions.invalid_params('Invalid reporting cursor offset')
        end = offset + request.paginator.page_size
        cursor = self.store.cursor(metadata, binding, end) if end < len(rows) else None
        return ListReportingRowsResult(
            listing=rows[offset:end],
            paginator=ReportingPaginator(
                page_size=request.paginator.page_size, cursor=cursor
            ),
            total_rows=len(rows),
            coverage=metadata['summary']['coverage'],
            warnings=metadata['summary']['warnings'],
        )

    def export(self, actor, request, authorize):
        metadata = self.store.lookup(request.snapshot_id, actor, authorize)
        rows = sort_rows(
            self.store.rows(request.snapshot_id, metadata, request.table),
            request.sort_by,
            request.descending,
        )
        result = export_csv(metadata['summary'], rows, request.table, request.columns)
        self.store.check_expiry(metadata)
        return result
