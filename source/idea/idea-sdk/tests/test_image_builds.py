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


# 26.10.1 pipeline rows


class ConditionTable(FakeTable):
    """evaluates the condition expressions put_if / claim / update_fields build"""

    @staticmethod
    def holds(item, expression, names, values):
        import re

        if not expression:
            return True
        python = expression
        python = re.sub(
            r'attribute_not_exists\(([#\w]+)\)',
            lambda m: f'({m.group(1) if m.group(1).startswith("#") else repr(m.group(1)).join(("item.get(", ")"))} is None)',
            python,
        )
        python = re.sub(r'NOT (#\w+) IN \(([^)]*)\)', r'(\1 not in [\2])', python)
        python = python.replace('<>', '!=').replace(' = ', ' == ')
        python = python.replace(' AND ', ' and ').replace(' OR ', ' or ')
        for placeholder, name in names.items():
            python = python.replace(placeholder, f'item.get({name!r})')
        for placeholder in sorted(values, key=len, reverse=True):
            python = python.replace(placeholder, repr(values[placeholder]))
        return eval(python, {'item': item})

    def put_item(
        self,
        Item,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        key = (Item['base_os'], Item['architecture'])
        if not self.holds(
            self.items.get(key, {}),
            ConditionExpression,
            ExpressionAttributeNames or {},
            ExpressionAttributeValues or {},
        ):
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
            )
        self.items[key] = dict(Item)

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeNames,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
    ):
        key = (Key['base_os'], Key['architecture'])
        item = self.items.setdefault(key, dict(Key))
        values = ExpressionAttributeValues or {}
        if not self.holds(item, ConditionExpression, ExpressionAttributeNames, values):
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'
            )
        for part in (
            UpdateExpression.replace('SET ', '').split(' REMOVE ')[0].split(', ')
        ):
            if '=' in part:
                name, value = (t.strip() for t in part.split('='))
                item[ExpressionAttributeNames[name]] = values[value]
        if ' REMOVE ' in f' {UpdateExpression}':
            for name in UpdateExpression.split('REMOVE ')[1].split(', '):
                item.pop(ExpressionAttributeNames[name.strip()], None)


def pipeline_db() -> ImageBuildRecordsDB:
    db = ImageBuildRecordsDB(context=Mock(), table_name='t', kind='desktop')
    db._table_obj = ConditionTable()
    return db


def test_gpu_and_custom_rows_get_their_own_range_keys_and_round_trip():
    from ideasdk.aws.image_builds import custom_build_architecture, is_custom_record

    db = pipeline_db()
    stamp = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    db.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='current',
            image_id='ami-cpu',
            validated_on=stamp,
            promoted_on=stamp,
            retry_after=stamp,
        )
    )
    db.put(
        ImageBuildRecord(
            base_os='rocky9', architecture='x86_64', variant='nvidia', status='queued'
        )
    )
    db.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture=custom_build_architecture('x86_64'),
            status='complete',
            image_id='ami-custom',
        )
    )

    assert sorted(db._table_obj.items) == [
        ('rocky9', 'x86_64'),
        ('rocky9', 'x86_64#custom'),
        ('rocky9', 'x86_64#nvidia'),
    ]
    cpu = db.get('rocky9', 'x86_64')
    assert (cpu.validated_on, cpu.promoted_on, cpu.retry_after) == (stamp, stamp, stamp)
    assert db._table_obj.items[('rocky9', 'x86_64')]['validated_on'] == int(
        stamp.timestamp() * 1000
    )
    gpu = db.get('rocky9', 'x86_64', 'nvidia')
    assert (gpu.architecture, gpu.variant) == ('x86_64', 'nvidia')
    custom = db.get('rocky9', 'x86_64#custom')
    assert is_custom_record(custom) and custom.image_id == 'ami-custom'
    assert cpu.image_id == 'ami-cpu'  # the custom build never touched the managed row


