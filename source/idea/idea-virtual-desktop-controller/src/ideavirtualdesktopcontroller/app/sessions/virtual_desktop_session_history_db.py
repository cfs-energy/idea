"""
A durable record of desktops that no longer exist, and of when every desktop ran.

The sessions table hard deletes a terminated desktop's row and its search document goes
with it, so one row is written here first and the desktop can still be costed. Beside it
sits one run row per start: the interval the instance actually ran, keyed
'<idea_session_id>#run#<started_on>' and pointing back at the desktop through run_of.
Only the cost pages read this table.
"""

from ideadatamodel import VirtualDesktopSession, VirtualDesktopSessionState
from ideasdk.utils import Utils

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from datetime import datetime
from typing import Dict, List, Optional

# how long a terminated desktop stays costable, matching the usage rows
DEFAULT_RETENTION_DAYS = 400

# run rows sort under their desktop's id and never collide with its termination row
RUN_KEY = '#run#'

# a desktop is launched when its row is created; its RunInstances event is matched to
# the row within this much of created_on
LAUNCH_MATCH_MS = 15 * 60 * 1000

# Known limitation: the administrator view reads every user's history with a scan. a
# cluster that outgrows this wants an index on the deletion time.
MAX_SCAN_PAGES = 1000


def to_epoch_ms(value) -> Optional[int]:
    """
    epoch milliseconds from a datetime, or from a number already in milliseconds. the
    session model holds datetimes while the stored rows hold milliseconds, and this
    record is written from both, so a number is taken at face value, never converted.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return Utils.to_milliseconds(value)
    if isinstance(value, (int, float)):
        return int(value)
    return None


class VirtualDesktopSessionHistoryDB:
    def __init__(self, context, logger=None):
        self.context = context
        self._logger = (
            logger
            if logger is not None
            else context.logger('virtual-desktop-session-history-db')
        )
        self._table_obj = None
        self._initialized = False

    @property
    def table_name(self) -> str:
        return f'{self.context.cluster_name()}.{self.context.module_id()}.controller.session-history'

    def initialize(self):
        """created by whichever controller process gets here first."""
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.table_name,
                'AttributeDefinitions': [
                    {'AttributeName': 'owner', 'AttributeType': 'S'},
                    {'AttributeName': 'idea_session_id', 'AttributeType': 'S'},
                ],
                'KeySchema': [
                    {'AttributeName': 'owner', 'KeyType': 'HASH'},
                    {'AttributeName': 'idea_session_id', 'KeyType': 'RANGE'},
                ],
                'BillingMode': 'PAY_PER_REQUEST',
            },
            wait=True,
            ttl=True,
            ttl_attribute_name='ttl',
        )
        self._initialized = True

    @property
    def table(self):
        if self._table_obj is None:
            if not self._initialized:
                self.initialize()
            self._table_obj = self.context.aws().dynamodb_table().Table(self.table_name)
        return self._table_obj

    def get_retention_days(self) -> int:
        return self.context.config().get_int(
            f'{self.context.module_id()}.dcv_session.history.retention_days',
            DEFAULT_RETENTION_DAYS,
        )

    def build_entry(
        self, session: VirtualDesktopSession, deleted_on: int = None
    ) -> Dict:
        """
        what the desktop was and when it ran. a desktop terminated straight from
        running carries no stopped_on, and the deletion is when it stopped costing.
        """
        deleted_on = to_epoch_ms(deleted_on)
        if deleted_on is None:
            deleted_on = Utils.current_time_ms()
        stopped_on = to_epoch_ms(session.stopped_on)
        stop_time_estimated = False
        warning_stop = to_epoch_ms(session.cleanup_warning_stop_time)
        # Older warning writes stamped stopped_on with the warning date.
        if warning_stop is not None and (
            stopped_on is None or warning_stop < stopped_on
        ):
            stopped_on = warning_stop
            stop_time_estimated = True
        if stopped_on is None:
            if session.state == VirtualDesktopSessionState.STOPPED:
                stopped_on = to_epoch_ms(session.updated_on)
                stop_time_estimated = True
            else:
                # terminated straight from running: it stopped costing at the deletion
                stopped_on = deleted_on
        server = session.server
        project = session.project
        return {
            'owner': session.owner,
            'idea_session_id': session.idea_session_id,
            'name': session.name,
            'base_os': Utils.get_as_string(session.base_os, None),
            'instance_type': None if server is None else server.instance_type,
            'instance_id': None if server is None else server.instance_id,
            # deleted before it was ever given an instance: it never ran or cost
            'never_ran': server is None or Utils.is_empty(server.instance_id),
            'project_id': None if project is None else project.project_id,
            'created_on': to_epoch_ms(session.created_on),
            'stopped_on': stopped_on,
            'stop_time_estimated': stop_time_estimated,
            'deleted_on': deleted_on,
            'hibernation_enabled': session.hibernation_enabled,
            'ttl': Utils.current_time() + (self.get_retention_days() * 86400),
        }

    def record_termination(
        self, session: VirtualDesktopSession, deleted_on: int = None
    ):
        """
        bookkeeping must never stop a deletion: a desktop the user asked to remove is
        removed whether or not its history could be written.
        """
        try:
            if session is None or Utils.is_empty(session.idea_session_id):
                return
            if Utils.is_empty(session.owner):
                return
            self.table.put_item(Item=self.build_entry(session, deleted_on=deleted_on))
        except Exception as e:
            # including a failure building the row: nothing here may reach the caller
            session_id = getattr(session, 'idea_session_id', None)
            self._logger.warning(
                f'failed to record session history for {session_id}: {e}'
            )

    def open_run(self, session: VirtualDesktopSession, reconciled: bool = False):
        """
        the desktop started running. a run row per start is what the cost pages bill
        from; like the termination record it must never fail the state change.
        """
        try:
            started_on = Utils.current_time_ms()
            server = session.server
            item = {
                'owner': session.owner,
                'idea_session_id': f'{session.idea_session_id}{RUN_KEY}{started_on}',
                'run_of': session.idea_session_id,
                'instance_id': None if server is None else server.instance_id,
                'instance_type': None if server is None else server.instance_type,
                'started_on': started_on,
                'ttl': Utils.current_time() + (self.get_retention_days() * 86400),
            }
            if reconciled:
                # not a real start: the desktop was already running, so the cost pages
                # still estimate the time before it
                item['reconciled'] = True
            self.table.put_item(Item=item)
        except Exception as e:
            self._logger.warning(
                f'failed to record a run for {getattr(session, "idea_session_id", None)}: {e}'
            )

    def close_runs(self, session: VirtualDesktopSession):
        """
        the desktop stopped costing: every run still open is closed now. more than one
        can be open only if a stop was missed, and the cost pages end such a run at the
        next start anyway.
        """
        try:
            stopped_on = Utils.current_time_ms()
            for item in self._open_runs(session):
                self.table.update_item(
                    Key={
                        'owner': item['owner'],
                        'idea_session_id': item['idea_session_id'],
                    },
                    UpdateExpression='SET stopped_on = :stopped_on, #ttl = :ttl',
                    ExpressionAttributeNames={'#ttl': 'ttl'},
                    ExpressionAttributeValues={
                        ':stopped_on': stopped_on,
                        ':ttl': Utils.current_time()
                        + (self.get_retention_days() * 86400),
                    },
                )
        except Exception as e:
            self._logger.warning(
                f'failed to close the runs of {getattr(session, "idea_session_id", None)}: {e}'
            )

    def _open_runs(self, session: VirtualDesktopSession) -> List[Dict]:
        request = {
            'KeyConditionExpression': Key('owner').eq(session.owner)
            & Key('idea_session_id').begins_with(f'{session.idea_session_id}{RUN_KEY}'),
            'FilterExpression': Attr('stopped_on').not_exists(),
        }
        items: List[Dict] = []
        while True:
            page = self.table.query(**request)
            items.extend(Utils.get_value_as_list('Items', page, []))
            if not page.get('LastEvaluatedKey'):
                return items
            request['ExclusiveStartKey'] = page['LastEvaluatedKey']

    def reconcile_run(self, session: VirtualDesktopSession) -> bool:
        """
        a running desktop with no open run, because it was already running when runs
        began to be recorded or its start write failed, gets one from now. a desktop
        that has an open run is left alone, so this is safe to repeat.
        """
        try:
            if self._open_runs(session):
                return False
        except Exception as e:
            self._logger.warning(
                f'failed to read the runs of {session.idea_session_id}: {e}'
            )
            return False
        self.open_run(session, reconciled=True)
        return True

    def backfill_instance_ids(self, launches, live_instance_ids=lambda: set()) -> bool:
        """
        put the instance id back on rows of desktops deleted before runs were
        recorded, so their cost comes from what CloudWatch saw rather than from
        creation to stop. a row is matched to the one launch of its owner (and
        instance type) within LAUNCH_MATCH_MS of its creation, among the instances no
        other desktop holds; with none, several, a creation older than CloudTrail
        keeps, or an instance another row also matched, it is marked instance_unknown
        and the cost pages leave it unpriced. each row is written once, so this is
        safe to repeat; true when nothing is left to do.
        """
        from ideavirtualdesktopcontroller.app.sessions.desktop_launches import (
            CLOUDTRAIL_DAYS,
        )

        all_rows = self.list_all()
        # runs began to be recorded at the first run; a desktop deleted since then
        # without an instance id never had one, and has nothing to look up
        runs = [
            Utils.get_value_as_int('started_on', row, 0)
            for row in all_rows
            if row.get('run_of')
        ]
        recording_since = min(runs) if runs else Utils.current_time_ms()
        rows = [
            row
            for row in all_rows
            if not row.get('run_of')
            and not row.get('instance_id')
            and not row.get('instance_unknown')
            and not row.get('never_ran')
            and Utils.get_value_as_int('deleted_on', row, 0) < recording_since
        ]
        if not rows:
            return True
        if not launches.read():
            return False
        taken = set(live_instance_ids()) | {
            row['instance_id'] for row in all_rows if row.get('instance_id')
        }
        horizon = Utils.current_time_ms() - CLOUDTRAIL_DAYS * 86400000
        claims: Dict[str, List[Dict]] = {}
        for row in rows:
            created_on = Utils.get_value_as_int('created_on', row, 0)
            instance_type = row.get('instance_type')
            matches = set()
            if created_on >= horizon:
                matches = {
                    launch['instance_id']
                    for launch in launches.launches
                    if launch['owner'] == row.get('owner')
                    and launch['instance_id'] not in taken
                    and abs(launch['launched_on'] - created_on) <= LAUNCH_MATCH_MS
                    and (
                        not instance_type
                        or not launch['instance_type']
                        or launch['instance_type'] == instance_type
                    )
                }
            claims.setdefault(matches.pop() if len(matches) == 1 else None, []).append(
                row
            )
        for instance_id, claimants in claims.items():
            # two rows on one instance: at most one is right, and which is unknown
            unique = instance_id is not None and len(claimants) == 1
            for row in claimants:
                if unique:
                    update, values = 'SET instance_id = :value', {':value': instance_id}
                else:
                    update, values = 'SET instance_unknown = :value', {':value': True}
                try:
                    self.table.update_item(
                        Key={
                            'owner': row['owner'],
                            'idea_session_id': row['idea_session_id'],
                        },
                        UpdateExpression=update,
                        ExpressionAttributeValues=values,
                        # a row its ttl removed meanwhile stays removed
                        ConditionExpression='attribute_exists(idea_session_id)',
                    )
                except ClientError as e:
                    if (
                        e.response.get('Error', {}).get('Code')
                        != 'ConditionalCheckFailedException'
                    ):
                        raise
        return True

    def list_for_owner(self, owner: str) -> List[Dict]:
        result = self.table.query(KeyConditionExpression=Key('owner').eq(owner))
        return Utils.get_value_as_list('Items', result, [])

    def list_all(self) -> List[Dict]:
        items: List[Dict] = []
        last_key: Optional[Dict] = None
        for _ in range(MAX_SCAN_PAGES):
            kwargs = {}
            if last_key is not None:
                kwargs['ExclusiveStartKey'] = last_key
            result = self.table.scan(**kwargs)
            items.extend(Utils.get_value_as_list('Items', result, []))
            last_key = Utils.get_value_as_dict('LastEvaluatedKey', result, None)
            if last_key is None:
                break
        return items
