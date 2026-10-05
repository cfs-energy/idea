"""
Image row contract: legacy records migrate onto the row model, the table range key
round-trips the variant, filters and the monthly schedule behave as documented.
"""

from datetime import datetime, timezone

import pytest

from ideadatamodel import (
    ImageBuildRecord,
    ImageCheck,
    ImageKind,
    ImagePipelineSettings,
    ImageRefreshSchedule,
    ImageRowFilter,
    ImageRowKey,
    ImageRowStatus,
    ImageVariant,
    RefreshImagesRequest,
    VirtualDesktopSoftwareStack,
    HpcQueueProfile,
)


def test_legacy_complete_row_migrates_to_current_unvalidated():
    legacy = ImageBuildRecord(
        base_os='rocky9',
        architecture='x86_64',
        status='complete',
        base_ami='ami-stock',
        image_id='ami-built',
        update_target=True,
    )
    row = legacy.migrated(ImageKind.DESKTOP)
    assert row.status == ImageRowStatus.CURRENT.value
    assert row.kind == ImageKind.DESKTOP
    assert row.variant == ImageVariant.CPU
    assert row.source_ami == 'ami-stock'
    assert row.current_image_id == 'ami-built'
    # never validated by the pipeline: the promote gate must refuse it
    assert row.validated_on is None
    # the original is untouched
    assert legacy.status == 'complete'


def test_legacy_complete_without_update_target_is_not_current_image():
    row = ImageBuildRecord(status='complete', image_id='ami-built').migrated(
        ImageKind.COMPUTE
    )
    assert row.status == 'current'
    assert row.current_image_id is None


@pytest.mark.parametrize(
    'legacy,expected', [('building', 'building'), ('failed', 'failed')]
)
def test_legacy_status_mapping(legacy, expected):
    assert (
        ImageBuildRecord(status=legacy).migrated(ImageKind.DESKTOP).status == expected
    )


def test_new_rows_migrate_unchanged():
    row = ImageBuildRecord(
        kind=ImageKind.COMPUTE,
        variant=ImageVariant.NVIDIA,
        status=ImageRowStatus.TEST_LAUNCHING.value,
        source_ami='ami-a',
        base_ami='ami-old',
    )
    assert row.migrated(ImageKind.DESKTOP) == row


def test_record_round_trips_through_json_dict():
    row = ImageBuildRecord(
        kind=ImageKind.DESKTOP,
        base_os='windows2022',
        architecture='x86_64',
        variant=ImageVariant.CPU,
        status=ImageRowStatus.FAILED.value,
        validated_on=datetime(2026, 10, 2, tzinfo=timezone.utc),
        checks=[
            ImageCheck(
                name='ready_gate', ok=False, detail='not READY in 480 s', seconds=480
            )
        ],
        trigger='button',
        pinned=False,
        rollback_hold=True,
    )
    data = row.model_dump(mode='json')
    assert data['kind'] == 'desktop'
    assert data['trigger'] == 'button'
    assert data['checks'][0] == {
        'name': 'ready_gate',
        'ok': False,
        'detail': 'not READY in 480 s',
        'seconds': 480,
    }
    assert ImageBuildRecord(**data) == row


def test_range_key_round_trip():
    cpu = ImageRowKey(base_os='ubuntu2404', architecture='arm64')
    gpu = ImageRowKey(base_os='ubuntu2404', architecture='x86_64', variant='nvidia')
    assert cpu.range_key() == 'arm64'
    assert gpu.range_key() == 'x86_64#nvidia'
    assert ImageRowKey.split_range_key('arm64') == ('arm64', 'cpu')
    assert ImageRowKey.split_range_key('x86_64#amd') == ('x86_64', 'amd')