def test_legacy_records_load_migrated_and_never_pass_the_gate():
    from ideasdk.aws.image_builds import ImageNotValidated, promote_gate

    db = pipeline_db()
    db._table_obj.items[('rocky9', 'x86_64')] = {
        'base_os': 'rocky9',
        'architecture': 'x86_64',
        'status': 'complete',
        'image_id': 'ami-legacy',
        'base_ami': 'ami-stock',
        'update_target': True,
    }
    record = db.get('rocky9', 'x86_64')
    assert (record.status, record.kind, record.variant, record.source_ami) == (
        'current',
        'desktop',
        'cpu',
        'ami-stock',
    )
    assert record.current_image_id == 'ami-legacy'
    with pytest.raises(ImageNotValidated):
        promote_gate(record, 'ami-legacy')


def test_the_promote_gate_accepts_only_validated_candidates_and_promoted_images():
    from ideasdk.aws.image_builds import (
        ImageNotValidated,
        promote_gate,
        validated_image_ids,
    )

    started = datetime(2026, 10, 2, tzinfo=timezone.utc)
    record = ImageBuildRecord(
        base_os='rocky9',
        architecture='x86_64',
        image_id='ami-cand',
        started_on=started,
        current_image_id='ami-cur',
        previous_image_id='ami-prev',
    )
    for image_id in ('ami-cand', 'ami-cur', 'ami-prev', None):
        with pytest.raises(ImageNotValidated):
            promote_gate(record, image_id)
    record.validated_on = started - timedelta(days=1)  # an older run's validation
    with pytest.raises(ImageNotValidated):
        promote_gate(record, 'ami-cand')
    record.validated_on = started + timedelta(hours=1)
    promote_gate(record, 'ami-cand')
    record.promoted_on = started
    promote_gate(record, 'ami-prev')
    with pytest.raises(ImageNotValidated):
        promote_gate(record, 'ami-other')
    assert validated_image_ids([record]) == {'ami-cand', 'ami-cur', 'ami-prev'}


def test_put_if_is_the_queue_claim_and_the_host_fence():
    db = pipeline_db()
    row = ImageBuildRecord(
        base_os='rocky9', architecture='x86_64', status='queued', host='a'
    )
    in_flight = ['queued', 'building']
    assert db.put_if(row, {'status': in_flight}) is True
    assert db.put_if(row, {'status': in_flight}) is False  # already queued
    row.status = 'building'
    assert db.put_if(row, {'host': 'b'}) is False
    assert db.put_if(row, {'host': 'a'}) is True
    fresh = ImageBuildRecord(base_os='rhel9', architecture='x86_64', host='a')
    assert db.put_if(fresh, {'host': None}) is True


def test_claim_refuses_every_in_flight_status_and_a_pinned_row():
    db = pipeline_db()
    for status in (
        'queued',
        'checking',
        'test_launching',
        'promoting',
        'waiting_capacity',
    ):
        db._table_obj.items[('rocky9', 'x86_64')] = {
            'base_os': 'rocky9',
            'architecture': 'x86_64',
            'status': status,
        }
        assert (
            db.claim(
                ImageBuildRecord(
                    base_os='rocky9', architecture='x86_64', status='building'
                )
            )
            is False
        )
    db._table_obj.items[('rocky9', 'x86_64')] = {
        'base_os': 'rocky9',
        'architecture': 'x86_64',
        'status': 'current',
        'pinned': True,
    }
    assert (
        db.claim(
            ImageBuildRecord(base_os='rocky9', architecture='x86_64', status='queued')
        )
        is False
    )
    db._table_obj.items[('rocky9', 'x86_64')]['pinned'] = False
    assert (
        db.claim(
            ImageBuildRecord(base_os='rocky9', architecture='x86_64', status='queued')
        )
        is True
    )
    assert db.get('rocky9', 'x86_64').status == 'queued'


def test_update_fields_sets_only_those_attributes_unless_a_job_holds_the_row():
    db = pipeline_db()
    db.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='building',
            instance_id='i-1',
        )
    )
    row = db.get('rocky9', 'x86_64')
    assert db.update_fields(row, {'pinned': True}) is True
    assert (
        db.update_fields(row, {'status': 'pinned'}, unless_status={'building'}) is False
    )
    stored = db.get('rocky9', 'x86_64')
    assert (stored.pinned, stored.status, stored.instance_id) == (
        True,
        'building',
        'i-1',
    )


