"""A desktop as the controller reads it back: a stored session row through the session table's own conversion."""

from unittest.mock import Mock

from ideavirtualdesktopcontroller.app.schedules.virtual_desktop_schedule_db import (
    VirtualDesktopScheduleDB,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_db import (
    VirtualDesktopServerDB,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
    VirtualDesktopSoftwareStackDB,
)


def session_table_reader() -> VirtualDesktopSessionDB:
    db = VirtualDesktopSessionDB.__new__(VirtualDesktopSessionDB)
    db._logger = Mock()
    db._server_db = VirtualDesktopServerDB.__new__(VirtualDesktopServerDB)
    db._software_stack_db = VirtualDesktopSoftwareStackDB.__new__(
        VirtualDesktopSoftwareStackDB
    )
    db._schedule_db = VirtualDesktopScheduleDB.__new__(VirtualDesktopScheduleDB)
    return db


def stored_row(
    session_id='s-1',
    state='INITIALIZING',
    dcv_session_id='dcv-1',
    instance_id='i-1',
    **extra,
) -> dict:
    return {
        'owner': 'user-a',
        'idea_session_id': session_id,
        'base_os': 'amazonlinux2023',
        'name': 'desk',
        'dcv_session_id': dcv_session_id,
        'session_type': 'VIRTUAL',
        'state': state,
        'updated_on': 1700000000000,
        # the host address is written here too, and the reader drops it
        'server': {'instance_id': instance_id, 'private_ip': '10.0.0.5'},
        'software_stack': {
            'stack_id': 'ss-1',
            'base_os': 'amazonlinux2023',
            'min_storage_value': 10,
            'min_storage_unit': 'gb',
            'min_ram_value': 4,
            'min_ram_unit': 'gb',
            'architecture': 'x86_64',
            'gpu': 'NO_GPU',
        },
        **extra,
    }


def read_back(**kwargs):
    return session_table_reader().convert_db_dict_to_session_object(
        stored_row(**kwargs)
    )


class FakeTable:
    """
    The DynamoDB calls the controller's tables make, on rows held in memory: SET updates,
    the attribute_exists and version conditions, gets, queries and deletes.
    """

    def __init__(self, hash_key, range_key, rows=()):
        self.hash_key, self.range_key = hash_key, range_key
        self.rows = {self._key(row): dict(row) for row in rows}

    def _key(self, item):
        return (item[self.hash_key], item[self.range_key])

    def get_item(self, Key, **kwargs):
        row = self.rows.get(self._key(Key))
        return {'Item': dict(row)} if row else {}

    def query(self, **kwargs):
        return {'Items': [dict(row) for row in self.rows.values()]}

    def scan(self, **kwargs):
        return {'Items': [dict(row) for row in self.rows.values()]}

    def delete_item(self, Key, **kwargs):
        self.rows.pop(self._key(Key), None)
        return {}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeNames,
        ExpressionAttributeValues,
        ConditionExpression=None,
        ReturnValues=None,
    ):
        from botocore.exceptions import ClientError

        key = self._key(Key)
        old = self.rows.get(key)
        if ConditionExpression:
            names = ExpressionAttributeNames

            def holds(clause):
                if clause.startswith('attribute_exists'):
                    return old is not None
                if clause.startswith('(attribute_not_exists('):
                    # an absent or NULL attribute
                    name = clause[len('(attribute_not_exists(') :].split(')')[0]
                    return old is not None and old.get(names[name]) is None
                name, value = [part.strip() for part in clause.split('=')]
                return (
                    old is not None
                    and old.get(names[name]) == ExpressionAttributeValues[value]
                )

            ok = all(holds(c) for c in ConditionExpression.split(' AND '))
            if not ok:
                raise ClientError(
                    {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'
                )
        new = dict(old or Key)
        sets = UpdateExpression.split(' REMOVE ')[0][len('SET ') :]
        for assignment in sets.split(', '):
            name, value = [part.strip() for part in assignment.split('=')]
            new[ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]
        self.rows[key] = new
        return {
            'Attributes': dict(old or {}) if ReturnValues == 'ALL_OLD' else dict(new)
        }
