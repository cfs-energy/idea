"""Selected-period insights built from retained activity and current budgets."""

import time
from collections import defaultdict, OrderedDict
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import RLock
from functools import lru_cache
from zoneinfo import ZoneInfo

from ideadatamodel import ReportingInsights, GetUserProjectsRequest, exceptions, locale
from ideadatamodel.reporting.efficiency import job_efficiency
from ideaclustermanager.app.costs.my_costs_service import MyCostsService
from .reporting_service import resolve_period, known_sum, selected_money
from .reporting_sources import ReportingSources, number, timestamp, subject, user_label
from .snapshot_store import BUILD_SECONDS, TTL_SECONDS, check_deadline

FINISHED = {'finished', 'exit', 'success', 'failed', 'failure', 'cancelled', 'canceled'}


def ranked(groups, limit=15):
    """Keep the largest entries and combine the remainder without losing spend."""
    entries = sorted(groups.items(), key=lambda item: (-item[1][0], item[0]))
    total = sum((value[0] for _, value in entries), Decimal(0))
    if len(entries) > limit:
        tail = entries[limit - 1 :]
        entries = entries[: limit - 1] + [
            (
                'Other',
                (
                    sum((v[0] for _, v in tail), Decimal(0)),
                    sum(v[1] for _, v in tail)
                    if all(v[1] is not None for _, v in tail)
                    else None,
                ),
            )
        ]
    entries.sort(key=lambda item: (-item[1][0], item[0]))
    return [
        dict(
            name=name,
            cost=cost,
            count=count,
            share_pct=float(100 * cost / total) if total else 0,
        )
        for name, (cost, count) in entries
    ]


def add_group(groups, name, cost, count=1):
    if cost is None:
        return
    old_cost, old_count = groups.get(name, (Decimal(0), 0))
    groups[name] = (
        old_cost + cost,
        old_count + count if count is not None and old_count is not None else None,
    )


def amount(value, currency):
    value = value or {}
    return (
        number(value.get('amount')) if value.get('unit', currency) == currency else None
    )


def budget_row(project, budget, currency):
    if budget.get('is_missing'):
        return None
    limit = amount(budget.get('budget_limit'), currency)
    spent = amount(budget.get('actual_spend'), currency)
    forecast = amount(budget.get('forecasted_spend'), currency)
    if limit is None or spent is None or not budget.get('budget_name'):
        return None
    pct = float(100 * forecast / limit) if forecast is not None and limit > 0 else None
    return dict(
        project=project,
        budget_name=budget['budget_name'],
        limit=limit,
        spent=spent,
        forecast=forecast,
        pct_at_forecast=pct,
        headroom=limit - forecast if forecast is not None else None,
        status='over'
        if spent >= limit or (pct is not None and pct >= 100)
        else 'watch'
        if pct is not None and pct >= 85
        else 'ok',
    )


def project_name(source, data):
    raw = source.get('project')
    if isinstance(raw, dict):
        key = raw.get('project_id') or raw.get('name')
    else:
        key = source.get('project_id') or raw
    key = data.get('project_names', {}).get(key, key)
    return data.get('projects', {}).get(key, key) or 'Unassigned'