def test_resume_goes_to_the_test_launch_when_the_image_exists():
    from ideasdk.aws.image_builds import resume_record

    db = pipeline_db()
    context = Mock()
    context.aws().ec2().describe_images.return_value = {
        'Images': [{'ImageId': 'ami-1', 'State': 'pending'}]
    }
    record = resume_record(
        context,
        db,
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='building',
            ami_name='idea-dcv-host-rocky9-v1',
            instance_id='i-1',
        ),
        Mock(),
    )
    assert (record.status, record.image_id) == ('test_launching', 'ami-1')
    context.aws().ec2().stop_instances.assert_not_called()


# legacy builder image cleanup

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


class Pages:
    def __init__(self, call):
        self.call = call

    def paginate(self, **kwargs):
        return [self.call(**kwargs)]


class LegacyEc2:
    """honors the tag, snapshot, image-id and state filters the legacy sweep sends"""

    def __init__(self):
        self.images = {}
        self.instances = []
        self.templates = {}
        self.deregistered = []
        self.deleted_snapshots = []

    def add(self, image_id, days_old, cluster='idea-test', module='scheduler', **tags):
        tags = {'idea:AmiBuilder': 'true', 'idea:ModuleName': module, **tags}
        if cluster:
            tags['idea:ClusterName'] = cluster
        self.images[image_id] = {
            'ImageId': image_id,
            'Name': f'idea-compute-node-{image_id}',
            'State': 'available',
            'CreationDate': (NOW - timedelta(days=days_old)).strftime(
                '%Y-%m-%dT%H:%M:%S.000Z'
            ),
            'Tags': [{'Key': k, 'Value': v} for k, v in tags.items()],
            'BlockDeviceMappings': [{'Ebs': {'SnapshotId': 'snap-' + image_id}}],
        }
        return self.images[image_id]

    def get_paginator(self, name):
        return Pages(getattr(self, name))

    def describe_images(self, Owners, Filters):
        assert Owners == ['self']
        found = list(self.images.values())
        for f in Filters:
            if f['Name'] == 'block-device-mapping.snapshot-id':
                found = [
                    i
                    for i in found
                    if any(
                        m['Ebs']['SnapshotId'] in f['Values']
                        for m in i['BlockDeviceMappings']
                    )
                ]
            else:
                key = f['Name'][len('tag:') :]
                found = [
                    i
                    for i in found
                    if {t['Key']: t['Value'] for t in i['Tags']}.get(key) in f['Values']
                ]
        return {'Images': found}

    def describe_instances(self, Filters):
        values = {f['Name']: f['Values'] for f in Filters}
        return {
            'Reservations': [
                {
                    'Instances': [
                        i
                        for i in self.instances
                        if i['ImageId'] in values['image-id']
                        and i['State']['Name'] in values['instance-state-name']
                    ]
                }
            ]
        }

    def describe_launch_templates(self):
        return {
            'LaunchTemplates': [
                {'LaunchTemplateId': k, 'LaunchTemplateName': k} for k in self.templates
            ]
        }

    def describe_launch_template_versions(self, LaunchTemplateId):
        return {
            'LaunchTemplateVersions': [
                {'VersionNumber': n, 'LaunchTemplateData': {'ImageId': image_id}}
                for n, image_id in enumerate(self.templates[LaunchTemplateId], 1)
            ]
        }

    def deregister_image(self, ImageId):
        self.deregistered.append(ImageId)
        del self.images[ImageId]

    def delete_snapshot(self, SnapshotId):
        self.deleted_snapshots.append(SnapshotId)


class LegacyDynamo:
    def __init__(self):
        self.tables = {
            'idea-test.cluster-settings': [],
            'idea-test.scheduler.queue-profiles': [],
            'idea-test.scheduler.image-builds': [],
            'idea-test.vdc.controller.software-stacks': [],
            'idea-test.vdc.controller.image-builds': [],
        }
        self.failing = None

    def get_paginator(self, name):
        assert name == 'scan'
        return Pages(self.scan)

    def scan(self, TableName):
        if TableName == self.failing:
            raise ClientError({'Error': {'Code': 'AccessDeniedException'}}, 'Scan')
        if TableName not in self.tables:
            raise ClientError({'Error': {'Code': 'ResourceNotFoundException'}}, 'Scan')
        return {'Items': self.tables[TableName]}


