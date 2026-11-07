"""Read recorded projections and activity without refreshing any source."""

import json
import logging
import time
from datetime import datetime
from decimal import Decimal

from concurrent.futures import ThreadPoolExecutor

from boto3.dynamodb.conditions import Attr, Key

from ideadatamodel import (
    ListUsersRequest,
    ListProjectsRequest,
    SocaPaginator,
    constants,
    exceptions,
)
from .snapshot_store import check_deadline


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (ValueError, ArithmeticError):
        return None


def timestamp(value):
    try:
        if isinstance(value, (int, float, Decimal)):
            return float(value) / 1000 if value > 0 else None
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.timestamp() if result.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def user_label(name):
    return (
        'System'
        if name == 'root' or name.startswith('uid:') or name.isdecimal()
        else name
    )


def subject(value):
    return isinstance(value, str) and bool(value) and not value.startswith('!')


def source_not_created(error: Exception) -> bool:
    """a DynamoDB table or OpenSearch index that does not exist yet"""
    code = getattr(error, 'response', {}).get('Error', {}).get('Code')
    if code == 'ResourceNotFoundException':
        return True
    return getattr(error, 'error', None) == 'index_not_found_exception' or (
        'index_not_found_exception' in str(error)
    )


class ReportingSources:
    def __init__(self, context):
        self.context = context

    @staticmethod
    def scan(table, deadline, owner=None):
        request = {'ConsistentRead': True}
        if owner is not None:
            request['KeyConditionExpression'] = Key('owner').eq(owner)
        read = table.scan if owner is None else table.query
        while True:
            check_deadline(deadline)
            page = read(**request)
            yield from page.get('Items', [])
            if not page.get('LastEvaluatedKey'):
                break
            request['ExclusiveStartKey'] = page['LastEvaluatedKey']
        check_deadline(deadline)

    @staticmethod
    def scan_heads(table, deadline, segments=8):
        """
        every head record, read in parallel segments. the table is mostly generation and
        inventory chunks, and one sequential pass over it was most of a report build.
        """

        def segment(index):
            request = dict(
                ConsistentRead=True,
                Segment=index,
                TotalSegments=segments,
                FilterExpression=Attr('record').eq('head'),
            )
            rows = []
            while True:
                check_deadline(deadline)
                page = table.scan(**request)
                rows.extend(page.get('Items', []))
                if not page.get('LastEvaluatedKey'):
                    return rows
                request['ExclusiveStartKey'] = page['LastEvaluatedKey']

        with ThreadPoolExecutor(max_workers=segments) as pool:
            return [row for rows in pool.map(segment, range(segments)) for row in rows]

    def read_heads(self, usernames, deadline):
        store = self.context.personal_costs_store
        subjects = store.head_subjects()
        if subjects is None:
            # before the collector's first run on this version
            return self.scan_heads(store.table, deadline)
        return store.batch_heads(
            set(subjects) | set(usernames), lambda: check_deadline(deadline)
        )

    @staticmethod
    def listing(read, request_type, deadline):
        cursor = None
        while True:
            check_deadline(deadline)
            page = read(request_type(paginator=SocaPaginator(cursor=cursor)))
            for item in page.listing or []:
                yield item.model_dump(mode='json')
            cursor = page.paginator.cursor if page.paginator else None
            if not cursor:
                break
        check_deadline(deadline)

    def search(self, index, query, deadline, fields=None):
        client = self.context.analytics_service().os_client.os_client
        scroll_id = None
        try:
            check_deadline(deadline)
            page = client.search(
                index=index,
                scroll='1m',
                request_timeout=max(0.1, deadline - time.monotonic()),
                body={
                    'size': 1000,
                    'sort': ['_doc'],
                    'query': query,
                    **({'_source': fields} if fields is not None else {}),
                },
            )
            while True:
                scroll_id = page.get('_scroll_id', scroll_id)
                if page.get('timed_out') or page.get('_shards', {}).get('failed', 0):
                    raise ValueError('Incomplete index read')
                hits = page.get('hits', {}).get('hits', [])
                if not hits:
                    break
                yield from hits
                check_deadline(deadline)
                if not scroll_id:
                    raise ValueError('Missing consistent search context')
                page = client.scroll(
                    scroll_id=scroll_id,
                    scroll='1m',
                    request_timeout=max(0.1, deadline - time.monotonic()),
                )
        finally:
            if scroll_id:
                try:
                    client.clear_scroll(scroll_id=scroll_id, request_timeout=1)
                except Exception:
                    pass
        check_deadline(deadline)

    @staticmethod
    def desktop_period_query(period, scope_username):
        """
        session documents that can matter to the period. a document is left out only when
        it says it was created after the period, or stopped before it on a date it gave.
        whatever cannot say so stays in: no date at all is counted with unknown hours,
        and a live desktop runs on from a last write of any age.
        """
        from ideaclustermanager.app.costs.my_costs_service import LIVE_SESSION_STATES

        start_ms = int(timestamp(period.start) * 1000)
        end_ms = int(timestamp(period.end) * 1000)

        def absent(field):
            return {'bool': {'must_not': [{'exists': {'field': field}}]}}

        return {
            'bool': {
                'filter': (
                    [{'term': {'owner.raw': scope_username}}]
                    if scope_username is not None
                    else []
                )
                + [
                    {
                        'bool': {
                            'minimum_should_match': 1,
                            'should': [
                                absent('created_on'),
                                {'range': {'created_on': {'lt': end_ms}}},
                            ],
                        }
                    },
                    {
                        'bool': {
                            'minimum_should_match': 1,
                            'should': [
                                absent('updated_on'),
                                {'range': {'updated_on': {'lte': 0}}},
                                *(
                                    {'range': {field: {'gte': start_ms}}}
                                    for field in (
                                        'updated_on',
                                        'stopped_on',
                                        'cleanup_warning_stop_time',
                                        'deleted_on',
                                    )
                                ),
                                {'terms': {'state.raw': sorted(LIVE_SESSION_STATES)}},
                            ],
                        }
                    },
                ]
            }
        }

    def whole_sessions(self, index, hits, runs, activity, period, deadline, scope):
        """
        every document of the sessions the period filter cannot judge from one document.
        a stale generation of the index can still say READY for a desktop a newer document
        says stopped before the period, so a session kept on an old write needs all of its
        documents for the newest to win. the same for a desktop whose run overlaps the
        period, or that the activity checks saw, whatever its documents said.
        """
        start = timestamp(period.start)
        end = timestamp(period.end)
        wanted = set(activity)
        for hit in hits:
            source = hit.get('_source', {})
            updated = timestamp(source.get('updated_on'))
            if updated is None or updated < start:
                wanted.add(source.get('idea_session_id'))
        for row in runs:
            started = timestamp(row.get('started_on'))
            stopped = timestamp(row.get('stopped_on'))
            if (
                started is not None
                and started < end
                and (stopped is None or stopped >= start)
            ):
                wanted.add(row.get('run_of'))
        wanted = sorted(name for name in wanted if name)
        seen = {(hit.get('_index'), hit.get('_id')) for hit in hits}
        found = []
        for offset in range(0, len(wanted), 1000):
            for hit in self.search(
                index,
                {
                    'bool': {
                        'filter': (
                            [{'term': {'owner.raw': scope}}]
                            if scope is not None
                            else []
                        )
                        + [
                            {
                                'terms': {
                                    'idea_session_id.raw': wanted[
                                        offset : offset + 1000
                                    ]
                                }
                            }
                        ]
                    }
                },
                deadline,
            ):
                if (hit.get('_index'), hit.get('_id')) not in seen:
                    seen.add((hit.get('_index'), hit.get('_id')))
                    found.append(hit)
        return found

    def desktop_shapes(self, index, deadline):
        """
        each owner and project on any session document, without the documents: the report
        keeps a row for every one of them, with or without activity in its period.
        """
        client = self.context.analytics_service().os_client.os_client
        sources = [
            {name: {'terms': {'field': field, 'missing_bucket': True}}}
            for name, field in (
                ('owner', 'owner.raw'),
                ('project_id', 'project.project_id.raw'),
                ('name', 'project.name.raw'),
            )
        ]
        shapes, after = [], None
        while True:
            check_deadline(deadline)
            composite = dict(size=1000, sources=sources)
            if after:
                composite['after'] = after
            page = client.search(
                index=index,
                request_timeout=max(0.1, deadline - time.monotonic()),
                body={'size': 0, 'aggs': {'shapes': {'composite': composite}}},
            )
            if page.get('timed_out') or page.get('_shards', {}).get('failed', 0):
                raise ValueError('Incomplete index read')
            shapes_page = page.get('aggregations', {}).get('shapes', {})
            shapes.extend(bucket['key'] for bucket in shapes_page.get('buckets', []))
            after = shapes_page.get('after_key')
            if not after or not shapes_page.get('buckets'):
                return shapes

    def projection(self, username, head, deadline):
        store = self.context.personal_costs_store
        for attempt in range(3):
            check_deadline(deadline)
            try:
                chunks = []
                for index in range(head['parts']['costs']):
                    check_deadline(deadline)
                    chunks.append(
                        store.get(username, f'g:{head["generation"]}:costs:{index}')
                    )
                costs = json.loads(''.join(chunks), parse_float=Decimal)
                check_deadline(deadline)
                return dict(
                    head=head,
                    costs=costs,
                    state='stale' if costs.get('state') == 'stale' else 'ready',
                )
            except (TypeError, KeyError, ValueError):
                if attempt == 2:
                    break
                head = store.get(username, 'head')
                if not head:
                    break
        return dict(head=head, costs=None, state='unavailable')

    def add_desktop_shapes(self, result, index, deadline):
        """these only add empty rows: losing them loses no desktop that was read"""
        try:
            result['desktop_shapes'] = self.desktop_shapes(index, deadline)
        except exceptions.SocaException as error:
            if error.error_code in ('REPORT_TIMEOUT', 'REPORT_TOO_LARGE'):
                raise
            result['desktop_shapes'] = []
            result['warnings'].append('Desktop shapes unavailable.')
        except Exception:
            logging.getLogger(__name__).warning(
                'desktop shapes unavailable', exc_info=True
            )
            result['desktop_shapes'] = []
            result['warnings'].append('Desktop shapes unavailable.')

    def read(
        self, period, deadline, username=None, insights=False, include_storage=False
    ):
        scope_username = username
        result = dict(
            users={},
            projects={},
            projections={},
            jobs=[],
            desktops=[],
            coverage={},
            warnings=[],
        )

        def optional(name, read):
            try:
                value = read()
                result['coverage'][name] = 'ready'
                return value
            except exceptions.SocaException as error:
                if error.error_code in ('REPORT_TIMEOUT', 'REPORT_TOO_LARGE'):
                    raise
                # the report shows the source as missing; the log says why
                logging.getLogger(__name__).warning(
                    'report source %s unavailable: %s', name, error
                )
                result['coverage'][name] = 'unavailable'
                result['warnings'].append(f'{name} source unavailable.')
                return []
            except Exception as error:
                if source_not_created(error):
                    # a new cluster has no desktop history table or session index until
                    # its first desktop: that is no data, not an unreadable source
                    result['coverage'][name] = 'ready'
                    return []
                logging.getLogger(__name__).warning(
                    'report source %s unavailable', name, exc_info=True
                )
                result['coverage'][name] = 'unavailable'
                result['warnings'].append(f'{name} source unavailable.')
                return []

        users = optional(
            'accounts',
            lambda: list(
                self.listing(
                    self.context.accounts.list_users, ListUsersRequest, deadline
                )
            ),
        )
        if username is not None:
            users = [row for row in users if row.get('username') == username]
        result['users'] = {
            row['username']: row.get('username')
            for row in users
            if subject(row.get('username'))
        }
        projects = optional(
            'projects',
            lambda: list(
                self.listing(
                    self.context.projects.list_projects, ListProjectsRequest, deadline
                )
            ),
        )
        result['project_records'] = projects
        result['projects'] = {
            row['project_id']: row.get('title') or row.get('name') or row['project_id']
            for row in projects
        }
        result['project_names'] = {
            row['name']: row['project_id'] for row in projects if row.get('name')
        }
        heads = optional(
            'projections',
            lambda: [
                row
                for row in (
                    self.context.personal_costs_store.records(scope_username, 'head')
                    if scope_username is not None
                    else self.read_heads(result['users'], deadline)
                )
                if row.get('record') == 'head'
                and subject(row.get('subject'))
                and (scope_username is None or row.get('subject') == scope_username)
            ],
        )
        for row in heads:
            username = row['subject']
            result['users'].setdefault(username, username)
            try:
                result['projections'][username] = self.projection(
                    username, json.loads(row['payload']), deadline
                )
            except exceptions.SocaException:
                raise
            except Exception:
                result['projections'][username] = dict(
                    costs=None, head=None, state='unavailable'
                )
        for username in result['users']:
            result['projections'].setdefault(
                username,
                dict(
                    costs=None,
                    head=None,
                    state='collecting'
                    if result['coverage']['projections'] == 'ready'
                    else 'unavailable',
                ),
            )
        config = self.context.config()
        if config.is_module_enabled(constants.MODULE_SCHEDULER):
            module = config.get_module_id(constants.MODULE_SCHEDULER)
            index = f'{self.context.cluster_name()}_{module}_jobs'
            result['jobs'] = optional(
                'jobs',
                lambda: list(
                    self.search(
                        index,
                        {
                            'bool': {
                                'filter': (
                                    [{'term': {'owner.raw': scope_username}}]
                                    if scope_username
                                    else []
                                )
                                + [
                                    {
                                        'range': {
                                            'end_time': {
                                                'gte': period.start,
                                                'lt': period.end,
                                            }
                                        }
                                    }
                                ]
                            }
                        },
                        deadline,
                        fields=[
                            'job_uid',
                            'job_id',
                            'name',
                            'queue',
                            'execution_hosts',
                            'owner',
                            'state',
                            'start_time',
                            'end_time',
                            'total_time_secs',
                            'params',
                            'scaling_mode',
                            'provisioning_options',
                            'estimated_bom_cost',
                            'project_id',
                            'project',
                            'project_title',
                            'project_name',
                        ],
                    )
                ),
            )
            if result['coverage']['jobs'] == 'ready':
                result['coverage']['jobs'] = 'partial'
                result['warnings'].append(
                    'Completed-job index freshness is unknown; indexing lag may omit records.'
                )
        else:
            result['coverage']['jobs'] = 'not_applicable'
        if config.is_module_enabled(constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER):
            index = config.get_string(
                'virtual-desktop-controller.opensearch.dcv_session.alias', required=True
            )
            result['desktops'] = optional(
                'desktops',
                lambda: list(
                    self.search(
                        index,
                        self.desktop_period_query(period, scope_username),
                        deadline,
                    )
                ),
            )
            module = config.get_module_id(constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)
            table = (
                self.context.aws()
                .dynamodb_table()
                .Table(
                    f'{self.context.cluster_name()}.{module}.controller.session-history'
                )
            )
            history = optional(
                'desktop_history',
                lambda: list(self.scan(table, deadline, scope_username)),
            )
            activity_sessions = set()
            if insights:

                def activity():
                    # one stored record per day the collector wrote; never the log groups
                    store = self.context.personal_costs_store
                    from ideaclustermanager.app.costs.personal_costs_store import SYSTEM
                    from ideaclustermanager.app.costs.desktop_activity import (
                        record_key,
                    )

                    rows = []
                    for row in store.records(
                        SYSTEM,
                        record_key(period.start_date),
                        record_key(period.end_date),
                    ):
                        check_deadline(deadline)
                        value = store.resolve_source(SYSTEM, json.loads(row['payload']))
                        sessions = (value or {}).get('sessions') or {}
                        if scope_username is not None:
                            sessions = {
                                key: entry
                                for key, entry in sessions.items()
                                if entry.get('owner') == scope_username
                            }
                        rows.append(dict(date=row['record'][5:], sessions=sessions))
                        activity_sessions.update(sessions)
                    return rows

                result['desktop_activity'] = optional('desktop_activity', activity)
            if result['coverage']['desktops'] == 'ready':
                documents = result['desktops']
                more = optional(
                    'desktops',
                    lambda: self.whole_sessions(
                        index,
                        documents,
                        [row for row in history if row.get('run_of')],
                        activity_sessions,
                        period,
                        deadline,
                        scope_username,
                    ),
                )
                if result['coverage']['desktops'] == 'ready' and not insights:
                    self.add_desktop_shapes(result, index, deadline)
                if result['coverage']['desktops'] == 'ready':
                    documents.extend(more)
                else:
                    # as when the one read failed: the source is missing, not partial
                    result['desktops'] = []
                    result['desktop_shapes'] = []
            result['desktops'].extend(
                {'_history': True, '_source': row} for row in history
            )
        else:
            result['coverage']['desktops'] = 'not_applicable'
            result['coverage']['desktop_history'] = 'not_applicable'
        for hit in [] if insights else result['jobs'] + result['desktops']:
            owner = hit.get('_source', {}).get('owner')
            if subject(owner):
                result['users'].setdefault(owner, owner)
                result['projections'].setdefault(
                    owner, dict(costs=None, head=None, state='collecting')
                )
        for shape in result.get('desktop_shapes', []):
            owner = shape.get('owner')
            if subject(owner):
                result['users'].setdefault(owner, owner)
                result['projections'].setdefault(
                    owner, dict(costs=None, head=None, state='collecting')
                )
        if insights or include_storage:

            def storage():
                store = self.context.personal_costs_store
                from ideaclustermanager.app.costs.personal_costs_store import SYSTEM

                rows = []
                # share:<day>:<fs id>, so a key range reads only the period's days; '~'
                # sorts after every character a filesystem id holds
                for row in store.records(
                    SYSTEM,
                    f'share:{period.start_date}',
                    f'share:{period.end_date}~',
                ):
                    check_deadline(deadline)
                    day = row['record'].split(':')[1]
                    if period.start_date <= day <= period.end_date:
                        value = store.resolve_source(SYSTEM, json.loads(row['payload']))
                        if value:
                            rows.append(dict(value, date=day))
                return rows

            result['storage'] = optional('storage', storage)
            if include_storage:
                for row in result['storage']:
                    for owner in row.get('users') or {}:
                        if subject(owner):
                            result['users'].setdefault(owner, owner)
        check_deadline(deadline)
        return result