def test_filter():
    rows = [
        ImageBuildRecord(
            kind='desktop',
            base_os='ubuntu2204',
            architecture='x86_64',
            status='current',
        ),
        ImageBuildRecord(
            kind='desktop', base_os='ubuntu2404', architecture='arm64', status='failed'
        ),
        ImageBuildRecord(
            kind='desktop',
            base_os='rocky9',
            architecture='x86_64',
            variant='nvidia',
            status='current',
        ),
    ]

    def pick(**kwargs):
        f = ImageRowFilter(**kwargs)
        return [r.base_os for r in rows if f.matches(r)]

    assert pick() == ['ubuntu2204', 'ubuntu2404', 'rocky9']
    assert pick(base_os_family='ubuntu') == ['ubuntu2204', 'ubuntu2404']
    assert pick(variant='cpu') == ['ubuntu2204', 'ubuntu2404']
    assert pick(variant='nvidia') == ['rocky9']
    assert pick(statuses=['failed']) == ['ubuntu2404']
    assert pick(kind='compute') == []


def test_in_flight():
    assert ImageBuildRecord(status='queued').is_in_flight()
    assert ImageBuildRecord(status='waiting_capacity').is_in_flight()
    assert not ImageBuildRecord(status='current').is_in_flight()
    assert not ImageBuildRecord(status='complete').is_in_flight()


@pytest.mark.parametrize(
    'after,expected',
    [
        # october 2026: the 1st is a thursday, first sunday the 4th
        (datetime(2026, 10, 2, 12), datetime(2026, 10, 4, 2)),
        (datetime(2026, 10, 4, 2), datetime(2026, 11, 1, 2)),
        (datetime(2026, 12, 31, 23), datetime(2027, 1, 3, 2)),
    ],
)
def test_schedule_first_sunday(after, expected):
    assert ImageRefreshSchedule().next_run_after(after) == expected


def test_schedule_last_friday_and_disabled():
    s = ImageRefreshSchedule(day='last friday', hour=23)
    assert s.next_run_after(datetime(2026, 10, 1)) == datetime(2026, 10, 30, 23)
    assert (
        ImageRefreshSchedule(enabled=False).next_run_after(datetime(2026, 10, 1))
        is None
    )


def test_schedule_rejects_bad_rule():
    with pytest.raises(ValueError):
        ImageRefreshSchedule(day='every sunday').validate_rule()
    with pytest.raises(ValueError):
        ImageRefreshSchedule(hour=24).validate_rule()


def test_defaults_and_pins():
    s = ImagePipelineSettings()
    assert (s.max_concurrent_bakes, s.keep_generations) == (4, 2)
    assert (s.ready_gate_seconds_linux, s.ready_gate_seconds_windows) == (600, 900)
    assert s.validation_user == 'idea-validate'
    assert VirtualDesktopSoftwareStack().image_pinned is None
    assert HpcQueueProfile(image_pinned=True).image_pinned is True
    assert (
        RefreshImagesRequest(rows=[ImageRowKey(base_os='rocky9')]).rows[0].base_os
        == 'rocky9'
    )


def test_settings_template_carries_the_model_gates():
    import re
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[2]
        / 'ideactl/resources/config/templates/virtual-desktop-controller/settings.yml'
    ).read_text()
    s = ImagePipelineSettings()
    for key in ('ready_gate_seconds_linux', 'ready_gate_seconds_windows'):
        assert re.search(rf'^\s+{key}: (\d+)$', template, re.M).group(1) == str(
            getattr(s, key)
        )


def test_baked_today_counts_the_cluster_day_of_the_last_start():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo('America/New_York')
    # 23:30 local on Oct 3 is 03:30Z on Oct 4
    row = ImageBuildRecord(started_on=datetime(2026, 10, 4, 3, 30, tzinfo=timezone.utc))
    assert row.baked_today(datetime(2026, 10, 4, 3, 59, tzinfo=timezone.utc), tz)
    # next local day (00:10 Oct 4 local)
    assert not row.baked_today(datetime(2026, 10, 4, 4, 10, tzinfo=timezone.utc), tz)
    # a naive stored time is UTC; a row never started was never baked
    naive = ImageBuildRecord(started_on=datetime(2026, 10, 4, 3, 30))
    assert naive.baked_today(datetime(2026, 10, 4, 3, 59, tzinfo=timezone.utc), tz)
    assert not ImageBuildRecord().baked_today(datetime.now(timezone.utc), tz)
    assert RefreshImagesRequest(all=True, force=True).force is True