class InsightsService:
    def __init__(self, context, sources=None):
        self.context = context
        self.sources = sources or ReportingSources(context)
        self._cache = OrderedDict()
        self._lock = RLock()

    def get_insights(self, request, authorize, username=None):
        deadline = time.monotonic() + BUILD_SECONDS
        if not authorize():
            raise exceptions.unauthorized_access()
        zone = self.context.config().get_string('cluster.timezone', required=True)
        currency = locale.get_currency_code()
        now = datetime.now(timezone.utc)
        period = resolve_period(request, zone, now)
        projects = None
        if username is not None:
            projects = (
                self.context.projects.get_user_projects(
                    GetUserProjectsRequest(username=username)
                ).projects
                or []
            )
            projects = [p.model_dump(mode='json') for p in projects]
        membership = (
            tuple(sorted(p['project_id'] for p in projects))
            if projects is not None
            else ()
        )
        key = (username, membership, period.start_date, period.end_date, zone, currency)
        with self._lock:
            check_deadline(deadline)
            cached = self._cache.get(key)
            result = cached[1] if cached and time.monotonic() < cached[0] else None
        if result is not None:
            result = result.model_copy(deep=True)
        else:
            data = self.sources.read(period, deadline, username=username, insights=True)
            result = self.build(data, period, currency, zone, deadline, username)
            budget_projects = (
                projects if projects is not None else data.get('project_records', [])
            )
            result.budgets = self.budgets(budget_projects, currency, deadline)
            check_deadline(deadline)
            cached_result = result.model_copy(deep=True)
            with self._lock:
                check_deadline(deadline)
                self._cache[key] = (time.monotonic() + TTL_SECONDS, cached_result)
                self._cache.move_to_end(key)
                while len(self._cache) > 128:
                    self._cache.popitem(last=False)
        if not authorize():
            raise exceptions.unauthorized_access()
        return result

    def budgets(self, projects, currency, deadline):
        rows, fetched = [], {}
        for project in projects:
            check_deadline(deadline)
            reference = project.get('budget') or {}
            name = reference.get('budget_name')
            if not name or reference.get('is_missing'):
                continue
            if name not in fetched:
                try:
                    fetched[name] = (
                        self.context.aws_util()
                        .budgets_get_budget(budget_name=name)
                        .model_dump(mode='json')
                    )
                except exceptions.SocaException as error:
                    if error.error_code != 'BUDGET_NOT_FOUND':
                        raise
                    fetched[name] = {'is_missing': True}
            row = budget_row(
                project.get('title') or project.get('name') or project['project_id'],
                fetched[name],
                currency,
            )
            if row is not None:
                rows.append(row)
        rows.sort(
            key=lambda row: (
                -(row['pct_at_forecast'] if row['pct_at_forecast'] is not None else -1),
                row['project'],
            )
        )
        from ideadatamodel.reporting.insights import BudgetInsight

        return [BudgetInsight(**row) for row in rows]

    def build(self, data, period, currency, zone, deadline, username=None):
        result = ReportingInsights(
            period=dict(
                start=period.start_date,
                end=period.end_date,
                label=f'{date.fromisoformat(period.start_date):%b %d, %Y} to {date.fromisoformat(period.end_date):%b %d, %Y}',
            ),
            currency=currency,
            updated_at=datetime.now(timezone.utc),
        )
        jobs = result.jobs
        groups = {key: {} for key in ('user', 'project', 'queue', 'instance_family')}
        daily, seen = defaultdict(Decimal), set()
        totals = {
            key: []
            for key in (
                'cost',
                'savings',
                'cpu_efficiency_pct',
                'memory_efficiency_pct',
                'walltime_efficiency_pct',
                'wasted_core_hours',
                'wasted_cost',
            )
        }
        rows, weighted, weights = [], 0, 0
        start, end = timestamp(period.start), timestamp(period.end)
        for hit in data.get('jobs', []):
            check_deadline(deadline)
            job = hit.get('_source', {})
            identity = job.get('job_uid') or hit.get('_id')
            ended = timestamp(job.get('end_time'))
            if (
                not identity
                or identity in seen
                or not subject(job.get('owner'))
                or (username is not None and job['owner'] != username)
                or job.get('state') not in FINISHED
                or ended is None
                or not start <= ended < end
            ):
                continue
            seen.add(identity)
            jobs.count += 1
            efficiency = job_efficiency(job)
            bom = job.get('estimated_bom_cost') or {}
            cost = (
                amount(bom.get('total'), currency)
                if not bom.get('price_unavailable')
                else None
            )
            savings = (
                amount(bom.get('savings_total'), currency)
                if not bom.get('price_unavailable')
                else None
            )
            cpu = efficiency['cpu_efficiency_pct']
            if cpu is not None:
                jobs.jobs_with_efficiency += 1
                weighted += cpu * efficiency['core_hours']
                weights += efficiency['core_hours']
            values = dict(
                efficiency,
                cost=cost,
                savings=savings,
                wasted_cost=cost * (1 - Decimal(str(cpu)) / 100)
                if cost is not None and cpu is not None
                else None,
            )
            for key in totals:
                if values.get(key) is not None:
                    totals[key].append(values[key])
            instance = next(
                (
                    h['instance_type']
                    for h in job.get('execution_hosts') or []
                    if h.get('instance_type')
                ),
                None,
            )
            instance = instance or next(
                iter((job.get('params') or {}).get('instance_types') or []), None
            )
            project = project_name(job, data)
            for key, name in dict(
                user=user_label(job['owner']),
                project=project,
                queue=job.get('queue') or 'Unassigned',
                instance_family=instance.split('.')[0] if instance else 'Unknown',
            ).items():
                add_group(groups[key], name, cost)
            finished = datetime.fromtimestamp(ended, ZoneInfo(zone))
            if cost is not None:
                daily[(finished.date(), project)] += cost
            rows.append(
                dict(
                    job_id=str(job.get('job_id') or identity),
                    name=str(job['name']) if job.get('name') is not None else None,
                    owner=job['owner'],
                    project=project,
                    queue=job.get('queue'),
                    instance_type=instance,
                    finished_at=finished,
                    cost=cost,
                    **{
                        key: value
                        for key, value in efficiency.items()
                        if key != 'core_hours'
                    },
                )
            )
        for key, values in totals.items():
            if key.endswith('efficiency_pct'):
                setattr(jobs, key, sum(values) / len(values) if values else None)
            elif key == 'wasted_core_hours':
                jobs.wasted_core_hours = sum(values) if values else None
            else:
                setattr(jobs, key, known_sum(values))
        jobs.cpu_efficiency_weighted_pct = weighted / weights if weights else None
        for key, group in groups.items():
            setattr(
                jobs,
                f'by_{key}',
                ranked(group) if key != 'user' or username is None else [],
            )
        jobs.daily_by_project = [
            dict(date=day, project=project, cost=cost)
            for (day, project), cost in sorted(daily.items())
        ]
        jobs.costliest = sorted(
            (r for r in rows if r['cost'] is not None),
            key=lambda r: (-r['cost'], r['job_id']),
        )[:50]
        jobs.least_efficient = sorted(
            (r for r in rows if r['wasted_core_hours'] is not None),
            key=lambda r: (-r['wasted_core_hours'], r['job_id']),
        )[:25]
        self.desktops(result, data, period, currency, zone, deadline, username)
        self.storage(result, data, period, currency, zone, deadline, username)
        if any(value == 'unavailable' for value in data.get('coverage', {}).values()):
            result.notes.append('Some costs could not be read; try again later.')
        # Validate assigned rows as well as constructor fields.
        return ReportingInsights.model_validate(result.model_dump(warnings=False))

    def desktops(self, result, data, period, currency, zone, deadline, username):
        users, projects, daily = {}, {}, []
        for owner, projection in data.get('projections', {}).items():
            check_deadline(deadline)
            if username is not None and owner != username:
                continue
            costs = projection.get('costs') or {}
            if costs.get('currency') != currency or costs.get('timezone') != zone:
                continue
            points = {}
            for month in ('previous', 'current'):
                for point in ((costs.get(month) or {}).get('desktops') or {}).get(
                    'daily'
                ) or []:
                    points[point['date']] = point
            for day, point in points.items():
                cost = number(point.get('amount'))
                if (
                    period.start_date <= day <= period.end_date
                    and cost is not None
                    and point.get('status') not in ('unavailable', 'not_applicable')
                ):
                    add_group(users, user_label(owner), cost, None)
                    daily.append(dict(date=day, user=user_label(owner), cost=cost))
        result.desktops.cost = known_sum(v[0] for v in users.values())
        top = {
            name
            for name, _ in sorted(
                users.items(), key=lambda item: (-item[1][0], item[0])
            )[:10]
        }
        result.desktops.by_user = ranked(users) if username is None else []
        result.desktops.daily_top_users = sorted(
            (p for p in daily if p['user'] in top), key=lambda p: (p['date'], p['user'])
        )
        pricing = MyCostsService(self.context)
        pricing._ondemand_price = lru_cache(maxsize=128)(pricing._ondemand_price)
        hours = []
        for source in pricing._newest_per_session(data.get('desktops', [])):
            check_deadline(deadline)
            if not subject(source.get('owner')) or (
                username is not None and source.get('owner') != username
            ):
                continue
            source = dict(source)
            for key in ('created_on', 'updated_on', 'stopped_on', 'deleted_on'):
                source[key] = int((timestamp(source.get(key)) or 0) * 1000)
            if not source['created_on']:
                continue
            if source.get('deleted_on'):
                source['state'] = 'DELETED'
                source['updated_on'] = source['deleted_on']
            source.setdefault('server', dict(instance_type=source.get('instance_type')))
            session = pricing._desktop_session(
                source,
                int(timestamp(period.start) * 1000),
                int(timestamp(period.end) * 1000),
            )
            if session:
                hours.append(session.hours)
                cost = number(session.cost)
                cost = self.usd_amount(cost, currency)
                add_group(projects, project_name(source, data), cost)
        result.desktops.hours = sum(hours) if hours else None
        result.desktops.by_project = ranked(projects)

    def usd_amount(self, cost, currency):
        if cost is None or currency == 'USD':
            return cost
        rate = number(
            self.context.config().get_float('cluster.costs.usd_exchange_rate', None)
        )
        return cost * rate if rate else None

    def storage(self, result, data, period, currency, zone, deadline, username):
        latest, tiers, user_costs = {}, defaultdict(int), defaultdict(list)
        for row in data.get('storage', []):
            check_deadline(deadline)
            day = row.get('date', '')
            if not period.start_date <= day <= period.end_date:
                continue
            fs = row.get('filesystem_id')
            if not fs or not row.get('complete'):
                continue
            if fs not in latest or day > latest[fs]['date']:
                latest[fs] = row
            total = number(row.get('total_bytes'))
            rate = self.usd_amount(number(row.get('daily_rate')), currency)
            if total and rate is not None:
                for owner, value in (row.get('users') or {}).items():
                    used_bytes = number(value)
                    if (
                        subject(owner)
                        and used_bytes is not None
                        and used_bytes <= total
                        and (username is None or owner == username)
                    ):
                        user_costs[owner].append(rate * used_bytes / total)
            if username is None:
                for tier in ('ssd', 'capacity_pool'):
                    value = number(row.get(f'{tier}_bytes'))
                    if value is not None:
                        tiers[(day, tier)] += int(value)
        used = defaultdict(int)
        for row in latest.values():
            for owner, value in (row.get('users') or {}).items():
                value = number(value)
                if (
                    subject(owner)
                    and value is not None
                    and (username is None or owner == username)
                ):
                    used[owner] += int(value)
        for owner, projection in data.get('projections', {}).items():
            check_deadline(deadline)
            if username is not None and owner != username:
                continue
            cost, _ = selected_money(
                projection, 'shared_storage', period, currency, zone
            )
            if cost is not None and owner not in user_costs:
                user_costs[owner].append(cost)
        result.storage.cost = known_sum(
            cost for values in user_costs.values() for cost in values
        )
        result.storage.used_bytes = sum(used.values()) if used else None
        grouped_used, grouped_costs = defaultdict(int), defaultdict(list)
        for owner, value in used.items():
            grouped_used[user_label(owner)] += value
            grouped_costs[user_label(owner)].extend(user_costs[owner])
        result.storage.by_user = (
            [
                dict(name=name, bytes=value, cost=known_sum(grouped_costs[name]))
                for name, value in sorted(
                    grouped_used.items(), key=lambda item: (-item[1], item[0])
                )[:15]
            ]
            if username is None
            else []
        )
        if (
            username is None
            and latest
            and all(
                number(row.get('ssd_bytes')) is not None
                and number(row.get('capacity_pool_bytes')) is not None
                for row in latest.values()
            )
        ):
            result.storage.used_bytes = sum(
                int(number(row['ssd_bytes']) + number(row['capacity_pool_bytes']))
                for row in latest.values()
            )
        result.storage.tier_daily = [
            dict(date=day, tier=tier, bytes=value)
            for (day, tier), value in sorted(tiers.items())
        ]
