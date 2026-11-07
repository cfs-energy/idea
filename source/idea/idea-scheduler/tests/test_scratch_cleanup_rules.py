"""Every reason a folder stays, at the rule level."""

from datetime import datetime, timedelta, timezone
import pytest

from ideascheduler.app.scratch_cleanup.rules import (
    ActiveJobs,
    CLOCK_SLACK,
    FolderContents,
    JobRecord,
    Limits,
    REASON_TEXT,
    Reason,
    base_job_id,
    decide_contents,
    decide_job,
    is_owner_folder_name,
    limits_problem,
    parse_folder_name,
    plan_problem,
)

UID = 'abcdefghijkl'
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
START = NOW - timedelta(days=31)
END = NOW - timedelta(days=30)


def record(**overrides):
    values = dict(
        job_id='123',
        job_uid=UID,
        owner='alice',
        finished=True,
        start_time=START,
        end_time=END,
    )
    values.update(overrides)
    return JobRecord(**values)


def decide(name='123', owner_folder='alice', records=None, **overrides):
    values = dict(
        owner_folder=owner_folder,
        folder_name=name,
        shape=parse_folder_name(name),
        folder_uid=1000,
        owner_folder_uid=1000,
        owner_uid=1000,
        active=ActiveJobs(),
        records=[record()] if records is None else records,
        now=NOW,
        min_age=timedelta(days=14),
        failed_min_age=timedelta(days=45),
    )
    values.update(overrides)
    return decide_job(**values).reason


@pytest.mark.parametrize(
    'name',
    [
        '',
        '.',
        '..',
        'apptainer-tmp',
        'job-123',
        'job-123-short',
        'job-123-has/slash1',
        '123\n',
        ' 123',
        '123 ',
        '-123',
        '12a',
        '123[]',
        'job-123-abcdefghijkl',
        '123_4',
        '123.4',
        '123[4].server',
        '[4]',
        '123[4][5]',
        '123[',
        '123[4',
        '123[4]x',
        '١٢٣',
        '1' * 19,
        'solution_123',
        '../123',
        'job-123-abcdefgh/..',
    ],
)
def test_any_other_name_is_not_a_job_folder(name):
    assert parse_folder_name(name) is None


def test_an_array_subjob_is_a_job_folder_shape():
    shape = parse_folder_name('123[4]')
    assert shape.base_id == '123'
    assert set(shape.job_id_forms) == {'123[4]', '123[]'}


def test_a_number_is_a_job_folder_shape():
    shape = parse_folder_name('123')
    assert (shape.base_id, set(shape.job_id_forms)) == ('123', {'123'})


@pytest.mark.parametrize('name', ['.', '..', '', '.hidden', 'a/b', 'a b', 'a\n'])
def test_owner_folder_names_refused(name):
    assert not is_owner_folder_name(name)


@pytest.mark.parametrize(
    'value, expected',
    [('123', '123'), ('123[]', '123'), ('123[4]', '123'), ('x', None), ('12.s', None)],
)
def test_scheduler_ids_reduce_to_their_base(value, expected):
    assert base_job_id(value) == expected


def test_a_finished_job_old_enough_is_eligible():
    assert decide() == Reason.ELIGIBLE


@pytest.mark.parametrize('state', ['running', 'queued', 'held', 'suspended', 'waiting'])
def test_an_active_job_in_any_state_stays(state):
    # the scheduler's list holds every job not over, whatever its state
    active = ActiveJobs(base_ids=frozenset({'123'}))
    assert decide(active=active) == Reason.JOB_ACTIVE


def test_active_wins_even_with_a_finished_record_for_a_reused_number():
    active = ActiveJobs(base_ids=frozenset({'123'}))
    assert decide(active=active, records=[record()]) == Reason.JOB_ACTIVE


def test_a_job_another_active_job_depends_on_stays():
    active = ActiveJobs(dependency_ids=frozenset({'123'}))
    assert decide(active=active) == Reason.REFERENCED_BY_ACTIVE_JOB


def test_a_job_number_an_active_job_mentions_stays():
    active = ActiveJobs(mentioned_ids=frozenset({'123'}))
    assert decide(active=active) == Reason.REFERENCED_BY_ACTIVE_JOB


def test_any_active_job_of_the_owner_keeps_all_their_folders():
    active = ActiveJobs(owners=frozenset({'alice'}))
    assert decide(active=active) == Reason.OWNER_HAS_ACTIVE_JOB