def legacy_context(module='scheduler'):
    ec2, dynamodb = LegacyEc2(), LegacyDynamo()
    context = Mock()
    context.cluster_name.return_value = 'idea-test'
    context.module_name.return_value = module
    context.config.return_value.is_module_enabled.return_value = True
    context.config.return_value.get_module_id.side_effect = lambda name: {
        'scheduler': 'scheduler',
        'virtual-desktop-controller': 'vdc',
    }[name]
    context.aws.return_value.ec2.return_value = ec2
    context.aws.return_value.dynamodb.return_value = dynamodb
    return context, ec2, dynamodb


def sweep(context, days=30, baking=()):
    from ideasdk.aws.image_builds import deregister_legacy_images

    return deregister_legacy_images(context, days, set(baking), Mock(), now=NOW)


def test_legacy_cleanup_takes_only_images_past_the_minimum_age():
    context, ec2, _ = legacy_context()
    ec2.add('ami-old', 31)
    ec2.add('ami-young', 29)
    assert sweep(context, days=0) == []
    assert sweep(context) == ['ami-old']
    assert ec2.deleted_snapshots == ['snap-ami-old']


def _reference(context, ec2, dynamodb, where):
    """name ami-ref in one place the sweep must honor"""
    ddb = lambda **fields: {k: {'S': v} for k, v in fields.items()}  # noqa: E731
    tables = {
        'setting': (
            'idea-test.cluster-settings',
            ddb(key='scheduler.compute_node_ami', value='ami-ref'),
        ),
        'any setting': (
            'idea-test.cluster-settings',
            ddb(key='x.y', value='["ami-ref"]'),
        ),
        'queue profile': (
            'idea-test.scheduler.queue-profiles',
            ddb(name='normal', param_instance_ami='ami-ref'),
        ),
        'compute row current': (
            'idea-test.scheduler.image-builds',
            ddb(base_os='rocky9', current_image_id='ami-ref'),
        ),
        'compute row previous': (
            'idea-test.scheduler.image-builds',
            ddb(base_os='rocky9', previous_image_id='ami-ref'),
        ),
        'compute row candidate': (
            'idea-test.scheduler.image-builds',
            ddb(base_os='rocky9', image_id='ami-ref'),
        ),
        'compute row base': (
            'idea-test.scheduler.image-builds',
            ddb(base_os='rocky9', source_ami='ami-ref'),
        ),
        'base stack': (
            'idea-test.vdc.controller.software-stacks',
            ddb(stack_id='ss-base', base_ami_id='ami-ref'),
        ),
        'custom stack': (
            'idea-test.vdc.controller.software-stacks',
            ddb(stack_id='custom-1', ami_id='ami-ref'),
        ),
        'desktop row': (
            'idea-test.vdc.controller.image-builds',
            ddb(base_os='rocky9', previous_image_id='ami-ref'),
        ),
    }
    if where in tables:
        table, item = tables[where]
        dynamodb.tables[table].append(item)
    elif where == 'stopped instance':
        ec2.instances.append(
            {'InstanceId': 'i-1', 'ImageId': 'ami-ref', 'State': {'Name': 'stopped'}}
        )
    elif where == 'launch template version':
        ec2.templates['lt-1'] = ['ami-other', 'ami-ref', 'ami-latest']
    else:
        raise AssertionError(where)


@pytest.mark.parametrize(
    'where',
    [
        'setting',
        'any setting',
        'queue profile',
        'compute row current',
        'compute row previous',
        'compute row candidate',
        'compute row base',
        'base stack',
        'custom stack',
        'desktop row',
        'stopped instance',
        'launch template version',
    ],
)
def test_legacy_cleanup_keeps_an_image_named_anywhere(where):
    context, ec2, dynamodb = legacy_context()
    ec2.add('ami-ref', 90)
    ec2.add('ami-free', 90)
    _reference(context, ec2, dynamodb, where)
    assert sweep(context) == ['ami-free']


def test_legacy_cleanup_keeps_an_image_still_baking_under_its_name():
    context, ec2, _ = legacy_context()
    ec2.add('ami-baking', 90)
    assert sweep(context, baking={'idea-compute-node-ami-baking'}) == []


