"""Compute row queuing, validation gates, target guards and leader scheduling."""

from datetime import datetime, timezone
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


def test_monthly_checks_only_changed_inputs_and_release_works_when_disabled(
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
    svc._enqueue.assert_called_once_with(changed, None, 'monthly')
    svc._enqueue.reset_mock()
    svc.context.config().values[
        'virtual-desktop-controller.software_stacks.image_refresh_schedule'
    ] = {'enabled': False}
    svc.context.config().values['scheduler.images.refreshed_release'] = 'old'
    unchanged.release = 'old'
    svc.tick(datetime(2026, 10, 4, 2, tzinfo=timezone.utc))
    assert svc._enqueue.call_count == 2
    svc._enqueue.assert_any_call(unchanged, None, 'release')


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
    assert update['ConditionExpression'] == 'started_on = :started'


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


def test_builder_sweep_stops_only_orphaned_builders_past_any_real_bake(monkeypatch):
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
    stopped = []
    monkeypatch.setattr(
        module,
        'stop_builder',
        lambda context, instance_id, logger: stopped.append(instance_id),
    )
    monkeypatch.setattr(module, 'terminate_old_stopped_builders', lambda *a: [])
    svc.sweep_builders(now)
    assert stopped == ['i-orphan']


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
        module.ComputeImageCanary, 'reap', lambda self, p: reaped.append(p.name) or []
    )
    svc.sweep_builders(datetime.now(timezone.utc))
    assert reaped == ['iv-left']