def test_someone_elses_active_job_does_not_hold_this_owners_folders():
    assert decide(active=ActiveJobs(owners=frozenset({'bob'}))) == Reason.ELIGIBLE


def test_owner_names_compare_without_case():
    active = ActiveJobs(owners=frozenset({'alice'}))
    assert decide(
        name='123', owner_folder='Alice', active=active, records=[record(owner='Alice')]
    ) == (Reason.OWNER_HAS_ACTIVE_JOB)


def test_a_folder_an_active_job_names_stays():
    active = ActiveJobs(referenced_folders=frozenset({('alice', '123')}))
    assert decide(active=active) == Reason.REFERENCED_BY_ACTIVE_JOB


def test_no_record_stays():
    assert decide(records=[]) == Reason.NO_FINISHED_RECORD


def test_a_record_for_someone_else_says_nothing_about_this_folder():
    assert decide(records=[record(owner='bob')]) == Reason.NO_FINISHED_RECORD


def test_a_record_with_no_owner_stays():
    assert decide(records=[record(owner=None)]) == Reason.NO_FINISHED_RECORD


def test_unknown_owner_uid_stays():
    assert decide(owner_uid=None) == Reason.OWNER_UNKNOWN


def test_folder_owned_by_someone_else_stays():
    assert decide(folder_uid=2000) == Reason.OWNER_MISMATCH


def test_a_record_not_marked_finished_stays():
    assert decide(records=[record(finished=False)]) == Reason.RECORD_NOT_FINISHED


def test_any_unfinished_record_under_a_reused_number_stays():
    records = [record(), record(job_uid='another-uid-1', finished=False)]
    assert decide(records=records) == Reason.RECORD_NOT_FINISHED


@pytest.mark.parametrize('missing', ['start_time', 'end_time'])
def test_a_record_missing_a_time_stays(missing):
    assert decide(records=[record(**{missing: None})]) == Reason.RECORD_INCOMPLETE


def test_a_job_that_never_started_stays():
    assert decide(records=[record(start_time=None, end_time=None)]) == (
        Reason.RECORD_INCOMPLETE
    )


def test_an_owner_folder_owned_by_someone_else_stays():
    assert decide(owner_folder_uid=2000) == Reason.OWNER_MISMATCH


def test_a_failed_job_waits_its_longer_time():
    records = [record(succeeded=False)]
    assert decide(records=records, min_age=timedelta(days=14)) == Reason.TOO_RECENT
    assert decide(records=records, failed_min_age=timedelta(days=29)) == Reason.ELIGIBLE


def test_any_failed_job_under_a_reused_number_takes_the_longer_time():
    records = [record(), record(job_uid='another-uid-1', succeeded=False)]
    assert decide(records=records) == Reason.TOO_RECENT


def subjob_decide(records, **overrides):
    return decide(name='123[4]', records=records, **overrides)


def test_a_subjob_with_its_own_record_goes_by_that_record():
    own = record(job_id='123[4]', job_uid='own-uid-0001')
    parent = record(job_id='123[]', job_uid='parent-uid-1', succeeded=False)
    assert subjob_decide([own, parent]) == Reason.ELIGIBLE


def test_a_subjob_that_failed_itself_waits_though_its_array_succeeded():
    own = record(job_id='123[4]', job_uid='own-uid-0001', succeeded=False)
    parent = record(job_id='123[]', job_uid='parent-uid-1')
    assert subjob_decide([own, parent]) == Reason.TOO_RECENT


def test_a_subjob_whose_own_record_is_not_finished_stays():
    own = record(job_id='123[4]', job_uid='own-uid-0001', finished=False)
    parent = record(job_id='123[]', job_uid='parent-uid-1')
    assert subjob_decide([own, parent]) == Reason.RECORD_NOT_FINISHED


def test_a_subjob_with_no_own_record_takes_a_finished_parents_status():
    assert subjob_decide([record(job_id='123[]')]) == Reason.ELIGIBLE
    failed = record(job_id='123[]', succeeded=False)
    assert subjob_decide([failed]) == Reason.TOO_RECENT


def test_a_subjob_whose_parent_is_not_finished_stays():
    parent = record(job_id='123[]', finished=False)
    assert subjob_decide([parent]) == Reason.RECORD_NOT_FINISHED


def test_a_subjob_with_no_record_at_all_stays():
    assert subjob_decide([]) == Reason.NO_FINISHED_RECORD


