"""
Image build bookkeeping: name stamp parsing, state classification, the record
round trip through a DynamoDB item, and the runner's building -> complete / failed
transitions including the stale-record guard.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

from botocore.exceptions import ClientError

import pytest

from ideadatamodel import ImageBuildRecord, exceptions
from ideasdk.aws.image_builds import (
    STOPPED_AT_TAG,
    BUILD_STAMP,
    BUILD_STATUS_BUILDING,
    check_builder_instance_type,
    check_builder_type_architecture,
    default_builder_instance_type,
    sanitize_aws_message,
    unique_build_version,
    BUILD_STATUS_COMPLETE,
    BUILD_STATUS_FAILED,
    STALE_AFTER,
    ImageBuildRecordsDB,
    ImageBuildRunner,
    build_stamp,
    describe_images_by_id,
    image_state,
    new_record,
)


class FakeTable:
    """honors the conditional put the claim relies on"""

    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None, **kwargs):
        key = (Item['base_os'], Item['architecture'])
        if ConditionExpression and self.items.get(key, {}).get('status') == 'building':
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
            )
        self.items[key] = dict(Item)

    def delete_item(self, Key):
        self.items.pop((Key['base_os'], Key['architecture']), None)

    def get_item(self, Key):
        item = self.items.get((Key['base_os'], Key['architecture']))
        return {'Item': dict(item)} if item else {}

    def scan(self, **kwargs):
        return {'Items': [dict(item) for item in self.items.values()]}


def records_db() -> ImageBuildRecordsDB:
    db = ImageBuildRecordsDB(
        context=Mock(), table_name='idea-test.scheduler.image-builds'
    )
    db._table_obj = FakeTable()
    return db


def test_build_stamp_reads_the_builder_suffix():
    assert build_stamp('idea-compute-node-rocky9-v08312026-214021') == datetime(
        2026, 8, 31, 21, 40, 21, tzinfo=timezone.utc
    )
    assert build_stamp('al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64') is None
    assert build_stamp(None) is None


def test_image_state_is_the_prefix():
    assert (
        image_state('idea-dcv-host-rocky9-v08312026-214021', 'idea-dcv-host-')
        == 'built'
    )
    assert (
        image_state('Rocky-9-EC2-Base-9.6-20250531.0.x86_64', 'idea-dcv-host-')
        == 'stock'
    )
    assert image_state(None, 'idea-dcv-host-') == 'stock'


def test_record_round_trips_through_an_item():
    db = records_db()
    started = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    db.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status=BUILD_STATUS_COMPLETE,
            image_id='ami-1',
            started_on=started,
        )
    )
    item = db._table_obj.items[('rocky9', 'x86_64')]
    assert item['started_on'] == int(started.timestamp() * 1000)
    assert 'finished_on' not in item

    record = db.get('rocky9', 'x86_64')
    assert record.started_on == started
    assert record.image_id == 'ami-1'
    assert [r.base_os for r in db.list_all()] == ['rocky9']


def test_a_blocking_build_records_complete_and_runs_the_post_step():
    db = records_db()
    runner = ImageBuildRunner(context=Mock(), records=db, logger=Mock())
    seen = {}

    def build(progress):
        progress({'instance_id': 'i-builder'})
        assert db.get('rocky9', 'x86_64').instance_id == 'i-builder'
        return 'ami-built'

    def on_success(image_id, record):
        seen['image_id'] = image_id

    record = new_record(
        'rocky9',
        'x86_64',
        'idea-compute-node-rocky9-v1',
        'ami-stock',
        'operator',
        False,
    )
    runner.start(record, build, on_success, blocking=True)

    stored = db.get('rocky9', 'x86_64')
    assert stored.status == BUILD_STATUS_COMPLETE
    assert stored.image_id == 'ami-built'
    assert stored.instance_id == 'i-builder'
    assert stored.finished_on is not None
    assert stored.error is None
    assert seen == {'image_id': 'ami-built'}


def test_a_failing_build_records_failed_with_the_error():
    db = records_db()
    runner = ImageBuildRunner(context=Mock(), records=db, logger=Mock())

    def build(progress):
        raise RuntimeError('InsufficientInstanceCapacity')

    runner.start(
        new_record('rhel9', 'x86_64', 'n', 'ami-stock', 'operator', False),
        build,
        blocking=True,
    )

    stored = db.get('rhel9', 'x86_64')
    assert stored.status == BUILD_STATUS_FAILED
    assert stored.error.startswith('RuntimeError:')
    assert 'InsufficientInstanceCapacity' not in stored.error
    assert stored.image_id is None


def test_a_failing_post_step_keeps_the_image_and_notes_the_error():
    db = records_db()
    runner = ImageBuildRunner(context=Mock(), records=db, logger=Mock())

    def on_success(image_id, record):
        raise RuntimeError('stack row vanished')

    runner.start(
        new_record('rhel9', 'x86_64', 'n', 'ami-stock', 'operator', True),
        lambda progress: 'ami-built',
        on_success,
        blocking=True,
    )

    stored = db.get('rhel9', 'x86_64')
    assert stored.status == BUILD_STATUS_COMPLETE
    assert stored.image_id == 'ami-built'
    assert 'RuntimeError' in stored.error
    assert 'stack row vanished' not in stored.error


def test_a_declined_cli_prompt_leaves_no_record():
    db = records_db()
    runner = ImageBuildRunner(context=Mock(), records=db, logger=Mock())

    def build(progress):
        raise SystemExit(0)

    with pytest.raises(SystemExit):
        runner.start(
            new_record('rocky9', 'x86_64', 'n', 'ami-stock', 'operator', False),
            build,
            blocking=True,
        )
    assert db.get('rocky9', 'x86_64') is None


def test_a_second_build_while_one_is_running_is_refused():
    db = records_db()
    runner = ImageBuildRunner(context=Mock(), records=db, logger=Mock())
    db.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status=BUILD_STATUS_BUILDING,
            instance_id='i-busy',
            started_on=datetime.now(tz=timezone.utc) - timedelta(minutes=5),
        )
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        runner.start(
            new_record('rocky9', 'x86_64', 'n', 'ami-stock', 'operator', False),
            lambda p: 'x',
            blocking=True,
        )
    assert 'already running on i-busy' in exc_info.value.message


def test_a_stale_building_record_is_marked_failed_and_no_longer_blocks():
    db = records_db()
    runner = ImageBuildRunner(context=Mock(), records=db, logger=Mock())
    db.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status=BUILD_STATUS_BUILDING,
            instance_id='i-gone',
            started_on=datetime.now(tz=timezone.utc)
            - STALE_AFTER
            - timedelta(minutes=1),
        )
    )

    refreshed = runner.refresh(db.get('rocky9', 'x86_64'))
    assert refreshed.status == BUILD_STATUS_FAILED
    assert 'i-gone' in refreshed.error
    assert db.get('rocky9', 'x86_64').status == BUILD_STATUS_FAILED

    runner.start(
        new_record('rocky9', 'x86_64', 'n', 'ami-stock', 'operator', False),
        lambda p: 'ami-new',
        blocking=True,
    )
    assert db.get('rocky9', 'x86_64').image_id == 'ami-new'


def test_describe_images_by_id_survives_an_unknown_id():
    ec2 = Mock()

    def describe_images(ImageIds):
        if 'ami-gone' in ImageIds and len(ImageIds) > 1:
            raise RuntimeError('InvalidAMIID.NotFound')
        if ImageIds == ['ami-gone']:
            raise RuntimeError('InvalidAMIID.NotFound')
        return {'Images': [{'ImageId': i, 'Name': f'name-{i}'} for i in ImageIds]}

    ec2.describe_images.side_effect = describe_images
    found = describe_images_by_id(ec2, ['ami-ok', 'ami-gone', 'ami-ok'])
    assert set(found) == {'ami-ok'}


def test_the_version_is_unique_beyond_the_second_and_still_parses():
    a, b = unique_build_version(), unique_build_version()
    assert a != b
    assert BUILD_STAMP.search(f'-v{a}')
    assert build_stamp(f'idea-dcv-host-rocky9-v{a}') is not None
    assert build_stamp('idea-compute-node-rocky9-v08312026-214021') is not None


def test_builder_instance_types_are_allowlisted():
    check_builder_instance_type(None)
    check_builder_instance_type('m6i.large', 'x86_64')
    check_builder_instance_type('c6g.large', 'arm64')
    with pytest.raises(exceptions.SocaException) as exc_info:
        check_builder_instance_type('p4d.24xlarge', 'x86_64')
    assert 'm6i.large' in exc_info.value.message
    with pytest.raises(exceptions.SocaException):
        check_builder_instance_type('m6i.large', 'arm64')


def test_the_claim_is_conditional_so_a_lost_race_cannot_double_launch(monkeypatch):
    db = records_db()
    runner = ImageBuildRunner(context=Mock(), records=db, logger=Mock())
    # a competing request wrote the building row after the pre-read reported none
    db._table_obj.items[('rocky9', 'x86_64')] = {
        'base_os': 'rocky9',
        'architecture': 'x86_64',
        'status': 'building',
        'instance_id': 'i-theirs',
    }
    monkeypatch.setattr(db, 'get', lambda base_os, architecture: None)
    spawned = []
    monkeypatch.setattr(
        'ideasdk.aws.image_builds.threading.Thread',
        lambda *args, **kwargs: spawned.append(kwargs) or Mock(),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        runner.start(
            new_record('rocky9', 'x86_64', 'n', 'ami-stock', 'operator', False),
            lambda p: 'x',
        )
    assert 'already running' in exc_info.value.message
    assert spawned == []
    assert db._table_obj.items[('rocky9', 'x86_64')]['instance_id'] == 'i-theirs'


def test_a_stale_record_stops_its_builder_but_never_a_live_thread():
    db = records_db()
    context = MagicMock()
    runner = ImageBuildRunner(context=context, records=db, logger=Mock())
    stale = ImageBuildRecord(
        base_os='rocky9',
        architecture='x86_64',
        status=BUILD_STATUS_BUILDING,
        instance_id='i-stuck',
        started_on=datetime.now(tz=timezone.utc) - STALE_AFTER - timedelta(minutes=1),
    )
    db.put(stale)

    alive = Mock()
    alive.is_alive.return_value = True
    runner._live[('rocky9', 'x86_64')] = alive
    assert runner.refresh(db.get('rocky9', 'x86_64')).status == BUILD_STATUS_BUILDING
    context.aws().ec2().stop_instances.assert_not_called()

    alive.is_alive.return_value = False
    refreshed = runner.refresh(db.get('rocky9', 'x86_64'))
    assert refreshed.status == BUILD_STATUS_FAILED
    context.aws().ec2().stop_instances.assert_called_once_with(InstanceIds=['i-stuck'])


def test_initialize_treats_a_concurrent_create_as_exists_and_bounds_the_wait(
    monkeypatch,
):
    import ideasdk.aws.image_builds as module

    context = MagicMock()
    states = iter(['missing', 'CREATING', 'ACTIVE'])

    def describe_table(TableName):
        state = next(states)
        if state == 'missing':
            raise ClientError(
                {'Error': {'Code': 'ResourceNotFoundException'}}, 'DescribeTable'
            )
        return {'Table': {'TableStatus': state}}

    context.aws().dynamodb().describe_table.side_effect = describe_table
    context.aws_util().dynamodb_create_table.side_effect = ClientError(
        {'Error': {'Code': 'ResourceInUseException'}}, 'CreateTable'
    )
    context.aws().dynamodb_table().Table.return_value = FakeTable()
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    db = ImageBuildRecordsDB(context=context, table_name='t').initialize()
    assert db is not None

    stuck = MagicMock()
    stuck.aws().dynamodb().describe_table.return_value = {
        'Table': {'TableStatus': 'CREATING'}
    }
    clock = iter([0, 1, 10_000])
    monkeypatch.setattr(module.time, 'time', lambda: next(clock))
    with pytest.raises(exceptions.SocaException) as exc_info:
        ImageBuildRecordsDB(context=stuck, table_name='t').initialize()
    assert 'did not become active' in exc_info.value.message


def test_the_sweep_fails_this_hosts_orphans_and_terminates_old_stopped_builders():
    import socket
    import time as time_module

    db = records_db()
    context = MagicMock()
    context.module_id.return_value = 'vdc'
    db.context = context
    mine = ImageBuildRecord(
        base_os='rocky9',
        architecture='x86_64',
        status=BUILD_STATUS_BUILDING,
        instance_id='i-mine',
        host=socket.gethostname(),
        started_on=datetime.now(tz=timezone.utc),
    )
    theirs = ImageBuildRecord(
        base_os='rhel9',
        architecture='x86_64',
        status=BUILD_STATUS_BUILDING,
        instance_id='i-theirs',
        host='another-controller',
        started_on=datetime.now(tz=timezone.utc),
    )
    db.put(mine)
    db.put(theirs)
    old = str(int(time_module.time() - 2 * 86400))
    recent = str(int(time_module.time() - 3600))
    context.aws().ec2().describe_instances.return_value = {
        'Reservations': [
            {
                'Instances': [
                    {
                        'InstanceId': 'i-old',
                        'Tags': [{'Key': STOPPED_AT_TAG, 'Value': old}],
                    },
                    {
                        'InstanceId': 'i-recent',
                        'Tags': [{'Key': STOPPED_AT_TAG, 'Value': recent}],
                    },
                ]
            }
        ]
    }

    orphaned = db.sweep_orphans(Mock())

    assert orphaned == ['rocky9/x86_64']
    assert db.get('rocky9', 'x86_64').status == BUILD_STATUS_FAILED
    assert 'restarted' in db.get('rocky9', 'x86_64').error
    assert db.get('rhel9', 'x86_64').status == BUILD_STATUS_BUILDING
    context.aws().ec2().stop_instances.assert_called_once_with(InstanceIds=['i-mine'])
    context.aws().ec2().terminate_instances.assert_called_once_with(
        InstanceIds=['i-old']
    )


def test_builder_type_defaults_follow_the_architecture():
    assert default_builder_instance_type('x86_64', 'm7i.large') == 'm7i.large'
    assert default_builder_instance_type(None, 'c7i.large') == 'c7i.large'
    assert default_builder_instance_type('arm64', 'm7i.large') == 'm8g.large'


def test_a_builder_type_of_the_other_architecture_is_refused_by_name():
    check_builder_type_architecture('m8g.large', 'arm64')
    check_builder_type_architecture('unknown.type', 'arm64')
    with pytest.raises(exceptions.SocaException) as exc_info:
        check_builder_type_architecture('m6i.large', 'arm64')
    assert 'm6i.large is x86_64; a arm64 image needs one of' in exc_info.value.message
    with pytest.raises(exceptions.SocaException) as exc_info:
        check_builder_instance_type('m6i.large', 'arm64')
    assert 'm6i.large is x86_64' in exc_info.value.message


def test_aws_errors_keep_their_code_and_a_scrubbed_message():
    db = records_db()
    runner = ImageBuildRunner(context=Mock(), records=db, logger=Mock())
    error = ClientError(
        {
            'Error': {
                'Code': 'InvalidParameterValue',
                'Message': "The architecture 'x86_64' of the specified instance type does not match the architecture 'arm64' of the specified AMI "
                '(arn:aws:iam::123456789012:instance-profile/builder, account 123456789012)',
            }
        },
        'RunInstances',
    )

    def build(progress):
        raise error

    runner.start(
        new_record('rocky9', 'arm64', 'n', 'ami-stock', 'operator', False),
        build,
        blocking=True,
    )
    stored = db.get('rocky9', 'arm64')
    assert stored.error.startswith(
        "InvalidParameterValue: The architecture 'x86_64' of the specified instance type"
    )
    assert 'arn:' not in stored.error
    assert '123456789012' not in stored.error
    assert (
        sanitize_aws_message('x arn:aws:s3:::b y 111122223333 z')
        == 'x <arn> y <account> z'
    )
