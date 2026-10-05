"""
Daily idle time per desktop, from the checks the idle stop records.

Each time the idle stop checks a running desktop it writes the check (CPU, desktop
connections, login sessions) to a log group of that desktop. The collector reads those
checks once, counts checked and idle half hours per desktop per day, and stores one
record per day that reports read; a report never reads the log groups itself.
"""

import json
from collections import defaultdict

import arrow
from botocore.exceptions import ClientError

from ideadatamodel import constants
from ideadatamodel.reporting.efficiency import desktop_check_idle
from ideaclustermanager.app.costs.my_costs_service import (
    LIVE_SESSION_STATES,
    MAX_ADMIN_SESSIONS,
    SESSION_FIELDS,
    MyCostsService,
)
from ideaclustermanager.app.costs.personal_costs_store import SYSTEM

SLOT_SECONDS = 1800
# check output reaches the log group shortly after the check; a day is settled once
# this long has passed after it ends, and is not read again
SETTLE_SECONDS = 3600
STATE = 'idle-state'


def record_key(day):
    return f'idle:{day}'


def checks_from_events(events):
    """
    one check per command run. each run writes its own log stream, and output spread
    over several lines can arrive as several events, so a stream is read as a whole.
    """
    streams = {}
    for event in events:
        if not event.get('logStreamName', '').endswith('/stdout'):
            continue
        stream = streams.setdefault(event['logStreamName'], [event['timestamp'], []])
        stream[0] = min(stream[0], event['timestamp'])
        stream[1].append(event.get('message', ''))
    for timestamp, messages in streams.values():
        try:
            yield timestamp, json.loads('\n'.join(messages))
        except ValueError:
            continue


def tally(checks, threshold, zone):
    """
    {day: [checked half hours, idle half hours]}. one check counts per half hour; a half
    hour with any check in use is in use. unreadable checks and half hours without a
    check are not counted at all, so missing data is never read as idle or in use.
    """
    slots = {}
    for timestamp, check in checks:
        idle = desktop_check_idle(check, threshold)
        if idle is not None:
            slot = timestamp // 1000 // SLOT_SECONDS
            slots[slot] = slots.get(slot, True) and idle
    days = defaultdict(lambda: [0, 0])
    for slot, idle in slots.items():
        day = days[arrow.get(slot * SLOT_SECONDS).to(zone).format('YYYY-MM-DD')]
        day[0] += 1
        day[1] += int(idle)
    return days


class DesktopActivity:
    def __init__(self, context, store):
        self.context = context
        self.store = store

    def sessions(self, start_ms, end_ms):
        """every desktop that may have run since start: live ones and ones updated since."""
        costs = MyCostsService(self.context)
        index = self.context.config().get_string(
            'virtual-desktop-controller.opensearch.dcv_session.alias', required=True
        )
        response = costs._search(
            index,
            {
                'size': MAX_ADMIN_SESSIONS,
                '_source': SESSION_FIELDS,
                'query': {
                    'bool': {
                        'filter': [{'range': {'created_on': {'lte': end_ms}}}],
                        'should': [
                            {'range': {'updated_on': {'gte': start_ms}}},
                            {'terms': {'state.raw': sorted(LIVE_SESSION_STATES)}},
                        ],
                        'minimum_should_match': 1,
                    }
                },
            },
        )
        if response is None:
            raise ValueError('Desktop sessions unavailable')
        hits = list((response.get('hits') or {}).get('hits') or [])
        hits += costs._history_hits(None, start_ms, end_ms, all_users=True)
        return costs._newest_per_session(hits)

    def events(self, group, start_ms, end_ms):
        request = dict(logGroupName=group, startTime=start_ms, endTime=end_ms)
        logs = self.context.aws().logs()
        try:
            while True:
                page = logs.filter_log_events(**request)
                yield from page.get('events', [])
                if not page.get('nextToken'):
                    return
                request['nextToken'] = page['nextToken']
        except ClientError as error:
            # a desktop the idle stop never checked has no log group
            if error.response['Error']['Code'] != 'ResourceNotFoundException':
                raise

    def capture(self, now):
        """rewrite every unsettled day from the start of last month to now."""
        config = self.context.config()
        if not config.is_module_enabled(constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER):
            return
        module = config.get_module_id(constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)
        threshold = config.get_float(
            'virtual-desktop-controller.dcv_session.cpu_utilization_threshold',
            required=True,
        )
        zone = now.tzinfo
        start = now.floor('month').shift(months=-1)
        settled = (self.store.get(SYSTEM, STATE) or {}).get('settled_through')
        if settled:
            start = max(start, arrow.get(settled).replace(tzinfo=zone).shift(days=1))
        start_ms, end_ms = start.int_timestamp * 1000, now.int_timestamp * 1000
        days = defaultdict(dict)
        for source in self.sessions(start_ms, end_ms):
            owner, session_id = source.get('owner'), source.get('idea_session_id')
            if not owner or not session_id:
                continue
            group = f'/{self.context.cluster_name()}/{module}/dcv-session/{session_id}/cpu-utilization'
            created = int(source.get('created_on') or 0)
            checks = checks_from_events(
                self.events(group, max(start_ms, created), end_ms)
            )
            for day, (checked, idle) in tally(checks, threshold, zone).items():
                days[day][session_id] = dict(
                    owner=owner,
                    instance_type=(source.get('server') or {}).get('instance_type'),
                    checks=checked,
                    idle=idle,
                )
        cursor = start
        while cursor <= now:
            day = cursor.format('YYYY-MM-DD')
            self.store.put_source(SYSTEM, record_key(day), {'sessions': days[day]})
            cursor = cursor.shift(days=1)
        last = now.shift(seconds=-SETTLE_SECONDS).floor('day').shift(days=-1)
        if last >= start:
            self.store.put(
                SYSTEM, STATE, {'settled_through': last.format('YYYY-MM-DD')}
            )