def test_a_subjob_stays_while_any_of_its_array_is_active():
    active = ActiveJobs(base_ids=frozenset({'123'}))
    own = record(job_id='123[4]', job_uid='own-uid-0001')
    assert subjob_decide([own], active=active) == Reason.JOB_ACTIVE


def test_a_job_that_ended_recently_stays():
    records = [record(end_time=NOW - timedelta(days=13))]
    assert decide(records=records) == Reason.TOO_RECENT


def test_age_counts_from_the_latest_job_under_the_number():
    records = [
        record(),
        record(job_uid='later-job-uid', end_time=NOW - timedelta(days=1)),
    ]
    assert decide(records=records) == Reason.TOO_RECENT


def contents(newest, oldest=None, newest_file=None, **overrides):
    return FolderContents(
        newest_change=newest,
        oldest_change=newest if oldest is None else oldest,
        newest_file_change=newest_file,
        **overrides,
    )


def test_a_folder_last_changed_during_the_job_is_eligible():
    newest = START + timedelta(hours=1)
    assert decide_contents(contents(newest), [record()]) == Reason.ELIGIBLE


def test_a_change_after_the_job_keeps_the_folder():
    newest = END + CLOCK_SLACK + timedelta(seconds=1)
    assert decide_contents(contents(newest), [record()]) == Reason.CHANGED_AFTER_JOB


def test_a_change_at_the_edge_of_the_slack_is_still_the_job():
    assert decide_contents(contents(END + CLOCK_SLACK), [record()]) == Reason.ELIGIBLE


def test_older_than_the_job_means_the_number_may_be_reused():
    newest = START - CLOCK_SLACK - timedelta(seconds=1)
    assert decide_contents(contents(newest), [record()]) == Reason.OLDER_THAN_JOB


def test_either_job_under_a_reused_number_can_prove_the_folder():
    old = record(
        job_uid='old-job-uid-1',
        start_time=START - timedelta(days=200),
        end_time=END - timedelta(days=200),
    )
    newest = END - timedelta(days=200)
    assert decide_contents(contents(newest), [old, record()]) == Reason.ELIGIBLE
    between = END - timedelta(days=100)
    assert decide_contents(contents(between), [old, record()]) == Reason.OLDER_THAN_JOB


@pytest.mark.parametrize(
    'flag, reason',
    [
        ('unreadable', Reason.UNREADABLE),
        ('over_budget', Reason.TOO_LARGE_TO_CHECK),
        ('other_mount', Reason.OTHER_MOUNT_INSIDE),
        ('other_owner', Reason.OTHER_OWNERS_FILES),
    ],
)
def test_contents_that_disqualify_a_folder(flag, reason):
    found = contents(START + timedelta(hours=1), **{flag: True})
    assert decide_contents(found, [record()]) == reason
    # resuming a removal skips only the time check
    assert decide_contents(found, [record()], resume_until=NOW) == reason


def test_a_file_read_after_the_job_stays_where_atime_works():
    during = START + timedelta(hours=1)
    found = FolderContents(
        newest_change=during,
        oldest_change=during,
        newest_access=END + CLOCK_SLACK + timedelta(seconds=1),
    )
    assert decide_contents(found, [record()]) == Reason.READ_AFTER_JOB
    assert decide_contents(found, [record()], resume_until=NOW) == Reason.READ_AFTER_JOB


def test_a_file_read_during_the_job_is_fine():
    during = START + timedelta(hours=1)
    found = FolderContents(
        newest_change=during, oldest_change=during, newest_access=END
    )
    assert decide_contents(found, [record()]) == Reason.ELIGIBLE


def test_no_timestamp_is_not_a_proof():
    assert decide_contents(contents(None), [record()]) == Reason.UNREADABLE
    assert decide_contents(contents(START), []) == Reason.UNREADABLE


def test_old_data_renamed_into_a_job_folder_stays():
    # a rename can bump the top folder into the window; nothing moves old ctimes forward
    during = START + timedelta(hours=1)
    old = START - timedelta(days=90)
    assert decide_contents(contents(during, oldest=old), [record()]) == (
        Reason.OLDER_THAN_JOB
    )


