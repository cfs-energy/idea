"""
A durable record of desktops that no longer exist.

The sessions table hard deletes a terminated desktop's row and its search document goes
with it, so one row is written here first and the desktop can still be costed. Nothing
else reads this table, and it holds terminated desktops only.
"""

from ideadatamodel import VirtualDesktopSession
from ideasdk.utils import Utils

from datetime import datetime
from typing import Dict, List, Optional

# how long a terminated desktop stays costable, matching the usage rows
DEFAULT_RETENTION_DAYS = 400

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
        if stopped_on is None:
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
            'project_id': None if project is None else project.project_id,
            'created_on': to_epoch_ms(session.created_on),
            'stopped_on': stopped_on,
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

    def list_for_owner(self, owner: str) -> List[Dict]:
        from boto3.dynamodb.conditions import Key

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
