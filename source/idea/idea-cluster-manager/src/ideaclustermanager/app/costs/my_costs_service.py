"""
what one user cost the cluster over the trailing usage window, from what IDEA itself
recorded: attributed bedrock tokens, scheduler job cost estimates and indexed desktop
uptime. the only billed figure read here is the project's bedrock spend, by cost
allocation tag, and it is only ever used to apportion, never shown whole.
"""

__all__ = (
    'MyCostsService',
    'LIVE_SESSION_STATES',
    'RECENT_JOBS',
    'MAX_SESSIONS',
    'MAX_ADMIN_SESSIONS',
    'SESSION_FIELDS',
    'TERMINATED_DISPLAY_STATE',
    'TERMINATED_SESSION_STATES',
    'USER_BUCKETS',
)

from ideadatamodel import (
    constants,
    exceptions,
    locale,
    GetUserProjectsRequest,
    ProjectBedrockUsage,
    MyCostsAi,
    MyCostsAiModel,
    MyCostsAiProject,
    MyCostsDesktopSession,
    MyCostsDesktops,
    MyCostsJob,
    MyCostsJobGroup,
    MyCostsJobs,
    GetMyCostsSummaryResult,
    ListUserCostsResult,
    UserCosts,
)
from ideasdk.utils import Utils

from boto3.dynamodb.conditions import Key

from ideaclustermanager.app.projects.bedrock_usage_service import (
    USAGE_WINDOW,
    get_project_window_usage,
    list_bedrock_projects,
    usage_window_dates,
)

import arrow
from typing import Dict, List, Optional

# states in which the instance behind a desktop is still running and still costing
LIVE_SESSION_STATES = frozenset(
    {'PROVISIONING', 'CREATING', 'INITIALIZING', 'READY', 'RESUMING'}
)

# a deleted desktop still cost what it cost while it ran. the controller hard deletes
# the table row, so the session index is the only remaining record of it.
TERMINATED_SESSION_STATES = frozenset({'DELETING', 'DELETED'})

# "DELETED" is the controller's word for a row that is gone; a cost page reader wants
# to know the desktop was terminated.
TERMINATED_DISPLAY_STATE = 'Terminated'

# Known limitation: the administrator view scans the history table. a cluster that
# outgrows one row per terminated desktop wants an index on the deletion time.
HISTORY_SCAN_PAGES = 1000

RECENT_JOBS = 20
GROUP_BUCKETS = 50
# Known limitation: one bucket per user in the admin listing. a larger cluster loses the
# tail of the listing and wants a composite aggregation instead.
USER_BUCKETS = 1000
# Known limitation: one page of sessions; paginate the search if a user ever exceeds it.
MAX_SESSIONS = 200
# every desktop session in the window across every user. only the fields the hours are
# computed from are fetched, so a page this size stays small.
MAX_ADMIN_SESSIONS = 10000

# the fields a session's hours and price are computed from; without this the query
# returns whole session documents, none of the rest of which is read.
SESSION_FIELDS = [
    'idea_session_id',
    'name',
    'owner',
    'base_os',
    'state',
    'created_on',
    'updated_on',
    'stopped_on',
    'cleanup_warning_stop_time',
    'server.instance_type',
]

COST_FIELD = 'estimated_bom_cost.total.amount'
# the scheduler sets this when it could not price the instance hours. such a job still
# carries a real amount, so it cannot be detected from the amount being zero.
PRICE_UNAVAILABLE_FIELD = 'estimated_bom_cost.price_unavailable'
UNAVAILABLE_AGG = {'filter': {'term': {PRICE_UNAVAILABLE_FIELD: True}}}


def _round(value: float) -> float:
    return round(Utils.get_as_float(value, 0.0), 4)