def test_data_from_two_jobs_under_a_reused_number_stays():
    earlier = record(
        job_uid='old-job-uid-1',
        start_time=START - timedelta(days=200),
        end_time=END - timedelta(days=200),
    )
    newest = START + timedelta(hours=1)
    oldest = START - timedelta(days=200)
    assert decide_contents(contents(newest, oldest=oldest), [earlier, record()]) == (
        Reason.OLDER_THAN_JOB
    )


def test_files_from_before_a_requeue_still_count_as_the_job():
    queued = START - timedelta(days=3)
    newest = START + timedelta(hours=1)
    oldest = queued + timedelta(hours=1)
    assert decide_contents(
        contents(newest, oldest=oldest), [record(first_run_start=queued)]
    ) == (Reason.ELIGIBLE)
    assert decide_contents(contents(newest, oldest=oldest), [record()]) == (
        Reason.OLDER_THAN_JOB
    )


def test_resuming_excuses_only_directories_the_removal_emptied():
    emptied = END + timedelta(days=5)
    during = START + timedelta(hours=1)
    found = contents(emptied, oldest=during, newest_file=during)
    assert decide_contents(found, [record()], resume_until=NOW) == Reason.ELIGIBLE
    assert decide_contents(found, [record()]) == Reason.CHANGED_AFTER_JOB


def test_new_files_are_not_excused_by_a_leftover_removal():
    new_data = END + timedelta(days=5)
    found = contents(new_data, oldest=START, newest_file=new_data)
    assert decide_contents(found, [record()], resume_until=NOW) == (
        Reason.CHANGED_AFTER_JOB
    )


def test_old_files_are_not_excused_by_a_leftover_removal():
    old = START - timedelta(days=90)
    found = contents(START, oldest=old, newest_file=START)
    assert decide_contents(found, [record()], resume_until=NOW) == Reason.OLDER_THAN_JOB


def test_a_directory_changed_after_the_removal_stopped_is_not_excused():
    stopped = END + timedelta(days=5)
    during = START + timedelta(hours=1)
    found = FolderContents(
        newest_change=stopped,
        oldest_change=during,
        newest_file_change=during,
        newest_dir_change=stopped + timedelta(days=1),
    )
    assert decide_contents(found, [record()], resume_until=stopped) == (
        Reason.CHANGED_AFTER_JOB
    )
    emptied = FolderContents(
        newest_change=stopped,
        oldest_change=during,
        newest_file_change=during,
        newest_dir_change=stopped,
    )
    assert decide_contents(emptied, [record()], resume_until=stopped) == Reason.ELIGIBLE


def test_resuming_with_only_empty_directories_left_can_finish():
    found = contents(END + timedelta(days=5), oldest=START)
    assert decide_contents(found, [record()], resume_until=NOW) == Reason.ELIGIBLE


def test_every_reason_has_plain_text():
    assert set(REASON_TEXT) == set(Reason)


def limits(**overrides):
    values = dict(
        enabled=True,
        dry_run=False,
        kill_switch=False,
        file_systems=('scratch',),
        success_retention_days=30,
        failed_retention_days=60,
        max_folders_per_run=25,
        max_run_bytes_percent=25.0,
        max_run_share_percent=50.0,
    )
    values.update(overrides)
    return Limits(**values)


@pytest.mark.parametrize(
    'overrides',
    [
        {'success_retention_days': 13},
        {'failed_retention_days': 13},
        {'digest_weekday': 7},
        {'digest_weekday': -1},
        {'max_folders_per_run': 0},
        {'max_run_bytes_percent': 0.0},
        {'max_run_share_percent': 101.0},
    ],
)
def test_settings_that_make_no_sense_stop_cleanup(overrides):
    assert limits_problem(limits(**overrides))


def test_the_default_settings_are_acceptable():
    assert limits_problem(limits()) is None


def plan(**overrides):
    values = dict(
        planned_folders=5,
        planned_bytes=100,
        job_folders=100,
        capacity_bytes=1000,
        limits=limits(),
    )
    values.update(overrides)
    return plan_problem(**values)


def test_an_ordinary_plan_passes():
    assert plan() is None


def test_a_plan_removing_too_much_capacity_stops():
    assert '25.1%' in plan(planned_bytes=251)


def test_a_plan_removing_too_many_job_folders_stops():
    assert '51%' in plan(planned_folders=51)


def test_share_check_needs_enough_folders_to_mean_anything():
    assert plan(planned_folders=9, job_folders=9) is None
    assert plan(planned_folders=10, job_folders=10) is not None


def test_no_capacity_stops():
    assert plan(capacity_bytes=0)
