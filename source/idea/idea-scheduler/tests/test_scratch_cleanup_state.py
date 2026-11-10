"""Job state: a failed source raises, and zero active jobs is only believed with no other sign."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideadatamodel import SocaJob, SocaJobState
from ideascheduler.app.scratch_cleanup.job_state import (
    ScratchJobState,
    StateUnavailable,
)

ALIASES = ['/lustre', '/fsx']


def shell(
    qselect='', qstat='{"Jobs":{}}', qselect_rc=0, qstat_rc=0, scripts=None, seen=None
):
    if 'Job_Owner' not in qstat:
        # every job PBS lists has an owner; a test about one says so itself
        qstat = qstat + ' "Job_Owner":"t@h"' * len(qselect.splitlines())

    def invoke(cmd, **_):
        if seen is not None:
            seen.append(cmd)
        if cmd[0].endswith('qselect'):
            return SimpleNamespace(returncode=qselect_rc, stdout=qselect)
        if cmd[0].endswith('printjob'):
            job = cmd[-1]
            if scripts is None:
                return SimpleNamespace(returncode=0, stdout='')
            if job not in scripts:
                return SimpleNamespace(returncode=1, stdout='', stderr='no such job')
            return SimpleNamespace(returncode=0, stdout=scripts[job])
        return SimpleNamespace(returncode=qstat_rc, stdout=qstat)

    return SimpleNamespace(invoke=invoke)


def state(tracked=(), nodes=(), ready=True, **shell_args):
    context = Mock()
    context.shell = shell(**shell_args)
    context.scheduler.is_ready.return_value = ready
    context.job_cache.is_ready.return_value = ready
    context.job_cache.list_jobs.return_value = [
        SimpleNamespace(job_id=job_id, owner='t') for job_id in tracked
    ]
    context.scheduler.list_nodes.return_value = list(nodes)
    context.document_store.is_enabled.return_value = False
    return ScratchJobState(context, Mock()), context


def test_active_ids_come_from_pbs_and_the_cache_alike():
    source, _ = state(
        qselect='10.server\n11[].server\n12[3].server\n',
        tracked=['20'],
        qstat='{"Jobs":{}}',
    )
    active = source.active_jobs(ALIASES)
    assert active.base_ids == {'10', '11', '12', '20'}


def test_failed_qselect_is_not_an_empty_cluster():
    source, _ = state(qselect_rc=1)
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_failed_qstat_is_not_no_references():
    source, _ = state(qselect='10.server\n', qstat_rc=1)
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_pbs_not_ready_stops():
    source, _ = state(ready=False)
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_an_unparsable_job_id_stops():
    source, _ = state(qselect='what-is-this.server\n')
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_zero_from_pbs_with_the_cache_tracking_jobs_stops():
    source, _ = state(qselect='', tracked=['5'])
    with pytest.raises(StateUnavailable) as raised:
        source.active_jobs(ALIASES)
    assert 'tracks 1' in str(raised.value)


def test_zero_from_pbs_with_busy_nodes_stops():
    source, _ = state(qselect='', nodes=[SimpleNamespace(jobs=['10.server'])])
    with pytest.raises(StateUnavailable) as raised:
        source.active_jobs(ALIASES)
    assert 'nodes' in str(raised.value)


def test_zero_from_pbs_with_unreadable_nodes_stops():
    source, context = state(qselect='')
    context.scheduler.list_nodes.side_effect = RuntimeError('pbsnodes failed')
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_a_truly_idle_cluster_is_believed():
    source, _ = state(qselect='', nodes=[SimpleNamespace(jobs=None)])
    assert source.active_jobs(ALIASES).base_ids == frozenset()


def test_dependencies_of_active_jobs_are_found():
    qstat = (
        '{"Jobs":{"9.s":{"depend":"afterok:123.server:124[].server,afterany:125.s"}}}'
    )
    source, _ = state(qselect='9.s\n', qstat=qstat)
    assert source.active_jobs(ALIASES).dependency_ids == {'123', '124', '125'}


def test_dependencies_without_a_server_suffix_are_all_found():
    qstat = '{"Jobs":{"9.s":{"depend":"afterok:123:456,afterany:1:2:3"}}}'
    source, _ = state(qselect='9.s\n', qstat=qstat)
    assert source.active_jobs(ALIASES).dependency_ids == {'123', '456', '1', '2', '3'}


def test_qstat_saying_nothing_while_jobs_are_active_stops():
    source, _ = state(qselect='9.s\n', qstat='')
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_folders_named_by_active_jobs_are_found_even_in_invalid_json():
    qstat = (
        '{"Jobs":{"9.s":{"Output_Path":"host:/lustre/alice/123/out.log",'
        '"Variable_List":{"PBS_O_WORKDIR":"/fsx/bob/124","BAD":"un"escaped"},'
        '"Error_Path":"host:\\/lustre\\/carol\\/125\\/err"}}}'
    )
    source, _ = state(qselect='9.s\n', qstat=qstat)
    found = source.active_jobs(ALIASES).referenced_folders
    assert ('alice', '123') in found
    assert ('bob', '124') in found
    assert ('carol', '125') in found, 'escaped slashes are still found'


def job(job_id='123', owner='alice', uid='u1', job_state=SocaJobState.FINISHED):
    return SocaJob(
        job_id=job_id,
        owner=owner,
        job_uid=uid,
        state=job_state,
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def test_records_come_from_the_cache_and_the_index():
    source, context = state()
    context.job_cache.list_completed_jobs.return_value = [job(uid='u1')]
    context.document_store.is_enabled.return_value = True
    context.document_store.is_initialized.return_value = True
    context.document_store.find_jobs_by_id.return_value = [job(uid='u1'), job(uid='u2')]
    records = source.finished_records({'123'})
    assert {record.job_uid for record in records} == {'u1', 'u2'}
    assert all(record.finished for record in records)


def test_a_record_keeps_the_jobs_that_waited_on_it_by_number():
    source, context = state()
    finished = job()
    finished.dependent_job_ids = ['348', '350[]']
    context.job_cache.list_completed_jobs.return_value = [finished]
    assert source.finished_records({'123'})[0].dependents == {'348', '350'}


def test_a_record_in_any_other_state_is_not_finished():
    source, context = state()
    context.job_cache.list_completed_jobs.return_value = [
        job(job_state=SocaJobState.EXIT)
    ]
    assert not source.finished_records({'123'})[0].finished


def test_the_cache_failing_is_not_no_records():
    source, context = state()
    context.job_cache.list_completed_jobs.side_effect = RuntimeError('locked')
    with pytest.raises(StateUnavailable):
        source.finished_records({'123'})


def test_the_index_failing_is_not_no_records():
    source, context = state()
    context.job_cache.list_completed_jobs.return_value = [job()]
    context.document_store.is_enabled.return_value = True
    context.document_store.is_initialized.return_value = True
    context.document_store.find_jobs_by_id.side_effect = RuntimeError('503')
    with pytest.raises(StateUnavailable):
        source.finished_records({'123'})


def test_an_index_not_ready_is_not_no_records():
    source, context = state()
    context.job_cache.list_completed_jobs.return_value = []
    context.document_store.is_enabled.return_value = True
    context.document_store.is_initialized.return_value = False
    with pytest.raises(StateUnavailable):
        source.finished_records({'123'})


def test_no_index_on_the_cluster_means_the_cache_decides():
    source, context = state()
    context.job_cache.list_completed_jobs.return_value = [job()]
    assert len(source.finished_records({'123'})) == 1


def _job_with_runs(queue, start, runs):
    from ideadatamodel import (
        SocaJobExecution,
        SocaJobExecutionHost,
        SocaJobExecutionRun,
    )

    found = job()
    found.queue_time, found.start_time = queue, start
    found.execution_hosts = [
        SocaJobExecutionHost(
            host='h',
            execution=SocaJobExecution(
                runs=[
                    SocaJobExecutionRun(run_id=str(i), start=r)
                    for i, r in enumerate(runs)
                ]
            ),
        )
    ]
    return found


def _when(day):
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def _window_decision(found, oldest):
    from ideascheduler.app.scratch_cleanup.rules import (
        FolderContents,
        decide_contents,
    )

    source, context = state()
    context.job_cache.list_completed_jobs.return_value = [found]
    records = source.finished_records({'123'})
    contents = FolderContents(newest_change=found.end_time, oldest_change=oldest)
    return decide_contents(contents, records).value


def test_a_folder_made_after_queueing_but_before_the_job_ran_stays():
    # end time is 2026-01-02 from job(); queued long before it started
    found = _job_with_runs(_when(1), datetime(2026, 1, 1, 20, tzinfo=timezone.utc), [])
    made = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    assert _window_decision(found, made) == 'older_than_job'


def test_a_requeued_jobs_first_attempt_counts_as_the_job():
    first = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
    second = datetime(2026, 1, 1, 20, tzinfo=timezone.utc)
    found = _job_with_runs(_when(1), second, [second, first])
    made = datetime(2026, 1, 1, 3, tzinfo=timezone.utc)
    assert _window_decision(found, made) == 'eligible'
    earlier = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert _window_decision(found, earlier) == 'older_than_job'


@pytest.mark.parametrize(
    'label, listed',
    [
        ('running R', '123.server'),
        ('queued Q', '123.server'),
        ('held H', '123.server'),
        ('suspended S', '123.server'),
        ('exiting E', '123.server'),
        ('waiting W', '123.server'),
        ('moving T', '123.server'),
        ('array parent B', '123[].server'),
        ('array subjob', '123[7].server'),
        ('in a reservation queue', '123.server'),
    ],
)
def test_every_state_pbs_lists_is_active(label, listed):
    source, _ = state(qselect=f'{listed}\n', qstat='{"Jobs":{}}')
    assert '123' in source.active_jobs(ALIASES).base_ids, label


def test_the_listing_asks_pbs_for_every_state():
    seen = []
    source, context = state(qselect='1.s\n')
    context.shell = shell(qselect='1.s\n', seen=seen)
    source.active_jobs(ALIASES)
    listing = next(cmd for cmd in seen if cmd[0].endswith('qselect'))
    assert listing == ['/opt/pbs/bin/qselect'], 'no state filter, nothing left out'


def test_a_job_on_a_node_counts_even_if_pbs_lists_others():
    nodes = [SimpleNamespace(jobs=['777', '778[3]'])]
    source, _ = state(qselect='1.s\n', nodes=nodes)
    assert {'777', '778'} <= source.active_jobs(ALIASES).base_ids


def test_unreadable_nodes_stop_even_with_jobs_listed():
    source, context = state(qselect='1.s\n')
    context.scheduler.list_nodes.side_effect = RuntimeError('pbsnodes failed')
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_a_script_naming_a_folder_counts():
    scripts = {'9.s': 'cp -r /lustre/alice/123/restart .\n'}
    source, context = state()
    context.shell = shell(qselect='9.s\n', scripts=scripts)
    active = source.active_jobs(ALIASES)
    assert ('alice', '123') in active.referenced_folders


def test_a_script_using_user_and_a_job_number_counts():
    scripts = {'9.s': 'RESTART=/fsx/${USER}/4567/restart.dat\nrun_old=job_890\n'}
    source, context = state()
    context.shell = shell(qselect='9.s\n', scripts=scripts)
    assert {'4567', '890'} <= source.active_jobs(ALIASES).mentioned_ids


def test_numbers_in_the_environment_and_arguments_count():
    qstat = (
        '{"Jobs":{"9.s":{\n"Submit_arguments":"-v RESTART=321 job.sh",\n'
        '"Variable_List":{\n"FROM_JOB":"654",\n"BAD":"un"escaped"\n},\n'
        '"ctime":"Thu Jan 22 11:12:13 2026"\n}}}'
    )
    source, context = state()
    context.shell = shell(qselect='9.s\n', qstat=qstat, scripts={'9.s': ''})
    mentioned = source.active_jobs(ALIASES).mentioned_ids
    assert {'321', '654'} <= mentioned
    assert '2026' not in mentioned, 'only the fields a person sets'


def test_only_a_whole_number_counts_as_a_job_number():
    """
    seen on a cluster: one job's instance type and AMI id kept the folders of jobs 3, 17, 525
    and 645 as referenced. A job id stands on its own; digits inside another word do not.
    """
    qstat = (
        '{"Jobs":{"9.s":{\n"Submit_arguments":"-N s4jobs -l instance_type=t3.medium '
        '-l instance_ami=ami-01218e645c17e525d -l mem=8gb -v PREV=363,OLD=364[2] job.sh",\n'
        '"Variable_List":{\n"FROM":"365.scheduler"\n},\n'
        '"ctime":"Thu Jan 22 11:12:13 2026"\n}}}'
    )
    script = 'cp out_0366.log /tmp; x=job_367; y=c5n.2xlarge; z=i-0cfe47831e2715e4e\n'
    source, context = state()
    context.shell = shell(qselect='9.s\n', qstat=qstat, scripts={'9.s': script})
    mentioned = source.active_jobs(ALIASES).mentioned_ids
    assert {'363', '364', '2', '365', '366', '367'} <= mentioned
    assert not mentioned & {
        '3',
        '4',
        '5',
        '8',
        '17',
        '525',
        '645',
        '01218',
        '1218',
        '0',
    }
    assert '0366' not in mentioned, 'a folder is looked up by its number, with no zeros'


def test_scripts_are_read_once_per_job():
    seen = []
    source, context = state()
    context.shell = shell(qselect='9.s\n', scripts={'9.s': 'x'}, seen=seen)
    source.active_jobs(ALIASES)
    source.active_jobs(ALIASES)
    assert sum(cmd[0].endswith('printjob') for cmd in seen) == 1


def test_a_script_that_cannot_be_read_for_a_live_job_stops():
    source, context = state()
    context.shell = shell(qselect='9.s\n', scripts={})
    context.scheduler.is_job_active.return_value = True
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


COMMAND_JOB = (
    '"395.s":{\n"Job_Owner":"carol@h",\n'
    '"Submit_arguments":"-h -q test -- /bin/cp -r /lustre/alice/123/restart 456",\n'
    '"executable":"<jsdl-hpcpa:Executable>/bin/cp</jsdl-hpcpa:Executable>",\n'
    '"argument_list":"<jsdl-hpcpa:Argument>-r</jsdl-hpcpa:Argument>"\n}'
)
SCRIPT_JOB = '"396.s":{\n"Job_Owner":"dave@h",\n"Submit_arguments":"job.sh"\n}'


@pytest.mark.parametrize(
    'order', [(COMMAND_JOB, SCRIPT_JOB), (SCRIPT_JOB, COMMAND_JOB)]
)
def test_a_job_submitted_as_a_command_has_no_script_to_read(order):
    """
    qsub -- command: printjob -s exits 1 with "Job N not found" while the job is active.
    Seen on a cluster, where one such job stopped every scratch run. What it runs is in
    its submission, which is searched like any other job's.
    """
    source, context = state()
    context.shell = shell(
        qselect='395.s\n396.s\n',
        qstat='{"Jobs":{' + ','.join(order) + '}}',
        scripts={'396.s': 'echo 789\n'},
    )
    context.scheduler.is_job_active.return_value = True
    active = source.active_jobs(ALIASES)
    assert active.base_ids == {'395', '396'}
    assert ('alice', '123') in active.referenced_folders
    assert {'456', '789'} <= active.mentioned_ids


def test_an_interactive_job_has_no_script_to_read():
    qstat = '{"Jobs":{"9.s":{"Job_Owner":"alice@h","interactive":"True"}}}'
    source, context = state()
    context.shell = shell(qselect='9.s\n', qstat=qstat, scripts={})
    context.scheduler.is_job_active.return_value = True
    assert source.active_jobs(ALIASES).base_ids == {'9'}


@pytest.mark.parametrize(
    'order', [(COMMAND_JOB, SCRIPT_JOB), (SCRIPT_JOB, COMMAND_JOB)]
)
def test_a_command_job_does_not_excuse_another_jobs_unreadable_script(order):
    source, context = state()
    context.shell = shell(
        qselect='395.s\n396.s\n',
        qstat='{"Jobs":{' + ','.join(order) + '}}',
        scripts={},
    )
    context.scheduler.is_job_active.return_value = True
    with pytest.raises(StateUnavailable) as raised:
        source.active_jobs(ALIASES)
    assert '396.s' in str(raised.value)


def test_a_job_that_ended_while_being_read_is_not_a_failure():
    source, context = state()
    context.shell = shell(qselect='9.s\n', scripts={})
    context.scheduler.is_job_active.return_value = False
    assert source.active_jobs(ALIASES).base_ids == {'9'}


def test_printjob_missing_stops():
    source, context = state(qselect='9.s\n')

    def invoke(cmd, **_):
        if cmd[0].endswith('printjob'):
            raise FileNotFoundError(cmd[0])
        return SimpleNamespace(returncode=0, stdout='9.s\n')

    context.shell = SimpleNamespace(invoke=invoke)
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_active_owners_come_from_pbs_and_the_cache():
    qstat = (
        '{"Jobs":{"9.s":{"Job_Owner":"alice@10.0.0.5"},"10.s":{"Job_Owner":"Bob@h"}}}'
    )
    source, context = state(qselect='9.s\n10.s\n', qstat=qstat, tracked=[])
    context.job_cache.list_jobs.return_value = [
        SimpleNamespace(job_id='11', owner='carol')
    ]
    assert source.active_jobs(ALIASES).owners == {'alice', 'bob', 'carol'}


def test_an_active_job_with_no_owner_stops():
    qstat = '{"Jobs":{"9.s":{"Job_Owner":"alice@h"},"10.s":{}}}'
    source, _ = state(qselect='9.s\n10.s\n', qstat=qstat)
    with pytest.raises(StateUnavailable):
        source.active_jobs(ALIASES)


def test_a_subjob_in_the_expired_state_counts_as_finished_by_its_own_status():
    source, context = state()
    context.job_cache.list_completed_jobs.side_effect = lambda job_id: (
        [job(job_id='123[4]', job_state=SocaJobState.SUBJOB_EXPIRED)]
        if job_id == '123[4]'
        else []
    )
    found = source.finished_records({'123[4]', '123[]'})[0]
    assert found.finished is True
    assert found.succeeded is False, 'job() here has no exit status of its own'


def test_only_a_subjob_can_be_finished_by_the_expired_state():
    source, context = state()
    context.job_cache.list_completed_jobs.return_value = [
        job(job_state=SocaJobState.SUBJOB_EXPIRED)
    ]
    assert source.finished_records({'123'})[0].finished is False


def _succeeded(**fields):
    from ideascheduler.app.scratch_cleanup.job_state import job_succeeded

    values = dict(exit_status=0, disposition='ran')
    values.update(fields)
    found = job()
    for name, value in values.items():
        setattr(found, name, value)
    return job_succeeded(found)


def test_a_job_that_ran_and_exited_0_succeeded():
    assert _succeeded()


@pytest.mark.parametrize(
    'fields',
    [
        {'disposition': None},
        {'disposition': 'deleted'},
        {'disposition': 'failed'},
        {'disposition': 'held'},
        {'disposition': 'something-new'},
        {'exit_status': None},
        {'exit_status': 1},
        {'reason_class': 'cancelled'},
        {'comment': 'Job run at Thu Jan 22 on (host) and terminated'},
        {'comment': 'Job deleted by alice'},
        # PBS's own record of a qdel, with the comment replaced by an error message
        {'terminated': True, 'comment': 'capacity wait'},
    ],
    ids=str,
)
def test_anything_not_shown_to_have_completed_normally_is_failed(fields):
    assert not _succeeded(**fields)


def test_pbs_history_outranks_the_comment_text():
    """the comment can be an error message from an earlier wait, which says nothing of the end"""
    assert _succeeded(terminated=False, comment='spot instance terminated, retrying')


def test_an_old_record_with_no_disposition_is_on_the_failed_path():
    source, context = state()
    old = job()
    old.exit_status = 0
    context.job_cache.list_completed_jobs.return_value = [old]
    assert source.finished_records({'123'})[0].succeeded is False