def test_legacy_cleanup_ignores_terminated_instances():
    context, ec2, _ = legacy_context()
    ec2.add('ami-gone', 90)
    ec2.instances.append(
        {'InstanceId': 'i-1', 'ImageId': 'ami-gone', 'State': {'Name': 'terminated'}}
    )
    assert sweep(context) == ['ami-gone']


def test_legacy_cleanup_never_touches_another_clusters_or_untagged_or_other_module_images():
    context, ec2, _ = legacy_context()
    ec2.add('ami-other-cluster', 90, cluster='idea-other')
    ec2.add('ami-no-cluster-tag', 90, cluster=None)
    ec2.add('ami-desktop', 90, module='virtual-desktop-controller')
    ec2.add('ami-pipeline', 90, **{'idea:ImagePipeline': 'compute'})
    ec2.add('ami-mine', 90)
    assert sweep(context) == ['ami-mine']


def test_legacy_cleanup_keeps_a_custom_stack_image():
    context, ec2, dynamodb = legacy_context(module='virtual-desktop-controller')
    ec2.add('ami-custom', 400, module='virtual-desktop-controller')
    dynamodb.tables['idea-test.vdc.controller.software-stacks'].append(
        {
            'stack_id': {'S': 'custom-1'},
            'ami_id': {'S': 'ami-custom'},
            'enabled': {'BOOL': False},
        }
    )
    assert sweep(context) == []


def test_legacy_cleanup_deletes_at_most_the_cap_per_sweep_oldest_first():
    from ideasdk.aws.image_builds import LEGACY_MAX_PER_SWEEP

    context, ec2, _ = legacy_context()
    for n in range(LEGACY_MAX_PER_SWEEP + 5):
        ec2.add(f'ami-{n:02d}', 100 + n)
    removed = sweep(context)
    assert len(removed) == LEGACY_MAX_PER_SWEEP
    assert removed[0] == f'ami-{LEGACY_MAX_PER_SWEEP + 4:02d}'
    assert len(sweep(context)) == 5


def test_legacy_cleanup_keeps_a_snapshot_another_image_still_uses():
    context, ec2, dynamodb = legacy_context()
    shared = {'Ebs': {'SnapshotId': 'snap-shared'}}
    ec2.add('ami-gone', 90)['BlockDeviceMappings'].append(shared)
    ec2.add('ami-kept', 90)['BlockDeviceMappings'] = [shared]
    dynamodb.tables['idea-test.cluster-settings'].append({'value': {'S': 'ami-kept'}})
    assert sweep(context) == ['ami-gone']
    assert ec2.deleted_snapshots == ['snap-ami-gone']


def test_legacy_cleanup_deletes_nothing_when_a_reference_source_cannot_be_read():
    context, ec2, dynamodb = legacy_context()
    ec2.add('ami-free', 90)
    dynamodb.failing = 'idea-test.vdc.controller.software-stacks'
    with pytest.raises(ClientError):
        sweep(context)
    assert ec2.deregistered == []


def test_legacy_cleanup_treats_a_module_table_never_created_as_empty():
    context, ec2, dynamodb = legacy_context()
    ec2.add('ami-free', 90)
    del dynamodb.tables['idea-test.vdc.controller.image-builds']
    assert sweep(context) == ['ami-free']


def test_a_legacy_sweep_that_deletes_nothing_still_logs_its_summary():
    from ideasdk.aws.image_builds import deregister_legacy_images

    context, ec2, dynamodb = legacy_context()
    ec2.add('ami-young', 5)
    ec2.add('ami-ref', 90)
    dynamodb.tables['idea-test.cluster-settings'].append({'value': {'S': 'ami-ref'}})
    logger = Mock()
    assert deregister_legacy_images(context, 30, set(), logger, now=NOW) == []
    logger.info.assert_called_once_with(
        'legacy image sweep: 2 candidates, 2 kept (1 referenced, 1 too new, '
        '0 in flight), 0 deleted, capped=no'
    )
    logger = Mock()
    ec2.images.clear()
    deregister_legacy_images(context, 30, set(), logger, now=NOW)
    logger.info.assert_called_once_with(
        'legacy image sweep: 0 candidates, 0 kept (0 referenced, 0 too new, '
        '0 in flight), 0 deleted, capped=no'
    )
