"""Exercise two real lock clients with a shared conditional-write store and lost heartbeats."""

import datetime
import json
import multiprocessing
import sqlite3
from threading import RLock
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from python_dynamodb_lock.python_dynamodb_lock import DynamoDBLockClient

from ideasdk.distributed_lock.distributed_lock import DistributedLock
from ideaclustermanagertests.test_account_reconciler import build


class ConditionalTable:
    def __init__(self, path):
        self.path = path

    def get_item(self, **kwargs):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT item FROM lease').fetchone()
        return {'Item': json.loads(row[0])} if row else {}

    def change(self, operation, kwargs):
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT item FROM lease').fetchone()
            old = json.loads(row[0]) if row else None
            values = kwargs.get('ExpressionAttributeValues', {})
            expected = values.get(':old_rvn') or values.get(':rvn')
            if (expected and (not old or old['record_version_number'] != expected)) or (
                not expected and old
            ):
                raise ClientError(
                    {'Error': {'Code': 'ConditionalCheckFailedException'}}, operation
                )
            if operation == 'put':
                item = kwargs['Item']
            elif operation == 'update':
                item = {
                    **old,
                    'record_version_number': values[':new_rvn'],
                    'expiry_time': values[':new_et'],
                }
            else:
                item = None
            db.execute('DELETE FROM lease')
            if item is not None:
                db.execute(
                    'INSERT INTO lease VALUES (?)', (json.dumps(item, default=float),)
                )

    def put_item(self, **kwargs):
        self.change('put', kwargs)

    def update_item(self, **kwargs):
        self.change('update', kwargs)

    def delete_item(self, **kwargs):
        self.change('delete', kwargs)


def lease_worker(path, connection, pause):
    resource = Mock()
    resource.Table.return_value = ConditionalTable(path)
    # Suppress heartbeats to model a paused process, while retaining real acquisition and fencing.
    with (
        patch.object(DynamoDBLockClient, '_start_heartbeat_sender_thread'),
        patch.object(DynamoDBLockClient, '_start_heartbeat_checker_thread'),
    ):
        client = DynamoDBLockClient(
            resource,
            owner_name='worker',
            heartbeat_period=datetime.timedelta(seconds=0.1),
            safe_period=datetime.timedelta(seconds=0.5),
            lease_duration=datetime.timedelta(seconds=0.7),
        )
    lock = DistributedLock.__new__(DistributedLock)
    lock._lock_client = client
    lock._active_locks = {}
    lock._lock = RLock()
    service, context = build()
    context._lock = lock
    if pause:
        page = context.accounts.list_users.return_value

        def inventory(request):
            connection.send('paused')
            assert connection.recv() == 'resume'
            return page

        context.accounts.list_users.side_effect = inventory
    try:
        report = service.run_once(dry_run=False)
        connection.send((report, context.accounts.disable_user.call_count))
    except RuntimeError:
        connection.send(('lease-lost', context.accounts.disable_user.call_count))
    finally:
        client._app_callback_executor.shutdown()
        connection.close()


def test_expired_worker_cannot_apply_after_another_process_acquires(tmp_path):
    path = str(tmp_path / 'leases.db')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE lease (item TEXT)')
    ctx = multiprocessing.get_context('spawn')
    old_parent, old_child = ctx.Pipe()
    new_parent, new_child = ctx.Pipe()
    old = ctx.Process(target=lease_worker, args=(path, old_child, True))
    new = ctx.Process(target=lease_worker, args=(path, new_child, False))
    old.start()
    try:
        assert old_parent.poll(15)
        assert old_parent.recv() == 'paused'
        new.start()
        assert new_parent.poll(15)
        report, count = new_parent.recv()
        assert report['disabled'] == count == 1
        old_parent.send('resume')
        assert old_parent.poll(15)
        assert old_parent.recv() == ('lease-lost', 0)
        old.join(5)
        new.join(5)
        assert old.exitcode == new.exitcode == 0
    finally:
        for process in (old, new):
            if process.is_alive():
                process.terminate()
                process.join(5)
