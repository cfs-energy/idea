"""Compute row queuing, validation gates, target guards and leader scheduling."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from ideadatamodel import (
    IMAGE_ROW_IN_FLIGHT,
    ImageBuildRecord,
    ImageCheck,
    ImageRowKey,
    ImageRowFilter,
    RefreshImagesRequest,
    exceptions,
)
from ideascheduler.app.images import compute_images as module
from ideascheduler.app.images.compute_images import ComputeImageService
from test_compute_images import (
    build_service,
    FakeEc2,
    DEFAULT_CONFIG,
    IMAGES,
    SELF_ACCOUNT,
    queue_profile,
)


def row(**kwargs):
    return ImageBuildRecord(
        **{
            'kind': 'compute',
            'base_os': 'rocky9',
            'architecture': 'x86_64',
            'variant': 'cpu',
            'status': 'current',
            'current_image_id': 'ami-old',
            **kwargs,
        }
    )


def service(profiles=()):
    result = build_service(DEFAULT_CONFIG, FakeEc2(), profiles)
    result.records.put(row())
    return result


def test_listing_seeds_both_architectures_and_filters_without_baking():
    svc = service()
    assert len(svc.list_rows()) == len(module.COMPUTE_BASE_OS) * 2
    rows = svc.list_rows(ImageRowFilter(base_os_family='ubuntu', architecture='arm64'))
    assert [(r.base_os, r.architecture) for r in rows] == [
        ('ubuntu2204', 'arm64'),
        ('ubuntu2404', 'arm64'),
    ]
    with pytest.raises(exceptions.SocaException):
        svc.list_rows(ImageRowFilter(kind='desktop'))


@pytest.mark.parametrize('status', sorted(IMAGE_ROW_IN_FLIGHT))
def test_all_in_flight_states_are_idempotent(status):
    svc = service()
    svc.records.put(row(status=status))
    request = RefreshImagesRequest(rows=[row().row_key()])
    assert svc.refresh_images(request)[0].outcome == 'in_flight'
    assert svc.records.get('rocky9', 'x86_64').status == status


def test_button_rebakes_unchanged_row_and_duplicate_keys_queue_once():
    svc = service()
    key = row().row_key()
    results = svc.refresh_images(RefreshImagesRequest(rows=[key, key]))
    assert [r.outcome for r in results] == ['queued', 'in_flight']
    assert results[0].record.trigger == 'button'
    assert results[0].record.attempts == 0
    assert results[0].record.current_image_id == 'ami-old'
    assert results[0].record.validated_on is None


def test_pins_hold_automatic_work_and_explicit_refresh_releases_rollback_hold():
    svc = service()
    svc.records.put(row(pinned=True))
    assert svc.refresh_images(RefreshImagesRequest(all=True))[0].outcome != 'error'
    assert (
        svc.refresh_images(RefreshImagesRequest(rows=[row().row_key()]))[0].outcome
        == 'pinned'
    )
    svc.records.put(row(rollback_hold=True))
    assert svc._enqueue(row(), None, 'monthly').outcome == 'pinned'
    result = svc.refresh_images(RefreshImagesRequest(rows=[row().row_key()]))[0]
    assert result.outcome == 'queued'
    assert result.record.rollback_hold is False


@pytest.mark.parametrize(
    'payload',
    [
        RefreshImagesRequest(),
        RefreshImagesRequest(all=True, rows=[]),
        RefreshImagesRequest(rows=[ImageRowKey(kind='desktop', base_os='rocky9')]),
    ],
)
def test_invalid_refresh_selectors_are_rejected(payload):
    with pytest.raises(exceptions.SocaException):
        service().refresh_images(payload)


def test_failed_canary_cannot_promote(monkeypatch):
    svc = service()
    candidate = row(
        status='test_launching',
        image_id='ami-candidate',
        checks=[ImageCheck(name='kernel_default', ok=True)],
    )
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder, 'wait_for_image', lambda *args: None
    )
    canary = Mock()
    canary.validate.side_effect = exceptions.general_exception(
        'The validation job timed out.'
    )
    monkeypatch.setattr(module, 'ComputeImageCanary', lambda context: canary)
    svc.promote = Mock()
    svc._run_pipeline(candidate)
    assert candidate.status == 'failed'
    assert 'timed out' in candidate.error
    assert candidate.validated_on is None
    assert candidate.current_image_id == 'ami-old'
    assert candidate.checks[-1].ok is False
    svc.promote.assert_not_called()


def test_existing_candidate_resumes_at_canary_without_rebaking(monkeypatch):
    svc = service()
    candidate = row(
        status='test_launching',
        image_id='ami-candidate',
        checks=[ImageCheck(name='kernel_default', ok=True)],
    )
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder, 'wait_for_image', lambda *args: None
    )
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder,
        'build',
        Mock(side_effect=AssertionError('must not bake')),
    )
    canary = Mock()
    monkeypatch.setattr(module, 'ComputeImageCanary', lambda context: canary)
    svc.promote = Mock()
    svc._run_pipeline(candidate)
    canary.validate.assert_called_once()
    svc.promote.assert_called_once_with(candidate)
    assert candidate.validated_on is not None


def decode(values):
    return {key: TypeDeserializer().deserialize(value) for key, value in values.items()}


def own_image(image_id, validated):
    tags = [{'Key': module.VALIDATED_IMAGE_TAG, 'Value': 'r'}] if validated else []
    return {
        'ImageId': image_id,
        'OwnerId': SELF_ACCOUNT,
        'State': 'available',
        'Tags': tags,
    }


def test_promotion_is_one_transaction_with_validation_and_pin_guards(monkeypatch):
    monkeypatch.setitem(IMAGES, 'ami-old', own_image('ami-old', validated=True))
    managed = queue_profile('managed', 'rocky9', 'ami-old')
    managed.queue_profile_id = 'managed'
    pinned = queue_profile('pinned', 'rocky9', 'ami-old')
    pinned.image_pinned = True
    custom = queue_profile('custom', 'rocky9', 'ami-custom')
    svc = service([managed, pinned, custom])
    svc.context.config().values.update({'scheduler.compute_node_ami': 'ami-old'})
    svc.context.aws().ec2().create_tags = Mock()
    candidate = row(
        status='promoting',
        image_id='ami-new',
        release=module.__version__,
        validated_on=datetime.now(timezone.utc),
        checks=[ImageCheck(name='compute_job', ok=True)],
    )
    svc.promote(candidate)
    writes = (
        svc.context.aws()
        .dynamodb()
        .transact_write_items.call_args.kwargs['TransactItems']
    )
    assert len(writes) == 4
    guard = writes[0]['Put']
    assert 'validated_on = :validated' in guard['ConditionExpression']
    assert 'pinned = :false' in guard['ConditionExpression']
    assert 'rollback_hold = :false' in guard['ConditionExpression']
    assert 'current_image_id = :old' in guard['ConditionExpression']
    profile_write = writes[1]['Update']
    assert decode(profile_write['Key']) == {'queue_profile_id': 'managed'}
    assert 'image_pinned = :false' in profile_write['ConditionExpression']
    assert decode(profile_write['ExpressionAttributeValues'])[':old'] == 'ami-old'
    assert candidate.current_image_id == 'ami-new'
    assert candidate.previous_image_id == 'ami-old'
    assert pinned.default_job_params.instance_ami == 'ami-old'
    assert custom.default_job_params.instance_ami == 'ami-custom'


def test_promotion_leaves_targets_on_an_unmanaged_default(monkeypatch):
    # an admin set a custom build as default: the row promotes, the default and the
    # profiles on that image stay where the admin put them
    monkeypatch.setitem(IMAGES, 'ami-old', own_image('ami-old', validated=False))
    profile = queue_profile('managed', 'rocky9', 'ami-old')
    profile.queue_profile_id = 'managed'
    svc = service([profile])
    svc.context.config().values.update({'scheduler.compute_node_ami': 'ami-old'})
    svc.context.aws().ec2().create_tags = Mock()
    candidate = row(
        status='promoting',
        image_id='ami-new',
        release=module.__version__,
        validated_on=datetime.now(timezone.utc),
        checks=[ImageCheck(name='compute_job', ok=True)],
    )
    svc.promote(candidate)
    writes = (
        svc.context.aws()
        .dynamodb()
        .transact_write_items.call_args.kwargs['TransactItems']
    )
    assert [list(w) for w in writes] == [['Put']]
    assert candidate.current_image_id == 'ami-new'


def test_an_unpinned_queue_no_promotion_will_move_is_named():
    """
    dev: every row was current on the release while the arm-compute profile, not pinned,
    still launched an image from two months before. the follow rule only moves a profile
    off the image its row had, so it never catches up and nothing said so.
    """
    from ideadatamodel import HpcQueueProfile, SocaJobParams

    def profile(name, ami, pinned=False):
        return HpcQueueProfile(
            name=name,
            image_pinned=pinned,
            default_job_params=SocaJobParams(base_os='rocky9', instance_ami=ami),
        )

    svc = service(
        [
            profile('normal', 'ami-old'),
            profile('arm-compute', 'ami-left-behind'),
            profile('licensed', 'ami-admins-own', pinned=True),
            profile('iv-0123456789ab', 'ami-candidate'),
        ]
    )
    expected = ['queue profile arm-compute (ami-left-behind)']
    assert svc.targets_off_current() == expected
    assert svc.targets_off_current() == expected
    warnings = [c.args[0] for c in svc._logger.warning.call_args_list]
    assert len(warnings) == 1 and 'arm-compute (ami-left-behind)' in warnings[0]
    # the scheduler default is a target too, unless it is pinned
    svc.context.config.return_value.values['scheduler.compute_node_ami'] = 'ami-def'
    assert svc.targets_off_current() == expected + ['scheduler default (ami-def)']
    svc.context.config.return_value.values['scheduler.images.default_image_pinned'] = (
        True
    )
    assert svc.targets_off_current() == expected


def test_promotion_does_not_advance_the_row_when_the_current_image_cannot_be_read(
    monkeypatch,
):
    """throttled describe: the row stays on its image, so its queues still match later"""
    monkeypatch.setitem(IMAGES, 'ami-old', own_image('ami-old', validated=True))
    profile = queue_profile('managed', 'rocky9', 'ami-old')
    profile.queue_profile_id = 'managed'
    svc = service([profile])
    svc.context.config().values.update({'scheduler.compute_node_ami': 'ami-old'})
    ec2 = svc.context.aws().ec2()
    ec2.create_tags = Mock()

    def throttled(**kwargs):
        raise ClientError({'Error': {'Code': 'RequestLimitExceeded'}}, 'DescribeImages')

    monkeypatch.setattr(ec2, 'describe_images', throttled)
    candidate = row(
        status='promoting',
        image_id='ami-new',
        release=module.__version__,
        validated_on=datetime.now(timezone.utc),
        checks=[ImageCheck(name='compute_job', ok=True)],
    )
    with pytest.raises(ClientError):
        svc.promote(candidate)
    svc.context.aws().dynamodb().transact_write_items.assert_not_called()
    assert candidate.current_image_id == 'ami-old'


def test_transaction_failure_never_updates_in_memory_targets():
    profile = queue_profile('managed', 'rocky9', 'ami-old')
    profile.queue_profile_id = 'managed'
    svc = service([profile])
    svc.context.aws().ec2().create_tags = Mock()
    svc.context.aws().dynamodb().transact_write_items.side_effect = ClientError(
        {'Error': {'Code': 'TransactionCanceledException'}}, 'TransactWriteItems'
    )
    candidate = row(
        status='promoting',
        image_id='ami-new',
        validated_on=datetime.now(timezone.utc),
        checks=[ImageCheck(name='compute_job', ok=True)],
        release=module.__version__,
    )
    with pytest.raises(ClientError):
        svc.promote(candidate)
    assert profile.default_job_params.instance_ami == 'ami-old'
    assert candidate.current_image_id == 'ami-old'


def test_legacy_unvalidated_image_fails_shared_promote_gate():
    svc = service()
    with pytest.raises(Exception, match='has not passed validation'):
        svc.promote(row(image_id='ami-old', status='complete'))
    svc.context.aws().dynamodb().transact_write_items.assert_not_called()


def test_rollback_requires_previous_validation_and_holds_future_promotion(monkeypatch):
    svc = service()
    svc.records.put(
        row(previous_image_id='ami-before', validated_on=datetime.now(timezone.utc))
    )
    monkeypatch.setattr(
        module,
        'describe_images_by_id',
        lambda *args: {
            'ami-before': {
                'State': 'available',
                'Tags': [{'Key': 'idea:ComputeImageValidated', 'Value': 'release'}],
            }
        },
    )
    result = svc.rollback_image(row().row_key())
    assert result.current_image_id == 'ami-before'
    assert result.previous_image_id == 'ami-old'
    assert result.rollback_hold is True
    assert (
        'previous_image_id = :image'
        in svc.context.aws()
        .dynamodb()
        .transact_write_items.call_args.kwargs['TransactItems'][0]['Put'][
            'ConditionExpression'
        ]
    )


@pytest.mark.parametrize('status', ['current', 'failed'])
def test_rollback_from_an_idle_row_guards_on_the_status_it_read(monkeypatch, status):
    """a refresh that failed keeps current + previous; rollback works from that row too"""
    svc = service()
    svc.records.put(
        row(
            status=status,
            image_id='ami-failed-candidate',
            previous_image_id='ami-before',
            promoted_on=datetime.now(timezone.utc),
        )
    )
    monkeypatch.setattr(
        module,
        'describe_images_by_id',
        lambda *args: {
            'ami-before': {
                'State': 'available',
                'Tags': [{'Key': 'idea:ComputeImageValidated', 'Value': 'release'}],
            }
        },
    )
    svc.rollback_image(row().row_key())
    put = (
        svc.context.aws()
        .dynamodb()
        .transact_write_items.call_args.kwargs['TransactItems'][0]['Put']
    )
    condition = put['ConditionExpression']
    values = decode(put['ExpressionAttributeValues'])
    # the stored row matches the condition only if #s is compared with what it holds
    placeholder = condition.split('#s = ')[1].split()[0]
    assert values[placeholder] == status
    assert values[':image'] == 'ami-before'
    assert 'pinned = :false' in condition


@pytest.mark.parametrize('status', ['building', 'pinned', 'unsupported'])
def test_rollback_refuses_a_row_that_is_not_idle(monkeypatch, status):
    svc = service()
    svc.records.put(row(status=status, previous_image_id='ami-before'))
    with pytest.raises(Exception, match='idle, unpinned row'):
        svc.rollback_image(row().row_key())
    svc.context.aws().dynamodb().transact_write_items.assert_not_called()


def test_monthly_rebakes_unchanged_rows_and_release_still_needs_a_change(
    monkeypatch,
):
    svc = service()
    svc.context.config().values['scheduler.images.refreshed_release'] = (
        module.__version__
    )
    unchanged = row(source_ami='ami-stock', release=module.__version__)
    changed = row(
        base_os='ubuntu2404', source_ami='ami-obsolete', release=module.__version__
    )
    svc.list_rows = lambda: [unchanged, changed]
    monkeypatch.setattr(module, 'find_latest_stock_ami', lambda *args: 'ami-stock')
    svc._enqueue = Mock()
    svc._setting = Mock()
    svc.tick(datetime(2026, 10, 4, 2, tzinfo=timezone.utc))
    # repositories move between vendor images: an unchanged row is baked too
    assert svc._enqueue.call_count == 2
    svc._enqueue.assert_any_call(unchanged, None, 'monthly')
    svc._enqueue.assert_any_call(changed, None, 'monthly')
    svc._enqueue.reset_mock()
    # not due again this month: nothing unchanged is baked on an ordinary tick
    svc.tick(datetime(2026, 10, 4, 3, tzinfo=timezone.utc))
    svc._enqueue.assert_not_called()
    svc.context.config().values[
        'virtual-desktop-controller.software_stacks.image_refresh_schedule'
    ] = {'enabled': False}
    svc.context.config().values['scheduler.images.refreshed_release'] = 'old'
    unchanged.release = 'old'
    svc.tick(datetime(2026, 10, 4, 2, tzinfo=timezone.utc))
    assert svc._enqueue.call_count == 2
    svc._enqueue.assert_any_call(unchanged, None, 'release')


def test_monthly_waits_for_a_bake_in_flight_without_claiming_the_month(monkeypatch):
    svc = service()
    svc.context.config().values['scheduler.images.refreshed_release'] = (
        module.__version__
    )
    idle = row(source_ami='ami-stock', release=module.__version__)
    baking = row(
        base_os='ubuntu2404',
        status='test_launching',
        source_ami='ami-stock',
        release=module.__version__,
    )
    rows = [idle, baking]
    svc.list_rows = lambda: rows
    monkeypatch.setattr(module, 'find_latest_stock_ami', lambda *args: 'ami-stock')
    svc._enqueue = Mock()
    svc.tick(datetime(2026, 10, 4, 2, tzinfo=timezone.utc))
    svc._enqueue.assert_not_called()
    entries = svc.context.config().db.entries
    assert 'scheduler.images.image_refresh_last_run_on' not in entries
    baking.status = 'current'
    svc.tick(datetime(2026, 10, 4, 2, 30, tzinfo=timezone.utc))
    assert svc._enqueue.call_count == 2


def test_the_monthly_check_runs_once_when_the_settings_copy_lags(monkeypatch):
    # the scheduler's copy of last-run lags its own write; a second tick 15 s later
    # still reads the old value and must not check (and rebake) again
    svc = service()
    svc.context.config().values['scheduler.images.refreshed_release'] = (
        module.__version__
    )
    changed = row(source_ami='ami-obsolete', release=module.__version__)
    svc.list_rows = lambda: [changed]
    monkeypatch.setattr(module, 'find_latest_stock_ami', lambda *args: 'ami-stock')
    svc._enqueue = Mock()
    now = datetime(2026, 10, 4, 2, tzinfo=timezone.utc)
    svc.tick(now)
    svc.tick(now.replace(second=15))
    svc._enqueue.assert_called_once_with(changed, None, 'monthly')
    assert svc.context.config().db.entries[
        'scheduler.images.image_refresh_last_run_on'
    ] == int(now.timestamp() * 1000)


def test_a_resumed_candidate_terminates_its_builder(monkeypatch):
    svc = service()
    candidate = row(
        status='test_launching',
        image_id='ami-candidate',
        instance_id='i-builder',
        checks=[ImageCheck(name='kernel_default', ok=True)],
    )
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder, 'wait_for_image', lambda *args: None
    )
    monkeypatch.setattr(module, 'ComputeImageCanary', lambda context: Mock())
    ec2 = Mock()
    svc.context.aws.return_value.ec2.return_value = ec2
    svc.promote = Mock()
    svc._run_pipeline(candidate)
    ec2.terminate_instances.assert_called_once_with(InstanceIds=['i-builder'])
    svc.promote.assert_called_once_with(candidate)


def test_nonleader_does_no_work():
    svc = service()
    svc.context.is_leader.return_value = False
    svc.list_rows = Mock(side_effect=AssertionError('must not list'))
    svc.tick()


def test_build_api_ignores_configured_and_explicit_stale_source(monkeypatch):
    from ideadatamodel import BuildComputeImageRequest
    from test_compute_images import BUILDER_CONFIG

    svc = service()
    svc.context.config().values.update(BUILDER_CONFIG)
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-rocky9stock00001'
    )
    svc.run_build = lambda builder, **kwargs: builder
    builder = svc.build(
        BuildComputeImageRequest(base_os='rocky9', base_ami='ami-foreignpublic001'),
        'operator',
    )
    assert builder.base_ami == 'ami-rocky9stock00001'


def test_partial_progress_does_not_overwrite_concurrent_pin_or_promoted_target():
    svc = service()
    candidate = row(
        status='checking', started_on=datetime.now(timezone.utc), pinned=False
    )
    ComputeImageService._save(svc, candidate)
    update = svc.table.update_item.call_args.kwargs
    touched = set(update['ExpressionAttributeNames'].values())
    assert (
        not {'pinned', 'rollback_hold', 'current_image_id', 'previous_image_id'}
        & touched
    )
    condition = update['ConditionExpression']
    assert condition.startswith('started_on = :started AND #row_status IN (')
    allowed = {
        update['ExpressionAttributeValues'][name]
        for name in condition.split('IN (')[1].rstrip(')').split(', ')
    }
    assert allowed == set(IMAGE_ROW_IN_FLIGHT)


def test_pinned_scheduler_default_is_not_in_promotion_transaction():
    svc = service()
    svc.context.config().values.update(
        {
            'scheduler.compute_node_ami': 'ami-old',
            'scheduler.images.default_image_pinned': True,
        }
    )
    svc.context.aws().ec2().create_tags = Mock()
    candidate = row(
        status='promoting',
        image_id='ami-new',
        validated_on=datetime.now(timezone.utc),
        checks=[ImageCheck(name='compute_job', ok=True)],
        release=module.__version__,
    )
    svc.promote(candidate)
    assert (
        len(
            svc.context.aws()
            .dynamodb()
            .transact_write_items.call_args.kwargs['TransactItems']
        )
        == 1
    )


def test_pending_rows_wait_for_bake_capacity():
    svc = service()
    svc.context.config().values.update(
        {
            'scheduler.images.refreshed_release': module.__version__,
            'scheduler.images': {'max_concurrent_bakes': 1},
            'virtual-desktop-controller.software_stacks.image_refresh_schedule': {
                'enabled': False
            },
        }
    )
    svc.records.put(row(status='queued'))
    thread = Mock()
    thread.is_alive.return_value = True
    svc._live[('ubuntu2404', 'x86_64')] = thread
    svc._start_row = Mock()
    svc.tick()
    svc._start_row.assert_not_called()
    assert svc.records.get('rocky9', 'x86_64').status == 'queued'


def test_release_checkpoint_waits_for_an_older_in_flight_release():
    svc = service()
    svc.list_rows = lambda: [row(status='building', release='older')]
    svc.context.config().values[
        'virtual-desktop-controller.software_stacks.image_refresh_schedule'
    ] = {'enabled': False}
    svc._setting = Mock()
    svc.tick()
    svc._setting.assert_not_called()


@pytest.mark.parametrize(
    'method,request_type,response_field',
    [
        ('list_image_rows', 'ListImageRowsRequest', 'listing'),
        ('refresh_images', 'RefreshImagesRequest', 'results'),
        ('rollback_image', 'RollbackImageRequest', 'record'),
        ('set_image_pinned', 'SetImagePinnedRequest', 'record'),
    ],
)
def test_scheduler_pipeline_api_bodies_return_the_contract_payload(
    method, request_type, response_field
):
    import ideadatamodel
    from ideascheduler.app.api.scheduler_admin_api import SchedulerAdminAPI

    api = SchedulerAdminAPI.__new__(SchedulerAdminAPI)
    api._compute_images = Mock()
    api._compute_images.list_rows.return_value = [row()]
    api._compute_images.refresh_images.return_value = []
    api._compute_images.rollback_image.return_value = row()
    api._compute_images.set_image_pinned.return_value = row(pinned=True)
    invocation = Mock()
    invocation.get_request_payload_as.return_value = getattr(
        ideadatamodel, request_type
    )(row=row().row_key(), pinned=True, all=True)
    getattr(api, method)(invocation)
    invocation.success.assert_called_once()
    assert getattr(invocation.success.call_args.args[0], response_field) is not None


def test_builder_sweep_terminates_only_orphaned_builders_past_any_real_bake(
    monkeypatch,
):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    svc = service()
    svc.records.put(row(status='building', instance_id='i-live'))
    ec2 = Mock()
    ec2.describe_instances.return_value = {
        'Reservations': [
            {
                'Instances': [
                    {'InstanceId': 'i-live', 'LaunchTime': now - timedelta(hours=5)},
                    {
                        'InstanceId': 'i-young',
                        'LaunchTime': now - timedelta(minutes=30),
                    },
                    {'InstanceId': 'i-orphan', 'LaunchTime': now - timedelta(hours=5)},
                ]
            }
        ]
    }
    svc.context.aws.return_value.ec2.return_value = ec2
    terminated = []
    monkeypatch.setattr(
        module,
        'terminate_builder',
        lambda context, instance_id, logger: terminated.append(instance_id),
    )
    monkeypatch.setattr(module, 'terminate_old_stopped_builders', lambda *a: [])
    svc.sweep_builders(now)
    assert terminated == ['i-orphan']


def test_builder_sweep_reaps_validation_queues_no_row_is_validating(monkeypatch):
    from ideadatamodel import HpcQueueProfile, SocaJobParams

    svc = service()
    svc.records.put(row(status='test_launching', image_id='ami-live'))
    ec2 = Mock()
    ec2.describe_instances.return_value = {}
    svc.context.aws.return_value.ec2.return_value = ec2
    monkeypatch.setattr(module, 'terminate_old_stopped_builders', lambda *a: [])
    svc.context.queue_profiles.list_queue_profiles.return_value = [
        HpcQueueProfile(
            name='iv-live', default_job_params=SocaJobParams(instance_ami='ami-live')
        ),
        HpcQueueProfile(
            name='iv-left', default_job_params=SocaJobParams(instance_ami='ami-old')
        ),
        HpcQueueProfile(
            name='normal', default_job_params=SocaJobParams(instance_ami='ami-old')
        ),
    ]
    reaped = []
    monkeypatch.setattr(
        module.ComputeImageCanary,
        'reap',
        lambda self, p, patience: reaped.append((p.name, patience)) or [],
    )
    svc._last_sweep = before = module.time.monotonic()
    svc.sweep_builders(datetime.now(timezone.utc))
    # without waiting: the sweep runs on the thread that enforces the bake time limit
    assert reaped == [('iv-left', 0)]
    assert svc._last_sweep == before


def test_a_validation_queue_pbs_still_holds_is_asked_for_again_within_a_minute(
    monkeypatch,
):
    """
    the sweep waited up to 180 s on the leader's tick for each busy queue, so a 3 minute
    bake limit fired 3 min 10 s late.
    """
    from ideadatamodel import HpcQueueProfile, SocaJobParams

    svc = service()
    ec2 = Mock()
    ec2.describe_instances.return_value = {}
    svc.context.aws.return_value.ec2.return_value = ec2
    monkeypatch.setattr(module, 'terminate_old_stopped_builders', lambda *a: [])
    svc.context.queue_profiles.list_queue_profiles.return_value = [
        HpcQueueProfile(
            name='iv-busy', default_job_params=SocaJobParams(instance_ami='ami-old')
        ),
    ]
    answers = [['The validation queue iv-busy still has a node in PBS.']] * 2 + [[]]
    monkeypatch.setattr(
        module.ComputeImageCanary, 'reap', lambda self, p, patience: answers.pop(0)
    )
    now = datetime.now(timezone.utc)

    def due_in():
        return module.SWEEP_SECONDS - (module.time.monotonic() - svc._last_sweep)

    svc._last_sweep = module.time.monotonic()
    svc.sweep_builders(now)
    assert 55 < due_in() <= module.SWEEP_RETRY_SECONDS
    svc.sweep_builders(now)
    assert 55 < due_in() <= module.SWEEP_RETRY_SECONDS
    # said once, not on every retry
    warnings = [c.args[0] for c in svc._logger.warning.call_args_list]
    assert [w for w in warnings if 'iv-busy' in w] == [
        'validation queue iv-busy not reaped yet: '
        'The validation queue iv-busy still has a node in PBS.'
    ]
    svc._last_sweep = module.time.monotonic()
    svc.sweep_builders(now)
    assert due_in() > module.SWEEP_SECONDS - 5


def _capacity_instance(queue, stack='idea-stack', running=True):
    return Mock(
        instance_id='i-capacity',
        soca_job_queue=queue,
        soca_compute_stack=stack,
        is_running=running,
    )


def test_builder_sweep_removes_capacity_a_reaped_validation_queue_left_behind(
    monkeypatch,
):
    svc = service()
    ec2 = Mock()
    ec2.describe_instances.return_value = {}
    svc.context.aws.return_value.ec2.return_value = ec2
    monkeypatch.setattr(module, 'terminate_old_stopped_builders', lambda *a: [])
    svc.context.queue_profiles.list_queue_profiles.return_value = []
    svc.context.instance_cache.list_compute_instances.return_value = [
        _capacity_instance('iv-gone', 'stack-gone'),
        _capacity_instance('iv-live', 'stack-live'),
        _capacity_instance('normal', 'stack-normal'),
        _capacity_instance('iv-stopped', 'stack-stopped', running=False),
        _capacity_instance('iv-nostack', None),
    ]
    live = {'iv-live'}
    svc.context.scheduler.get_queue.side_effect = lambda queue: (
        Mock() if queue in live else None
    )
    delete = svc.context.aws_util.return_value.cloudformation_delete_stack
    svc.sweep_builders(datetime.now(timezone.utc))
    delete.assert_called_once_with(stack_name='stack-gone')


def test_a_failed_capacity_lookup_does_not_stop_the_builder_sweep(monkeypatch):
    svc = service()
    ec2 = Mock()
    ec2.describe_instances.return_value = {}
    svc.context.aws.return_value.ec2.return_value = ec2
    terminated = []
    monkeypatch.setattr(
        module,
        'terminate_old_stopped_builders',
        lambda *a: terminated.append(True) or [],
    )
    svc.context.queue_profiles.list_queue_profiles.return_value = []
    svc.context.instance_cache.list_compute_instances.side_effect = RuntimeError('x')
    svc.sweep_builders(datetime.now(timezone.utc))
    assert terminated == [True]
    svc.context.aws_util.return_value.cloudformation_delete_stack.assert_not_called()


def test_sweep_removes_failed_candidates_and_generations_older_than_previous(
    monkeypatch,
):
    """
    three promotions and a failed canary: only the first generation and the failed
    candidate go, each with its snapshot; current, previous, an in-flight bake, a queue's
    image, the scheduler default, an image a node still runs and an image a launch
    template or a setting names stay
    """
    from ideadatamodel import HpcQueueProfile, SocaJobParams

    svc = service()
    svc.records.put(row(current_image_id='ami-gen3', previous_image_id='ami-gen2'))
    svc.records.put(
        row(
            base_os='rocky8',
            status='failed',
            image_id='ami-fail',
            current_image_id=None,
        )
    )
    svc.records.put(
        row(
            base_os='rhel9',
            status='building',
            ami_name='idea-compute-node-rhel9-baking',
            current_image_id=None,
        )
    )
    svc.context.queue_profiles.list_queue_profiles.return_value = [
        HpcQueueProfile(
            name='normal', default_job_params=SocaJobParams(instance_ami='ami-queue')
        )
    ]
    names = {
        'ami-gen1': 'g1',
        'ami-gen2': 'g2',
        'ami-gen3': 'g3',
        'ami-fail': 'f',
        'ami-queue': 'q',
        'ami-inuse': 'u',
        'ami-baked': 'rhel9-baking',
        'ami-template': 't',
        'ami-setting': 's',
    }
    images = [
        {
            'ImageId': i,
            'Name': f'idea-compute-node-{n}',
            'State': 'available',
            'BlockDeviceMappings': [{'Ebs': {'SnapshotId': 'snap-' + i}}],
        }
        for i, n in names.items()
    ]
    deregistered, deleted = [], []

    class Ec2:
        def describe_images(self, **kwargs):
            for f in kwargs.get('Filters', []):
                if f['Name'] == 'block-device-mapping.snapshot-id':
                    snap = f['Values'][0]
                    return {
                        'Images': [
                            i
                            for i in images
                            if i['BlockDeviceMappings'][0]['Ebs']['SnapshotId'] == snap
                        ]
                    }
            assert {'Name': 'tag:idea:ImagePipeline', 'Values': ['compute']} in kwargs[
                'Filters'
            ]
            return {'Images': images}

        def describe_instances(self, **kwargs):
            ids = next(
                (
                    f['Values']
                    for f in kwargs.get('Filters', [])
                    if f['Name'] == 'image-id'
                ),
                [],
            )
            running = [{'ImageId': 'ami-inuse'}] if 'ami-inuse' in ids else []
            return {'Reservations': [{'Instances': running}]}

        def get_paginator(self, name):
            pages = {
                'describe_instances': lambda **kw: [self.describe_instances(**kw)],
                'describe_launch_templates': lambda **kw: [
                    {'LaunchTemplates': [{'LaunchTemplateId': 'lt-1'}]}
                ],
                'describe_launch_template_versions': lambda **kw: [
                    {
                        'LaunchTemplateVersions': [
                            {'LaunchTemplateData': {'ImageId': 'ami-template'}}
                        ]
                    }
                ],
            }
            return Mock(paginate=pages[name])

        def deregister_image(self, ImageId):
            deregistered.append(ImageId)

        def delete_snapshot(self, SnapshotId):
            deleted.append(SnapshotId)

    svc.context.aws.return_value.ec2.return_value = Ec2()
    tables = {
        'test-cluster.cluster-settings': [{'value': {'S': 'ami-setting'}}],
        # the failed candidate's own row names it; image rows do not keep an image
        'test-cluster.scheduler.image-builds': [{'image_id': {'S': 'ami-fail'}}],
    }
    unreadable = set()

    def scan(TableName):
        if TableName in unreadable:
            raise RuntimeError(f'cannot scan {TableName}')
        return [{'Items': tables.get(TableName, [])}]

    svc.context.aws.return_value.dynamodb.return_value.get_paginator.return_value = (
        Mock(paginate=scan)
    )
    svc.context.config().values['scheduler.compute_node_ami'] = 'ami-gen3'
    monkeypatch.setattr(module, 'terminate_old_stopped_builders', lambda *a: [])
    monkeypatch.setattr(module, 'deregister_legacy_images', lambda *a: [])
    unreadable.add('test-cluster.scheduler.queue-profiles')
    svc.sweep_builders(datetime.now(timezone.utc))
    assert deregistered == []  # a reference that cannot be read keeps everything
    unreadable.clear()
    svc.sweep_builders(datetime.now(timezone.utc))
    assert sorted(deregistered) == ['ami-fail', 'ami-gen1']
    assert sorted(deleted) == ['snap-ami-fail', 'snap-ami-gen1']


def test_pipeline_compute_images_carry_the_cleanup_tag():
    from ideascheduler.app.images.compute_node_ami_builder import (
        ComputeNodeAmiBuilder,
    )

    builder = ComputeNodeAmiBuilder.__new__(ComputeNodeAmiBuilder)
    builder.context = Mock()
    builder.context.config.return_value.get_list.return_value = []
    builder.context.cluster_name.return_value = 'cluster'
    builder.image_tags = {'idea:ImagePipeline': 'compute'}
    builder.base_os, builder.ami_name, builder.ami_version = 'rocky9', 'n', 'v'
    builder.ebs_volume_size, builder.no_reboot = 20, False
    builder.context.aws().ec2().create_image.return_value = {'ImageId': 'ami-new'}
    assert builder.create_image('i-builder') == 'ami-new'
    specs = (
        builder.context.aws().ec2().create_image.call_args.kwargs['TagSpecifications']
    )
    for spec in specs:
        assert {'Key': 'idea:ImagePipeline', 'Value': 'compute'} in spec['Tags']


@pytest.mark.parametrize('status', ['current', 'failed'])
def test_a_row_baked_today_is_skipped_by_the_button_unless_forced(status):
    from datetime import timedelta

    svc = service()
    now = datetime.now(timezone.utc)
    svc.records.put(row(status=status, started_on=now))
    request = RefreshImagesRequest(rows=[row().row_key()])
    result = svc.refresh_images(request)[0]
    assert result.outcome == 'baked_today' and 'Force rebake' in result.message
    assert svc.records.get('rocky9', 'x86_64').status == status
    forced = svc.refresh_images(
        RefreshImagesRequest(rows=[row().row_key()], force=True)
    )
    assert forced[0].outcome == 'queued'

    svc = service()
    svc.records.put(row(status=status, started_on=now - timedelta(days=1)))
    assert svc.refresh_images(request)[0].outcome == 'queued'


def test_the_release_trigger_waits_a_day_for_a_row_baked_today():
    svc = service()
    svc.context.config().values[
        'virtual-desktop-controller.software_stacks.image_refresh_schedule'
    ] = {'enabled': False}
    today = datetime(2026, 10, 4, 2, tzinfo=timezone.utc)
    baked = row(release='old', source_ami='ami-stock', started_on=today)
    svc.list_rows = lambda: [baked]
    svc.default_base_ami = Mock(return_value='ami-stock')
    svc._enqueue = Mock()
    svc._setting = Mock()
    svc.tick(today)
    svc._enqueue.assert_not_called()
    svc._setting.assert_not_called()  # the release is not marked done
    svc.tick(datetime(2026, 10, 5, 0, 1, tzinfo=timezone.utc))
    svc._enqueue.assert_called_once_with(baked, None, 'release')


def test_sweep_cleans_legacy_compute_images_with_the_setting_and_bakes_in_flight(
    monkeypatch,
):
    svc = service()
    svc.records.put(
        row(
            base_os='rhel9',
            status='building',
            ami_name='idea-compute-node-rhel9-baking',
        )
    )
    calls = []
    monkeypatch.setattr(
        module,
        'deregister_legacy_images',
        lambda context, days, baking, logger: calls.append((days, baking)),
    )
    monkeypatch.setattr(module, 'terminate_old_stopped_builders', lambda *a: [])
    monkeypatch.setattr(svc, 'deregister_unreferenced_images', lambda: [])
    svc.context.queue_profiles.list_queue_profiles.return_value = []
    svc.context.aws().ec2().describe_instances = lambda **kwargs: {}
    svc.context.config().values['scheduler.images.legacy_cleanup_min_age_days'] = 0
    svc.sweep_builders(datetime.now(timezone.utc))
    assert calls == [(0, {'idea-compute-node-rhel9-baking'})]


# the bake time limit

LIMIT_NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


def stuck_row(**kwargs):
    started_on = kwargs.get('started_on', LIMIT_NOW - timedelta(hours=24))
    return row(
        **{
            'status': 'test_launching',
            'image_id': 'ami-candidate',
            'instance_id': 'i-builder',
            'release': '26.11.1',
            'started_on': started_on,
            'attempt_started_on': started_on,
            **kwargs,
        }
    )


def test_a_row_with_no_clock_gets_one_and_no_failure():
    # hours in the queue behind other rows, then the upgrade replaced the task
    svc = service()
    svc.records.put(stuck_row(attempt_started_on=None))
    svc._enforce_time_limit(LIMIT_NOW)
    stored = svc.records.get('rocky9', 'x86_64')
    assert stored.status == 'test_launching'
    assert stored.attempt_started_on == LIMIT_NOW
    # and that clock still ends a bake that is really stuck
    svc._enforce_time_limit(LIMIT_NOW + timedelta(hours=2, minutes=1))
    assert svc.records.get('rocky9', 'x86_64').status == 'failed'


@pytest.mark.parametrize(
    'status', ['resolving', 'building', 'checking', 'test_launching', 'promoting']
)
def test_a_row_past_the_limit_fails_instead_of_resuming(status):
    svc = service()
    svc.records.put(stuck_row(status=status))
    svc._start_row = Mock()
    svc._enforce_time_limit(LIMIT_NOW)
    stored = svc.records.get('rocky9', 'x86_64')
    assert stored.status == 'failed'
    assert '120 minute limit' in stored.error and status in stored.error
    assert stored.finished_on == LIMIT_NOW
    assert stored.checks[-1].name == 'time_limit' and stored.checks[-1].ok is False
    svc.tick(LIMIT_NOW)
    svc._start_row.assert_not_called()


def test_a_hung_worker_past_the_limit_gives_its_slot_back():
    svc = service()
    svc.records.put(stuck_row())
    svc._live[('rocky9', 'x86_64')] = Mock(is_alive=lambda: True)
    svc._enforce_time_limit(LIMIT_NOW)
    assert ('rocky9', 'x86_64') not in svc._live


def test_a_hung_worker_cannot_fill_the_bake_slots_of_other_rows():
    svc = service()
    svc.context.config().values['scheduler.images'] = {'max_concurrent_bakes': 1}
    svc.records.put(stuck_row())
    healthy = row(
        base_os='ubuntu2404',
        status='queued',
        release=module.__version__,
        started_on=LIMIT_NOW,
    )
    svc.records.put(healthy)
    svc._live[('rocky9', 'x86_64')] = Mock(is_alive=lambda: True)
    svc._start_row = Mock()
    svc.list_rows = lambda: []
    svc._setting = Mock()
    svc.tick(LIMIT_NOW)
    assert [c.args[0].base_os for c in svc._start_row.call_args_list] == ['ubuntu2404']


def test_the_limit_leaves_a_healthy_bake_and_a_queued_row_alone():
    svc = service()
    # the longest real compute bake took 41 minutes
    svc.records.put(stuck_row(started_on=LIMIT_NOW - timedelta(minutes=55)))
    svc._enforce_time_limit(LIMIT_NOW)
    assert svc.records.get('rocky9', 'x86_64').status == 'test_launching'
    svc.records.put(stuck_row(status='queued'))
    svc._enforce_time_limit(LIMIT_NOW)
    assert svc.records.get('rocky9', 'x86_64').status == 'queued'


def test_the_limit_is_read_from_the_pipeline_setting():
    svc = service()
    svc.context.config().values[module.PIPELINE_SETTINGS] = {
        'bake_time_limit_minutes': 30
    }
    svc.records.put(stuck_row(started_on=LIMIT_NOW - timedelta(minutes=31)))
    svc._enforce_time_limit(LIMIT_NOW)
    stored = svc.records.get('rocky9', 'x86_64')
    assert stored.status == 'failed' and '30 minute limit' in stored.error


def test_a_custom_build_is_not_the_pipelines_to_time_out():
    svc = service()
    custom = stuck_row(architecture='x86_64#custom')
    svc.records.put(custom)
    svc._enforce_time_limit(LIMIT_NOW)
    assert svc.records.get('rocky9', 'x86_64#custom').status == 'test_launching'


def conditional_failure():
    return ClientError(
        {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'no'}},
        'UpdateItem',
    )


def test_a_worker_that_wakes_after_the_limit_cannot_bring_its_row_back(monkeypatch):
    svc = service()
    table = Mock()
    table.update_item.side_effect = conditional_failure()
    monkeypatch.setattr(ComputeImageService, 'table', table)
    candidate = row(status='checking', started_on=LIMIT_NOW)
    with pytest.raises(module.RowMoved):
        ComputeImageService._save(svc, candidate)
    # the worker stops quietly, without writing a second time
    svc._save = lambda record: ComputeImageService._save(svc, record)
    table.update_item.reset_mock()
    svc._run_pipeline(row(status='test_launching', started_on=LIMIT_NOW, image_id=None))
    assert table.update_item.call_count == 1
    svc._logger.exception.assert_not_called()


# failing disposes what the bake made


def builder_ec2(tags):
    ec2 = FakeEc2()
    ec2.terminate_instances = Mock()
    ec2.describe_instances = Mock(
        return_value={
            'Reservations': [{'Instances': [{'InstanceId': 'i-builder', 'Tags': tags}]}]
        }
    )
    return ec2


OWN_BUILDER_TAGS = [
    {'Key': 'idea:ImageBuild', 'Value': 'rocky9/x86_64'},
    {'Key': 'idea:ModuleId', 'Value': 'scheduler'},
]


def test_the_time_limit_terminates_the_builder_and_brings_the_sweep_forward():
    svc = service()
    ec2 = builder_ec2(OWN_BUILDER_TAGS)
    svc.context.aws.return_value.ec2.return_value = ec2
    svc.records.put(stuck_row())
    svc._enforce_time_limit(LIMIT_NOW)
    ec2.terminate_instances.assert_called_once_with(InstanceIds=['i-builder'])
    assert svc._last_sweep == float('-inf')


@pytest.mark.parametrize(
    'tags',
    [
        [],
        [
            {'Key': 'idea:ImageBuild', 'Value': 'rocky9/x86_64'},
            {'Key': 'idea:ModuleId', 'Value': 'vdc'},
        ],
    ],
)
def test_a_failed_bake_leaves_an_instance_it_cannot_prove_is_its_builder(tags):
    svc = service()
    ec2 = builder_ec2(tags)
    svc.context.aws.return_value.ec2.return_value = ec2
    svc.records.put(stuck_row())
    svc._enforce_time_limit(LIMIT_NOW)
    assert svc.records.get('rocky9', 'x86_64').status == 'failed'
    ec2.terminate_instances.assert_not_called()


def test_a_bake_that_fails_on_its_own_disposes_too(monkeypatch):
    svc = service()
    ec2 = builder_ec2(OWN_BUILDER_TAGS)
    svc.context.aws.return_value.ec2.return_value = ec2
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder, 'wait_for_image', lambda *a: None, raising=False
    )
    monkeypatch.setattr(module, 'terminate_builder', lambda *a: None)
    monkeypatch.setattr(
        module.ComputeImageCanary,
        'validate',
        Mock(side_effect=exceptions.general_exception('the canary failed')),
    )
    record = stuck_row(
        checks=[ImageCheck(name='bootstrap', ok=True, detail='fine', seconds=0)],
    )
    svc.records.put(record)
    svc._run_pipeline(record)
    stored = svc.records.get('rocky9', 'x86_64')
    assert stored.status == 'failed' and stored.error == 'the canary failed'
    ec2.terminate_instances.assert_called_once_with(InstanceIds=['i-builder'])
    assert svc._last_sweep == float('-inf')


# a failed bake reaches a person


def alert_lines(svc):
    return [
        call.args[0]
        for call in svc._logger.error.call_args_list
        if call.args[0].startswith('IDEA_IMAGE_BAKE_FAILED compute ')
    ]


def test_a_bake_past_the_limit_logs_the_line_the_monitor_matches():
    svc = service()
    svc.records.put(stuck_row())
    svc._enforce_time_limit(LIMIT_NOW)
    assert len(alert_lines(svc)) == 1
    assert alert_lines(svc)[0].startswith(
        'IDEA_IMAGE_BAKE_FAILED compute rocky9/x86_64 release 26.11.1: '
    )


def test_a_bake_that_fails_on_its_own_logs_the_line_the_monitor_matches(monkeypatch):
    svc = service()
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder, 'wait_for_image', lambda *a: None, raising=False
    )
    monkeypatch.setattr(module, 'terminate_builder', lambda *a: None)
    monkeypatch.setattr(
        module.ComputeImageCanary,
        'validate',
        Mock(side_effect=exceptions.general_exception('the canary failed')),
    )
    record = stuck_row(
        checks=[ImageCheck(name='bootstrap', ok=True, detail='fine', seconds=0)],
    )
    svc.records.put(record)
    svc._run_pipeline(record)
    assert alert_lines(svc) == [
        'IDEA_IMAGE_BAKE_FAILED compute rocky9/x86_64 release 26.11.1: the canary failed'
    ]


def test_a_test_launch_failure_is_counted_as_one_whatever_its_text_says(monkeypatch):
    """
    "The validation job timed out before completion." was counted with reason other: the
    count read the sentence, and the test launch's sentences do not name it.
    """
    svc = service()
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder, 'wait_for_image', lambda *a: None, raising=False
    )
    monkeypatch.setattr(module, 'terminate_builder', lambda *a: None)
    report = Mock()
    monkeypatch.setattr(module, 'report_bake_failure', report)
    monkeypatch.setattr(
        module.ComputeImageCanary,
        'validate',
        Mock(side_effect=exceptions.general_exception('/data is mounted read-only')),
    )
    record = stuck_row(
        checks=[ImageCheck(name='bootstrap', ok=True, detail='fine', seconds=0)],
    )
    svc.records.put(record)
    svc._run_pipeline(record)
    assert report.call_args.kwargs['step'] == 'canary'
    # a failure before the test launch is not one
    report.reset_mock()
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder,
        'wait_for_image',
        Mock(side_effect=exceptions.general_exception('no image')),
        raising=False,
    )
    record = stuck_row(
        checks=[ImageCheck(name='bootstrap', ok=True, detail='fine', seconds=0)],
    )
    svc.records.put(record)
    svc._run_pipeline(record)
    assert report.call_args.kwargs['step'] is None


# a failed bake explains itself

PANIC = 'Likely cause: the operating system crashed while starting (kernel panic).'


def test_a_failing_builder_is_read_before_it_goes_and_the_row_says_why(monkeypatch):
    svc = service()
    terminated = []
    monkeypatch.setattr(
        module, 'dispose_builder', lambda _, instance, __: terminated.append(instance)
    )
    seen = []

    def capture(context, logger, host):
        seen.append((host, list(terminated)))
        return PANIC

    monkeypatch.setattr('ideasdk.aws.image_builds.capture_failure', capture)
    monkeypatch.setattr(svc, 'default_base_ami', lambda *a: 'ami-stock')
    builder = Mock()
    builder.get_ami_full_name.return_value = 'idea-rocky9-1'

    def build(progress):
        progress({'instance_id': 'i-builder'})
        raise exceptions.general_exception('the builder never finished')

    builder.build.side_effect = build
    record = row(status='queued', current_image_id='ami-old')
    svc.records.put(record)
    svc._run_pipeline(record, builder)
    host, terminated_before = seen[0]
    assert terminated_before == [] and terminated == ['i-builder']
    assert host.kind == 'compute_bake' and host.instance_id == 'i-builder'
    assert host.log_group.endswith('/ami-builder')
    stored = svc.records.get('rocky9', 'x86_64')
    assert stored.status == 'failed'
    assert stored.error == f'the builder never finished {PANIC}'


@pytest.mark.parametrize(
    'stored',
    [
        {'status': 'failed', 'started_on': LIMIT_NOW},
        {'status': 'building', 'started_on': LIMIT_NOW + timedelta(hours=3)},
    ],
)
def test_a_worker_left_behind_by_the_limit_does_not_read_the_builder_again(
    monkeypatch, stored
):
    """the time limit already captured this bake; a second read counted it twice"""
    svc = service()
    capture = Mock()
    monkeypatch.setattr('ideasdk.aws.image_builds.capture_failure', capture)
    monkeypatch.setattr(svc, 'default_base_ami', lambda *a: 'ami-stock')
    builder = Mock()
    builder.get_ami_full_name.return_value = 'idea-rocky9-1'
    record = row(status='queued', started_on=LIMIT_NOW)
    svc.records.put(record)

    def build(progress):
        progress({'instance_id': 'i-builder'})
        # the limit fails the row, or a retry takes it, while this worker still waits
        svc.records.put(row(instance_id='i-builder', **stored))
        raise exceptions.general_exception(
            'builder i-builder did not report ready within 60 minutes'
        )

    builder.build.side_effect = build
    svc._run_pipeline(record, builder)
    capture.assert_not_called()
    assert svc.records.get('rocky9', 'x86_64').status == stored['status']


def test_a_canary_failure_does_not_read_the_builder_again(monkeypatch):
    svc = service()
    capture = Mock()
    monkeypatch.setattr('ideasdk.aws.image_builds.capture_failure', capture)
    monkeypatch.setattr(
        module.ComputeNodeAmiBuilder, 'wait_for_image', lambda *a: None, raising=False
    )
    monkeypatch.setattr(module, 'terminate_builder', lambda *a: None)
    monkeypatch.setattr(
        module.ComputeImageCanary,
        'validate',
        Mock(side_effect=exceptions.general_exception('the canary failed')),
    )
    record = stuck_row(
        status='test_launching',
        checks=[ImageCheck(name='bootstrap', ok=True, detail='fine', seconds=0)],
    )
    svc.records.put(record)
    svc._run_pipeline(record)
    capture.assert_not_called()
