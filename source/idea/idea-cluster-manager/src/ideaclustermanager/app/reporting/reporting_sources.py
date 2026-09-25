"""Read recorded projections and activity without refreshing any source."""

import json
import time
from datetime import datetime
from decimal import Decimal

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


def subject(value):
    return isinstance(value, str) and bool(value) and not value.startswith('!')


class ReportingSources:
    def __init__(self, context):
        self.context = context

    @staticmethod
    def scan(table, deadline):
        request = {'ConsistentRead': True}
        while True:
            check_deadline(deadline)
            page = table.scan(**request)
            yield from page.get('Items', [])
            if not page.get('LastEvaluatedKey'):
                break
            request['ExclusiveStartKey'] = page['LastEvaluatedKey']
        check_deadline(deadline)

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

    def search(self, index, query, deadline):
        client = self.context.analytics_service().os_client.os_client
        scroll_id = None
        try:
            check_deadline(deadline)
            page = client.search(
                index=index,
                scroll='1m',
                request_timeout=max(0.1, deadline - time.monotonic()),
                body={'size': 1000, 'sort': ['_doc'], 'query': query},
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

    def read(self, period, deadline):
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
                result['coverage'][name] = 'unavailable'
                result['warnings'].append(f'{name} source unavailable.')
                return []
            except Exception:
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
                for row in self.scan(self.context.personal_costs_store.table, deadline)
                if row.get('record') == 'head' and subject(row.get('subject'))
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
                'jobs', lambda: list(self.search(index, {'match_all': {}}, deadline))
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
                lambda: list(self.search(index, {'match_all': {}}, deadline)),
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
                'desktop_history', lambda: list(self.scan(table, deadline))
            )
            result['desktops'].extend(
                {'_history': True, '_source': row} for row in history
            )
        else:
            result['coverage']['desktops'] = 'not_applicable'
            result['coverage']['desktop_history'] = 'not_applicable'
        for hit in result['jobs'] + result['desktops']:
            owner = hit.get('_source', {}).get('owner')
            if subject(owner):
                result['users'].setdefault(owner, owner)
                result['projections'].setdefault(
                    owner, dict(costs=None, head=None, state='collecting')
                )
        check_deadline(deadline)
        return result