class MyCostsService:
    def __init__(self, context):
        self.context = context
        self.logger = context.logger('my-costs')

    # opensearch

    def _os_client(self):
        analytics = self.context.analytics_service()
        if analytics is None:
            return None
        return analytics.os_client.os_client

    def _search(self, index: str, body: Dict) -> Optional[Dict]:
        os_client = self._os_client()
        if os_client is None:
            return None
        return os_client.search(index=index, body=body)

    # summary

    def get_summary(self, username: str) -> GetMyCostsSummaryResult:
        # get_username() is Optional, and an empty owner filter reads as every user
        # further down. there is no sensible all-users answer to "what did I cost".
        if Utils.is_empty(username):
            raise exceptions.unauthorized_access()

        start_date, end_date = usage_window_dates()
        window_start = arrow.get(start_date).floor('day')
        window_end = arrow.utcnow()

        return GetMyCostsSummaryResult(
            username=username,
            window=USAGE_WINDOW,
            start_date=start_date,
            end_date=end_date,
            currency=locale.get_currency_code(),
            ai=self._ai(username, start_date, end_date),
            jobs=self._jobs(username, window_start, window_end),
            desktops=self._desktops(username, window_start, window_end),
        )

    # ai

    def _ai(self, username: str, start_date: str, end_date: str) -> MyCostsAi:
        try:
            projects = self.context.projects.get_user_projects(
                GetUserProjectsRequest(username=username)
            ).projects
        except Exception as e:
            self.logger.warning(f'failed to list projects for {username}: {e}')
            return MyCostsAi(is_unavailable=True)

        rows: List[MyCostsAiProject] = []
        for project in Utils.get_as_list(projects, []):
            row = self._ai_project(username, project, start_date, end_date)
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda entry: entry.total_tokens, reverse=True)

        return MyCostsAi(
            invocations=sum(row.invocations for row in rows),
            total_tokens=sum(row.total_tokens for row in rows),
            cost=_round(sum(row.cost for row in rows)),
            estimated=True,
            projects=rows,
        )

    def _usage_rows(self, project_id: str, start_date: str, end_date: str):
        """
        the window's day rows for one project, read once: the same page answers both
        what the caller used and what the project used in total.
        """
        projects = self.context.projects
        if not projects.bedrock_provisioner.is_enabled():
            return None
        dao = projects.bedrock_usage_dao
        if dao.table is None:
            return None
        return dao.query_day_rows(project_id, start_date, end_date)

    @staticmethod
    def _total_tokens(rows) -> int:
        return sum(
            Utils.get_value_as_int('input_tokens', row, 0)
            + Utils.get_value_as_int('output_tokens', row, 0)
            for row in rows
        )

    def _project_spend(self, project_name: str):
        """
        the project's window spend on its own. not lifted off an unscoped usage read:
        that carries every user's totals, which do not belong on a per caller page.
        """
        scratch = ProjectBedrockUsage()
        self.context.projects.apply_bedrock_spend(scratch, project_name)
        return scratch.spend

    def _ai_project(
        self, username: str, project, start_date: str, end_date: str
    ) -> Optional[MyCostsAiProject]:
        try:
            rows = self._usage_rows(project.project_id, start_date, end_date)
            if rows is None:
                return None
            # the caller's own tokens, from the page already in hand. the aggregation
            # filters by username, so no other user's row or name reaches the response.
            mine = get_project_window_usage(
                self.context.projects.bedrock_usage_dao,
                project.project_id,
                start_date,
                end_date,
                username=username,
                rows=rows,
            )
        except Exception as e:
            self.logger.warning(
                f'failed to read bedrock usage for project {project.project_id}: {e}'
            )
            return None
        if mine is None or Utils.get_as_int(mine.total_tokens, 0) == 0:
            return None

        row = MyCostsAiProject(
            project_id=project.project_id,
            project_name=project.name,
            project_title=project.title,
            invocations=Utils.get_as_int(mine.invocations, 0),
            input_tokens=Utils.get_as_int(mine.input_tokens, 0),
            output_tokens=Utils.get_as_int(mine.output_tokens, 0),
            total_tokens=Utils.get_as_int(mine.total_tokens, 0),
            cost=0.0,
            estimated=True,
            by_model=[
                MyCostsAiModel(
                    model_id=model.model_id,
                    invocations=Utils.get_as_int(model.invocations, 0),
                    input_tokens=Utils.get_as_int(model.input_tokens, 0),
                    output_tokens=Utils.get_as_int(model.output_tokens, 0),
                    total_tokens=Utils.get_as_int(model.total_tokens, 0),
                    cost=0.0,
                    estimated=True,
                )
                for model in Utils.get_as_list(mine.by_model, [])
            ],
        )
        try:
            spend = self._project_spend(project.name)
        except Exception as e:
            self.logger.warning(
                f'failed to read project bedrock spend for {project.project_id}: {e}'
            )
            spend = None

        self._apportion(row, self._total_tokens(rows), spend)
        return row

    @staticmethod
    def _apportion(row: MyCostsAiProject, project_tokens: int, spend):
        """
        the project's bedrock spend over the same window, split by the caller's token
        share and then by their share per model. a spend that cannot be read is marked
        unavailable rather than priced at zero: "no charge" must not follow a failed call.
        """
        if spend is None or project_tokens <= 0:
            row.cost_unavailable = True
            row.cost = 0.0
            for model in row.by_model:
                model.cost = None
            return

        share = row.total_tokens / project_tokens
        row.cost = _round(Utils.get_as_float(spend.amount, 0.0) * share)
        for model in row.by_model:
            model.cost = _round(
                Utils.get_as_float(spend.amount, 0.0)
                * (model.total_tokens / project_tokens)
            )

    # jobs

    def _jobs_index(self) -> Optional[str]:
        config = self.context.config()
        if not config.is_module_enabled(constants.MODULE_SCHEDULER):
            return None
        module_id = config.get_module_id(constants.MODULE_SCHEDULER)
        return f'{self.context.cluster_name()}_{module_id}_jobs'

    def _jobs(self, username: str, window_start, window_end) -> MyCostsJobs:
        """
        completed jobs the caller owns, from the index the scheduler writes. the per
        project and per queue totals are aggregated in opensearch so they cover every
        job in the window, not just the recent ones listed.
        """
        try:
            index = self._jobs_index()
            if index is None:
                return MyCostsJobs(is_unavailable=True)
            group = {
                'terms': {'field': 'project.raw', 'size': GROUP_BUCKETS},
                'aggs': {'cost': {'sum': {'field': COST_FIELD}}},
            }
            response = self._search(
                index,
                {
                    'size': RECENT_JOBS,
                    'track_total_hits': True,
                    'sort': [{'end_time': {'order': 'desc'}}],
                    '_source': [
                        'job_id',
                        'name',
                        'queue',
                        'project',
                        'end_time',
                        'estimated_bom_cost',
                    ],
                    'query': {
                        'bool': {
                            'filter': [
                                {'term': {'owner.raw': username}},
                                {
                                    'range': {
                                        'end_time': {
                                            'gte': window_start.isoformat(),
                                            'lte': window_end.isoformat(),
                                        }
                                    }
                                },
                            ]
                        }
                    },
                    'aggs': {
                        'cost': {'sum': {'field': COST_FIELD}},
                        # jobs that carry an estimate. a sum over a missing field is
                        # zero, so an unpriced job would otherwise look like a free one.
                        'priced': {'value_count': {'field': COST_FIELD}},
                        # jobs whose estimate is missing its compute. they carry an
                        # amount, so 'priced' counts them and only this finds them.
                        'unavailable': UNAVAILABLE_AGG,
                        'by_project': group,
                        'by_queue': {
                            'terms': {'field': 'queue.raw', 'size': GROUP_BUCKETS},
                            'aggs': {'cost': {'sum': {'field': COST_FIELD}}},
                        },
                    },
                },
            )
        except Exception as e:
            self.logger.warning(f'failed to read jobs for {username}: {e}')
            return MyCostsJobs(is_unavailable=True)

        if response is None:
            return MyCostsJobs(is_unavailable=True)

        hits = Utils.get_value_as_dict('hits', response, {})
        aggregations = Utils.get_value_as_dict('aggregations', response, {})
        job_count = Utils.get_value_as_int(
            'value', Utils.get_value_as_dict('total', hits, {}), 0
        )
        priced = Utils.get_value_as_int(
            'value', Utils.get_value_as_dict('priced', aggregations, {}), 0
        )
        unavailable = Utils.get_value_as_int(
            'doc_count', Utils.get_value_as_dict('unavailable', aggregations, {}), 0
        )
        return MyCostsJobs(
            job_count=job_count,
            cost=_round(
                Utils.get_value_as_float(
                    'value', Utils.get_value_as_dict('cost', aggregations, {}), 0.0
                )
            ),
            unpriced_jobs=max(job_count - priced, 0),
            cost_unavailable=unavailable > 0,
            estimated=True,
            by_project=self._job_groups(aggregations, 'by_project'),
            by_queue=self._job_groups(aggregations, 'by_queue'),
            recent_jobs=[
                self._job(Utils.get_value_as_dict('_source', hit, {}))
                for hit in Utils.get_value_as_list('hits', hits, [])
            ],
        )

    @staticmethod
    def _job_groups(aggregations: Dict, key: str) -> List[MyCostsJobGroup]:
        buckets = Utils.get_value_as_list(
            'buckets', Utils.get_value_as_dict(key, aggregations, {}), []
        )
        return [
            MyCostsJobGroup(
                name=Utils.get_value_as_string('key', bucket),
                job_count=Utils.get_value_as_int('doc_count', bucket, 0),
                cost=_round(
                    Utils.get_value_as_float(
                        'value', Utils.get_value_as_dict('cost', bucket, {}), 0.0
                    )
                ),
            )
            for bucket in buckets
        ]

    @staticmethod
    def _job(source: Dict) -> MyCostsJob:
        bom = Utils.get_value_as_dict('estimated_bom_cost', source, {})
        total = Utils.get_value_as_dict('total', bom, {})
        return MyCostsJob(
            job_id=Utils.get_value_as_string('job_id', source),
            name=Utils.get_value_as_string('name', source),
            queue=Utils.get_value_as_string('queue', source),
            project=Utils.get_value_as_string('project', source),
            end_time=Utils.get_value_as_string('end_time', source),
            cost=_round(Utils.get_value_as_float('amount', total, 0.0)),
            # only from the flag: a job cancelled before it ran was priced normally,
            # and its zero is the real answer.
            cost_unavailable=Utils.get_value_as_bool('price_unavailable', bom, False),
        )

    # desktops

    def _desktop_hits(
        self,
        username: Optional[str],
        start_ms: int,
        end_ms: int,
        size: int = None,
        all_users: bool = False,
    ):
        """
        raw session documents overlapping the window. the admin listing asks for every
        user by saying so; a missing username is a bug, never a wildcard. no state is
        filtered: a desktop deleted after running in the window still cost what it cost.
        """
        if not all_users and Utils.is_empty(username):
            raise exceptions.unauthorized_access()
        index = self.context.config().get_string(
            'virtual-desktop-controller.opensearch.dcv_session.alias'
        )
        if Utils.is_empty(index):
            return None
        owner = [] if all_users else [{'term': {'owner.raw': username}}]
        response = self._search(
            index,
            {
                'size': size if size is not None else MAX_SESSIONS,
                '_source': SESSION_FIELDS,
                'sort': [{'created_on': {'order': 'desc'}}],
                'query': {
                    'bool': {
                        'filter': owner
                        + [
                            {'range': {'created_on': {'lte': end_ms}}},
                            {'range': {'updated_on': {'gte': start_ms}}},
                        ]
                    }
                },
            },
        )
        if response is None:
            return None
        return Utils.get_value_as_list(
            'hits', Utils.get_value_as_dict('hits', response, {}), []
        )

    @staticmethod
    def _desktop_totals(sessions: List[MyCostsDesktopSession]) -> MyCostsDesktops:
        """
        cost is the subtotal of the sessions that had a price. unpriced_sessions says
        how many are missing from it, so a partial subtotal is never read as the whole.
        """
        return MyCostsDesktops(
            session_count=len(sessions),
            hours=_round(sum(session.hours for session in sessions)),
            cost=_round(
                sum(Utils.get_as_float(session.cost, 0.0) for session in sessions)
            ),
            unpriced_sessions=sum(
                1 for session in sessions if session.price_unavailable
            ),
            estimated=True,
            sessions=sessions,
        )

    def _desktops(self, username: str, window_start, window_end) -> MyCostsDesktops:
        """
        the caller's desktop sessions overlapping the window, priced at the recorded
        hours times the on-demand rate. an unpriced instance type reports hours only.
        """
        start_ms = window_start.int_timestamp * 1000
        end_ms = window_end.int_timestamp * 1000
        try:
            hits = self._desktop_hits(username, start_ms, end_ms)
        except Exception as e:
            self.logger.warning(f'failed to read desktop sessions for {username}: {e}')
            return MyCostsDesktops(is_unavailable=True)

        if hits is None:
            return MyCostsDesktops(is_unavailable=True)

        hits = list(hits) + self._history_hits(username, start_ms, end_ms)
        return self._desktop_totals(self._sessions_from(hits, start_ms, end_ms))

    def _history_table(self):
        """
        the controller's record of terminated desktops, absent until the controller
        writes one, which is not an error.
        """
        config = self.context.config()
        if not config.is_module_enabled(constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER):
            return None
        module_id = config.get_module_id(constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)
        name = f'{self.context.cluster_name()}.{module_id}.controller.session-history'
        return self.context.aws().dynamodb_table().Table(name)

    def _history_hits(
        self,
        username: Optional[str],
        start_ms: int,
        end_ms: int,
        all_users: bool = False,
    ) -> List[Dict]:
        """
        desktops that ran in the window and have since been deleted; this record is the
        only evidence left of them. shaped like a search hit so one builder serves both.
        """
        if not all_users and Utils.is_empty(username):
            raise exceptions.unauthorized_access()
        try:
            table = self._history_table()
            if table is None:
                return []
            if all_users:
                rows = self._scan_history(table)
            else:
                rows = Utils.get_value_as_list(
                    'Items',
                    table.query(KeyConditionExpression=Key('owner').eq(username)),
                    [],
                )
        except Exception as e:
            # additive: a history read that fails leaves the live sessions intact
            self.logger.warning(f'failed to read desktop session history: {e}')
            return []

        hits = []
        for row in rows:
            created_on = Utils.get_value_as_int('created_on', row, 0)
            deleted_on = Utils.get_value_as_int('deleted_on', row, 0)
            # the desktop has to have existed during the window to have cost anything
            if deleted_on < start_ms or (created_on > 0 and created_on > end_ms):
                continue
            hits.append(
                {
                    '_history': True,
                    '_source': {
                        'idea_session_id': Utils.get_value_as_string(
                            'idea_session_id', row
                        ),
                        'name': Utils.get_value_as_string('name', row),
                        'owner': Utils.get_value_as_string('owner', row),
                        'base_os': Utils.get_value_as_string('base_os', row),
                        # the record only exists because the desktop was terminated
                        'state': 'DELETED',
                        'created_on': created_on,
                        'updated_on': deleted_on,
                        'stopped_on': Utils.get_value_as_int('stopped_on', row, 0),
                        'server': {
                            'instance_type': Utils.get_value_as_string(
                                'instance_type', row
                            )
                        },
                    },
                }
            )
        return hits

    @staticmethod
    def _scan_history(table) -> List[Dict]:
        rows: List[Dict] = []
        last_key = None
        for _ in range(HISTORY_SCAN_PAGES):
            kwargs = {} if last_key is None else {'ExclusiveStartKey': last_key}
            result = table.scan(**kwargs)
            rows.extend(Utils.get_value_as_list('Items', result, []))
            last_key = Utils.get_value_as_dict('LastEvaluatedKey', result, None)
            if last_key is None:
                break
        return rows

    @staticmethod
    def _newest_per_session(hits) -> List[Dict]:
        """
        one document per desktop, the most recently written.

        the alias spans every generation of the versioned index, so a reindexed desktop
        has a document under more than one and counting both would bill it twice. a
        history record always wins: it is written at the deletion and is complete.
        """
        newest: Dict[str, Dict] = {}
        from_history: Dict[str, bool] = {}
        for hit in hits:
            source = Utils.get_value_as_dict('_source', hit, {})
            session_id = Utils.get_value_as_string('idea_session_id', source, '')
            if Utils.is_empty(session_id):
                continue
            is_history = Utils.get_value_as_bool('_history', hit, False)
            seen = newest.get(session_id)
            if seen is None:
                newest[session_id] = source
                from_history[session_id] = is_history
                continue
            if from_history.get(session_id) and not is_history:
                continue
            if is_history and not from_history.get(session_id):
                newest[session_id] = source
                from_history[session_id] = True
                continue
            if Utils.get_value_as_int('updated_on', source, 0) >= (
                Utils.get_value_as_int('updated_on', seen, 0)
            ):
                newest[session_id] = source
                from_history[session_id] = is_history
        return list(newest.values())

    def _sessions_from(
        self, hits, start_ms: int, end_ms: int
    ) -> List[MyCostsDesktopSession]:
        sessions = [
            self._desktop_session(source, start_ms, end_ms)
            for source in self._newest_per_session(hits)
        ]
        return [session for session in sessions if session is not None]

    def _desktop_session(
        self, source: Dict, start_ms: int, end_ms: int
    ) -> Optional[MyCostsDesktopSession]:
        created_on = Utils.get_value_as_int('created_on', source, 0)
        updated_on = Utils.get_value_as_int('updated_on', source, 0)
        state = Utils.get_value_as_string('state', source, '')

        stopped_at, stop_time_estimated = self._stop_time(
            source, state, updated_on, end_ms
        )
        started = max(created_on, start_ms)
        stopped = min(stopped_at, end_ms)
        if stopped <= started:
            return None

        hours = (stopped - started) / 3600000.0
        server = Utils.get_value_as_dict('server', source, {})
        instance_type = Utils.get_value_as_string('instance_type', server)

        session = MyCostsDesktopSession(
            idea_session_id=Utils.get_value_as_string('idea_session_id', source),
            name=Utils.get_value_as_string('name', source),
            instance_type=instance_type,
            base_os=Utils.get_value_as_string('base_os', source),
            state=TERMINATED_DISPLAY_STATE
            if state in TERMINATED_SESSION_STATES
            else state,
            started_on=arrow.get(started / 1000).isoformat(),
            ended_on=arrow.get(stopped / 1000).isoformat(),
            hours=_round(hours),
            estimated=True,
        )
        if stop_time_estimated:
            session.stop_time_estimated = True

        price = self._ondemand_price(instance_type)
        if price is None:
            session.price_unavailable = True
        else:
            session.cost = _round(hours * price)
        return session

    # admin listing

    def list_user_costs(self) -> ListUserCostsResult:
        """
        one row per user with any measured cost in the window, across the same three
        sources the per user summary reads. an unreadable source is reported as
        unavailable rather than leaving rows silently short.
        """
        start_date, end_date = usage_window_dates()
        window_start = arrow.get(start_date).floor('day')
        window_end = arrow.utcnow()
        start_ms = window_start.int_timestamp * 1000
        end_ms = window_end.int_timestamp * 1000

        rows: Dict[str, UserCosts] = {}

        def row_for(username: str) -> UserCosts:
            return rows.setdefault(
                username,
                UserCosts(
                    username=username,
                    ai_requests=0,
                    ai_tokens=0,
                    ai_cost=0.0,
                    desktop_session_count=0,
                    desktop_hours=0.0,
                    desktop_cost=0.0,
                    desktop_unpriced_sessions=0,
                    job_count=0,
                    job_cost=0.0,
                    job_unpriced_jobs=0,
                    total_cost=0.0,
                ),
            )

        ai_unavailable = self._ai_by_user(row_for)
        jobs_unavailable = self._jobs_by_user(row_for, window_start, window_end)
        desktops_unavailable = self._desktops_by_user(row_for, start_ms, end_ms)

        listing = list(rows.values())
        for row in listing:
            row.ai_cost = _round(row.ai_cost)
            row.desktop_cost = _round(row.desktop_cost)
            row.job_cost = _round(row.job_cost)
            row.total_cost = _round(row.ai_cost + row.desktop_cost + row.job_cost)
        listing.sort(key=lambda entry: entry.total_cost, reverse=True)

        return ListUserCostsResult(
            window=USAGE_WINDOW,
            start_date=start_date,
            end_date=end_date,
            currency=locale.get_currency_code(),
            listing=listing,
            ai_unavailable=ai_unavailable,
            jobs_unavailable=jobs_unavailable,
            desktops_unavailable=desktops_unavailable,
        )

    def _ai_by_user(self, row_for) -> bool:
        try:
            projects = list_bedrock_projects(self.context.projects.projects_dao)
        except Exception as e:
            self.logger.warning(f'failed to list bedrock projects: {e}')
            return True

        for project in projects:
            try:
                # unscoped read: the per project total plus its by_user split, the
                # same call the projects page makes. Known limitation: by_user is
                # capped at max_users_per_project, so a larger project loses its tail.
                usage = self.context.projects.get_project_bedrock_usage(
                    project_id=project.project_id, project_name=project.name
                )
            except Exception as e:
                self.logger.warning(
                    f'failed to read bedrock usage for project {project.project_id}: {e}'
                )
                continue
            if usage is None:
                continue

            project_tokens = Utils.get_as_int(usage.total_tokens, 0)
            spend = usage.spend
            for entry in Utils.get_as_list(usage.by_user, []):
                row = row_for(entry.username)
                row.ai_requests += Utils.get_as_int(entry.invocations, 0)
                row.ai_tokens += Utils.get_as_int(entry.total_tokens, 0)
                if spend is None or project_tokens <= 0:
                    row.ai_cost_unavailable = True
                    continue
                row.ai_cost += (
                    Utils.get_as_float(spend.amount, 0.0)
                    * Utils.get_as_int(entry.total_tokens, 0)
                    / project_tokens
                )
        return False

    def _jobs_by_user(self, row_for, window_start, window_end) -> bool:
        try:
            index = self._jobs_index()
            if index is None:
                return True
            response = self._search(
                index,
                {
                    'size': 0,
                    'query': {
                        'bool': {
                            'filter': [
                                {
                                    'range': {
                                        'end_time': {
                                            'gte': window_start.isoformat(),
                                            'lte': window_end.isoformat(),
                                        }
                                    }
                                }
                            ]
                        }
                    },
                    'aggs': {
                        'by_user': {
                            'terms': {'field': 'owner.raw', 'size': USER_BUCKETS},
                            'aggs': {
                                'cost': {'sum': {'field': COST_FIELD}},
                                'priced': {'value_count': {'field': COST_FIELD}},
                                'unavailable': UNAVAILABLE_AGG,
                            },
                        }
                    },
                },
            )
        except Exception as e:
            self.logger.warning(f'failed to aggregate jobs by user: {e}')
            return True

        if response is None:
            return True

        buckets = Utils.get_value_as_list(
            'buckets',
            Utils.get_value_as_dict(
                'by_user', Utils.get_value_as_dict('aggregations', response, {}), {}
            ),
            [],
        )
        for bucket in buckets:
            username = Utils.get_value_as_string('key', bucket)
            if Utils.is_empty(username):
                continue
            row = row_for(username)
            count = Utils.get_value_as_int('doc_count', bucket, 0)
            priced = Utils.get_value_as_int(
                'value', Utils.get_value_as_dict('priced', bucket, {}), 0
            )
            row.job_count += count
            row.job_unpriced_jobs += max(count - priced, 0)
            row.job_cost += Utils.get_value_as_float(
                'value', Utils.get_value_as_dict('cost', bucket, {}), 0.0
            )
            if (
                Utils.get_value_as_int(
                    'doc_count', Utils.get_value_as_dict('unavailable', bucket, {}), 0
                )
                > 0
            ):
                row.job_cost_unavailable = True
        return False

    def _desktops_by_user(self, row_for, start_ms: int, end_ms: int) -> bool:
        try:
            hits = self._desktop_hits(
                None, start_ms, end_ms, size=MAX_ADMIN_SESSIONS, all_users=True
            )
        except Exception as e:
            self.logger.warning(f'failed to read desktop sessions for all users: {e}')
            return True

        if hits is None:
            return True

        hits = list(hits) + self._history_hits(None, start_ms, end_ms, all_users=True)

        by_owner: Dict[str, List] = {}
        for hit in hits:
            source = Utils.get_value_as_dict('_source', hit, {})
            owner = Utils.get_value_as_string('owner', source, '')
            if Utils.is_empty(owner):
                continue
            by_owner.setdefault(owner, []).append(hit)

        for owner, owner_hits in by_owner.items():
            totals = self._desktop_totals(
                self._sessions_from(owner_hits, start_ms, end_ms)
            )
            row = row_for(owner)
            row.desktop_session_count += Utils.get_as_int(totals.session_count, 0)
            row.desktop_hours += Utils.get_as_float(totals.hours, 0.0)
            row.desktop_cost += Utils.get_as_float(totals.cost, 0.0)
            row.desktop_unpriced_sessions += Utils.get_as_int(
                totals.unpriced_sessions, 0
            )
        return False

    @staticmethod
    def _stop_time(source: Dict, state: str, updated_on: int, end_ms: int):
        """
        when the desktop stopped costing, and whether that had to be inferred.

        a live desktop bills to the end of the window; a stopped or terminated one to
        the recorded stop time. updated_on is a last resort because it moves on any
        write, so a desktop stopped on day one and renamed on day twenty five would
        otherwise bill the gap. it is reported as an estimate whenever it is used.
        """
        if state in LIVE_SESSION_STATES:
            return end_ms, False

        stopped_on = Utils.get_value_as_int('stopped_on', source, 0)
        if stopped_on > 0:
            return stopped_on, False

        # older sessions stopped before the stop time was recorded. the cleanup notice
        # carries a real ec2 stop time for some of them; either way it is a guess.
        cleanup_stop = Utils.get_value_as_int('cleanup_warning_stop_time', source, 0)
        if cleanup_stop > 0 and updated_on > 0:
            return min(cleanup_stop, updated_on), True
        if cleanup_stop > 0:
            return cleanup_stop, True
        return updated_on, True

    def _ondemand_price(self, instance_type: str) -> Optional[float]:
        if Utils.is_empty(instance_type):
            return None
        try:
            unit_price = self.context.aws_util().get_ec2_instance_type_unit_price(
                instance_type
            )
        except Exception as e:
            self.logger.warning(f'failed to price instance type {instance_type}: {e}')
            return None
        # no answer at all outside the commercial partition, or when the lookup failed.
        if unit_price is None:
            return None
        ondemand = Utils.get_as_float(getattr(unit_price, 'ondemand', None), 0.0)
        if ondemand <= 0:
            return None
        return ondemand
