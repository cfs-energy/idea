"""
The submission hook against PBS-shaped objects (dev rc.17 B4, B8, B9).

the earlier doubles handed the hook plain strings and dicts, which json writes as they
are; PBS hands it objects. a movejob event's src_queue is a queue object, resource values
are pbs.size and pbs.duration, and a modifyjob job_o's resource list reads back no keys
(the dev cluster's hook request: "job_o": {..., "Resource_List": {}}), so every qmove
failed on json.dumps and an owner's qalter was checked against the queue defaults alone.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideadatamodel import SocaJobParams
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSJob
from test_validate_job_ncpus import (  # noqa: F401 (fixture)
    SOURCE_DNS,
    _restricted_fsx_move,
    _source_queue,
    submit,
)

HOOK = Path(__file__).parents[1] / 'resources/openpbs/hooks/openpbs_hook_handler.py'
MOVEJOB, MODIFYJOB = 0x08, 0x02
# the cluster's custom job resources, as roles/scheduler.sh writes them to resourcedef
CUSTOM = (
    'instance_type',
    'efa_support',
    'job_uid',
    'job_group',
    'compute_node',
    'stack_id',
    'fsx_lustre',
    'base_os',
    'instance_ami',
    'spot_price',
)


class PbsQueue:
    """pbs.queue: json cannot write it; its name is the queue's name"""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


class PbsValue:
    """pbs.size, pbs.duration, pbs.select: text through str() only"""

    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


class PbsResources:
    """pbs.pbs_resource: not a dict. listed=False is a modifyjob job_o: no keys, but each
    resource reads by name"""

    def __init__(self, values, listed=True):
        self._values = {key: PbsValue(value) for key, value in values.items()}
        self._listed = listed

    def keys(self):
        return list(self._values) if self._listed else []

    def __getitem__(self, key):
        return self._values.get(key)


class PbsJob:
    def __init__(self, **attributes):
        self.__dict__.update(attributes)

    def __getattr__(self, name):
        if name == 'Executable':
            raise AttributeError(f"'_job' object has no attribute '{name}'")
        return None


@pytest.fixture
def hook(monkeypatch, tmp_path):
    pbs = Mock()
    (tmp_path / 'server_priv').mkdir()
    (tmp_path / 'server_priv' / 'resourcedef').write_text(
        ''.join(f'{name} type=string\n' for name in CUSTOM)
    )
    pbs.pbs_conf = {'PBS_HOME': str(tmp_path)}
    monkeypatch.setitem(sys.modules, 'pbs', pbs)
    namespace = {}
    source = HOOK.read_text().split('\ne = pbs.event()\n', 1)[0]
    exec(compile(source, str(HOOK), 'exec'), namespace)
    namespace['log_fine'] = lambda message: None
    namespace['log_error'] = lambda message: None
    return namespace


def payload(hook, event):
    built = hook['OpenPBSEvent'](event=event).build()
    # what the hook sends over the socket
    return json.loads(json.dumps(built))


def event(kind, job, job_o=None, src_queue=None):
    return SimpleNamespace(
        type=kind,
        hook_name='validate_job',
        requestor='researcher',
        requestor_host='scheduler',
        hook_type='site',
        job=job,
        job_o=job_o,
        src_queue=src_queue,
    )


def moved(
    select='1:ncpus=4:compute_node=tbd',
    fsx=None,
    queue='normal',
    src='old-q',
    schedselect=None,
    **extra,
):
    resources = {
        'select': select,
        'walltime': '00:10:00',
        'mem': '4gb',
        'job_uid': 'uidmove1',
        'job_group': 'g27f8153e',
        **extra,
    }
    if fsx:
        resources['fsx_lustre'] = fsx
    if select is None:
        del resources['select']
    # a movejob's job lists no resource keys on the cluster's PBS and answers by name
    # (lane H: every movejob request sent "Resource_List": {}), and a provisioned job
    # carries its node in schedselect
    job = PbsJob(
        id='545.scheduler',
        queue=PbsQueue(queue),
        project='project-a',
        Job_Name='job',
        Job_Owner='researcher@scheduler',
        Resource_List=PbsResources(resources, listed=False),
        schedselect=PbsValue(schedselect or select or '1:ncpus=4:compute_node=tbd'),
    )
    return event(MOVEJOB, job, src_queue=PbsQueue(src))


def test_a_move_event_is_sent_as_plain_values(hook):
    sent = payload(hook, moved())
    assert sent['src_queue'] == 'old-q'
    assert sent['job']['queue'] == 'normal'
    assert sent['job']['Resource_List']['mem'] == '4gb'


def test_an_unbound_move_is_accepted(hook, submit):  # noqa: F811
    sent = payload(hook, moved(fsx=SOURCE_DNS))
    result, _ = _restricted_fsx_move(
        submit,
        SOURCE_DNS,
        src_queue=sent['src_queue'],
        source_profile=Mock(default_job_params=SocaJobParams()),
        job=OpenPBSJob(**sent['job']),
        queue_settings={},
    )
    assert result.accept is True, result.formatted_user_message
    before, after = stored_selector(sent['job'], result, 'movejob')
    assert after == before == ('1:ncpus=4:compute_node=tbd',) * 2


def test_a_bound_move_reaches_its_refusal(hook, submit):  # noqa: F811
    sent = payload(hook, moved(select='1:ncpus=4:compute_node=idea-dev-test-543'))
    result, _ = _restricted_fsx_move(
        submit, None, src_queue=sent['src_queue'], job=OpenPBSJob(**sent['job'])
    )
    assert result.accept is False
    assert 'This job already has a node from queue old-q' in (
        result.formatted_user_message
    )


def test_a_move_keeping_the_source_queues_fsx_reaches_its_refusal(hook, submit):  # noqa: F811
    from ideadatamodel.scheduler import SocaQueueManagementParams

    sent = payload(hook, moved(fsx=SOURCE_DNS))
    result, _ = _restricted_fsx_move(
        submit,
        SOURCE_DNS,
        src_queue=sent['src_queue'],
        source_profile=_source_queue(),
        job=OpenPBSJob(**sent['job']),
        defaults=SocaJobParams(instance_types=['c5n.18xlarge']),
        queue_settings={
            'queue_management_params': SocaQueueManagementParams(
                restricted_parameters=['fsx_lustre']
            )
        },
    )
    assert result.accept is False
    assert f'uses the FSx for Lustre file system {SOURCE_DNS}' in (
        result.formatted_user_message
    )


def owner_edit(hook, edit, current):
    job_o = PbsJob(
        id='536.scheduler',
        queue=PbsQueue('normal'),
        project='project-a',
        Job_Name='job',
        Job_Owner='researcher@scheduler',
        Resource_List=PbsResources(current, listed=False),
    )
    job = PbsJob(id='536.scheduler', Resource_List=PbsResources(edit))
    sent = payload(hook, event(MODIFYJOB, job, job_o=job_o))
    return sent


CURRENT = {
    'select': '1:ncpus=1:compute_node=tbd',
    'instance_type': 'c5.large',
    'efa_support': 'False',
    'job_uid': 'uid536keep',
    'job_group': 'g06692ffa',
    'walltime': '01:00:00',
}


def test_an_owners_edit_is_checked_against_the_jobs_own_request(hook, submit):  # noqa: F811
    sent = owner_edit(hook, {'instance_type': 'c5.xlarge'}, CURRENT)
    # the request before the edit reaches the scheduler, read by name
    assert sent['job_o']['Resource_List']['efa_support'] == 'False'
    assert sent['job_o']['Resource_List']['job_uid'] == 'uid536keep'
    # the queue asks for EFA by default; the job turned it off
    result, _ = submit(
        sent['job']['Resource_List'],
        event_type='modifyjob',
        defaults=SocaJobParams(instance_types=['c5.large'], enable_efa_support=True),
        job_o=OpenPBSJob(**sent['job_o']),
    )
    assert result.accept is True, result.formatted_user_message
    assert result.resources_updated['job_uid'] == 'uid536keep'
    assert result.resources_updated['job_group'] != 'g06692ffa'
    sent['job_o']['schedselect'] = CURRENT['select']
    before, after = stored_selector(sent['job_o'], result)
    assert after == before == (CURRENT['select'],) * 2


def test_an_owners_edit_that_is_really_invalid_is_refused_with_its_reason(hook, submit):  # noqa: F811
    sent = owner_edit(
        hook, {'instance_type': 'c5.xlarge'}, {**CURRENT, 'efa_support': 'True'}
    )
    result, _ = submit(
        sent['job']['Resource_List'],
        event_type='modifyjob',
        defaults=SocaJobParams(instance_types=['c5.large']),
        job_o=OpenPBSJob(**sent['job_o']),
    )
    assert result.accept is False
    assert 'EFA' in result.formatted_user_message


def test_the_group_stored_at_submission_is_the_one_the_first_read_computes(
    submit,  # noqa: F811
    context,
    monkeypatch,
):
    # B13: submission drew an unseeded subnet and the first read a seeded one, so a
    # multi-node on-demand job regrouped once at its first placement
    from test_held_job_provisioning import make_provisioner

    private_subnets = context.config().get_list('cluster.network.private_subnets')
    assert len(private_subnets) > 1
    # a job that has not failed provisioning yet
    monkeypatch.setattr(
        context,
        'job_cache',
        Mock(
            get_job_provisioning_retry_count=lambda job_id: 0,
            get_job=lambda job_id: None,
        ),
        raising=False,
    )
    subnets = set()
    for _ in range(6):
        result, hook_context = submit({'select': '2:ncpus=1'})
        assert result.accept is True, result.formatted_user_message
        stored = result.resources_updated
        # the job as the provisioner's live read sees it
        read = OpenPBSJob(
            id='571.scheduler',
            queue='normal',
            project='project-a',
            Job_Name='job',
            Resource_List={
                **{'select': stored['select'], 'nodect': '2'},
                'job_uid': stored['job_uid'],
                'job_group': stored['job_group'],
            },
        ).as_soca_job(
            context=context,
            queue_profile=context.queue_profiles.get_queue_profile.return_value,
        )
        written = []
        monkeypatch.setattr(
            context,
            'scheduler',
            Mock(
                set_job_attributes=lambda job_id, attributes: written.append(attributes)
            ),
            raising=False,
        )
        regrouped = make_provisioner(context)._regrouped(read)
        assert regrouped.job_group == stored['job_group']
        assert written == []
        assert len(read.params.subnet_ids) == 1
        subnets.add(read.params.subnet_ids[0])
    # each job, by its own uid, can still land in another subnet
    assert len(subnets) > 1


# the live re-test of lane H, as it ran: jobs on queue test moved to queue job-shared,
# whose defaults ask for EFA and restrict fsx_lustre in H3


def test_h1_a_move_is_checked_against_the_jobs_request_in_the_destination(
    hook,
    submit,  # noqa: F811
):
    # 574: a held c5.large job; accepted against an empty request, it took job-shared's
    # EFA default and its stack failed twice ("EFA interfaces are not supported on c5.large")
    sent = payload(
        hook,
        moved(
            select='1:ncpus=1:compute_node=tbd',
            queue='job-shared',
            src='test',
            instance_type='c5.large',
        ),
    )
    assert sent['job']['Resource_List']['instance_type'] == 'c5.large'
    result, _ = submit(
        {},
        event_type='movejob',
        queue='job-shared',
        src_queue=sent['src_queue'],
        job=OpenPBSJob(**sent['job']),
        defaults=SocaJobParams(
            instance_types=['c5n.18xlarge'], enable_efa_support=True
        ),
    )
    # refused with the true reason instead of a job that can never start
    assert result.accept is False
    assert 'EFA' in result.formatted_user_message


def test_h1_a_job_that_turned_efa_off_keeps_its_choice_in_the_destination(
    hook,
    submit,  # noqa: F811
):
    sent = payload(
        hook,
        moved(
            select='1:ncpus=1:compute_node=tbd',
            queue='job-shared',
            src='test',
            instance_type='c5.large',
            efa_support='False',
        ),
    )
    result, context = submit(
        {},
        event_type='movejob',
        queue='job-shared',
        src_queue=sent['src_queue'],
        job=OpenPBSJob(**sent['job']),
        defaults=SocaJobParams(
            instance_types=['c5n.18xlarge'], enable_efa_support=True
        ),
    )
    assert result.accept is True, result.formatted_user_message
    # its own choices, not the destination's defaults
    assert context.job.params.instance_types == ['c5.large']
    assert context.job.params.enable_efa_support is False
    before, after = stored_selector(sent['job'], result, 'movejob')
    assert after == before == ('1:ncpus=1:compute_node=tbd',) * 2


@pytest.mark.parametrize('select_names_node', [True, False])
def test_h2_a_bound_move_gets_the_guidance_not_an_internal_error(
    hook,
    submit,  # noqa: F811
    select_names_node,
):
    # 575: bound (schedselect compute_node=idea-dev-test-ondemand-575); refused with
    # "Invalid Job: params.job_group not found"
    node = '1:ncpus=1:compute_node=idea-dev-test-ondemand-575'
    sent = payload(
        hook,
        moved(
            select=node if select_names_node else None,
            schedselect=node,
            queue='job-shared',
            src='test',
            instance_type='c5.large',
        ),
    )
    result, _ = submit(
        {},
        event_type='movejob',
        queue='job-shared',
        src_queue=sent['src_queue'],
        job=OpenPBSJob(**sent['job']),
        defaults=SocaJobParams(instance_types=['c5.large']),
    )
    assert result.accept is False
    assert (
        'This job already has a node from queue test, so it cannot be moved to queue '
        'job-shared. Delete it and submit it to queue job-shared, or move a job before '
        'it is given a node.'
    ) in result.formatted_user_message
    assert 'job_group not found' not in result.formatted_user_message


def test_h3_a_move_carrying_the_source_queues_fsx_into_a_restricting_queue_is_refused(
    hook,
    submit,  # noqa: F811
):
    # 577: test's default existing Lustre stamped on the job; job-shared restricts
    # fsx_lustre; the move was accepted and the job kept the source queue's file system
    from ideadatamodel import SocaFSxLustreConfig
    from ideadatamodel.scheduler import SocaQueueManagementParams

    dns = 'fs-0910bae4c4a6e758a.fsx.us-east-2.amazonaws.com'
    sent = payload(
        hook,
        moved(
            select='1:ncpus=1:compute_node=tbd',
            queue='job-shared',
            src='test',
            fsx=dns,
            instance_type='c5.large',
        ),
    )
    assert sent['job']['Resource_List']['fsx_lustre'] == dns
    result, _ = submit(
        {},
        event_type='movejob',
        queue='job-shared',
        src_queue=sent['src_queue'],
        job=OpenPBSJob(**sent['job']),
        defaults=SocaJobParams(instance_types=['c5.large']),
        queue_settings={
            'queue_management_params': SocaQueueManagementParams(
                restricted_parameters=['fsx_lustre']
            )
        },
        source_profile=Mock(
            default_job_params=SocaJobParams(
                fsx_lustre=SocaFSxLustreConfig(enabled=True, existing_fsx=dns)
            )
        ),
    )
    assert result.accept is False
    assert f'uses the FSx for Lustre file system {dns}, the default of queue test' in (
        result.formatted_user_message
    )


# the stored selector before and after an accepted edit or move. PBS keeps the request's
# select and rebuilds schedselect from it whenever select is written


def stored_selector(sent_job, result, event_type='modifyjob'):
    """(select, schedselect) before, and after the hook applies the scheduler's writes;
    the hook writes nothing back on a move (movejob may only read the job)"""
    before_select = (sent_job.get('Resource_List') or {}).get('select')
    before_sched = sent_job.get('schedselect') or before_select
    written = (result.resources_updated or {}).get('select')
    if written is None or event_type == 'movejob':
        return (before_select, before_sched), (before_select, before_sched)
    return (before_select, before_sched), (written, written)


TWO_CHUNKS = (
    '1:ncpus=2:mem=8gb:mpiprocs=2:compute_node=tbd'
    '+1:ncpus=4:mem=16gb:mpiprocs=4:compute_node=tbd'
)


def edited(hook, submit, edit, current, **kwargs):  # noqa: F811
    sent = owner_edit(hook, edit, current)
    sent['job_o']['schedselect'] = current['select']
    result, _ = submit(
        sent['job']['Resource_List'],
        event_type='modifyjob',
        job_o=OpenPBSJob(**sent['job_o']),
        **kwargs,
    )
    assert result.accept is True, result.formatted_user_message
    return sent, result


def test_a_walltime_edit_leaves_a_two_chunk_select_exactly_as_it_was(hook, submit):  # noqa: F811
    # rc19 sign-off: the selector was rebuilt from the node count, '2:compute_node=tbd',
    # dropping both chunks' CPUs, memory and MPI ranks
    sent, result = edited(
        hook,
        submit,
        {'walltime': '02:00:00'},
        {**CURRENT, 'select': TWO_CHUNKS, 'instance_type': 'c5n.18xlarge'},
        defaults=SocaJobParams(instance_types=['c5n.18xlarge']),
    )
    before, after = stored_selector(sent['job_o'], result)
    assert after == before == (TWO_CHUNKS, TWO_CHUNKS)


def test_an_instance_type_edit_keeps_the_jobs_cpus(hook, submit):  # noqa: F811
    # live job 633: '1:ncpus=1:compute_node=tbd' became '1:compute_node=tbd'
    sent, result = edited(
        hook,
        submit,
        {'instance_type': 'c5.xlarge'},
        CURRENT,
        defaults=SocaJobParams(instance_types=['c5.large']),
    )
    before, after = stored_selector(sent['job_o'], result)
    assert after == before == ('1:ncpus=1:compute_node=tbd',) * 2


def test_an_edit_of_the_select_itself_is_applied_as_given(hook, submit):  # noqa: F811
    sent, result = edited(
        hook,
        submit,
        {'select': '2:ncpus=1:mem=2gb'},
        CURRENT,
        defaults=SocaJobParams(instance_types=['c5.large']),
    )
    _, after = stored_selector(sent['job_o'], result)
    assert after == ('2:ncpus=1:mem=2gb:compute_node=tbd',) * 2


def test_an_edit_of_a_bound_job_keeps_its_node_and_selector(hook, submit):  # noqa: F811
    bound = '1:ncpus=1:compute_node=idea-mock-test-ondemand-5'
    sent, result = edited(
        hook,
        submit,
        {'walltime': '02:00:00'},
        {**CURRENT, 'select': bound, 'job_group': 'g06692ffa'},
        defaults=SocaJobParams(instance_types=['c5.large']),
    )
    before, after = stored_selector(sent['job_o'], result)
    assert after == before == (bound, bound)
    assert 'stack_id' not in result.resources_updated


def test_an_accepted_move_of_a_two_chunk_job_keeps_its_chunks(hook, submit):  # noqa: F811
    sent = payload(
        hook,
        moved(
            select=TWO_CHUNKS,
            queue='job-shared',
            src='test',
            instance_type='c5n.18xlarge',
            efa_support='False',
        ),
    )
    result, _ = submit(
        {},
        event_type='movejob',
        queue='job-shared',
        src_queue=sent['src_queue'],
        job=OpenPBSJob(**sent['job']),
        defaults=SocaJobParams(
            instance_types=['c5n.18xlarge'], enable_efa_support=True
        ),
    )
    assert result.accept is True, result.formatted_user_message
    before, after = stored_selector(sent['job'], result, 'movejob')
    assert after == before
    assert before[0] == TWO_CHUNKS
    # nor is one rebuilt for it: the answer names the job's own chunks
    assert result.resources_updated['select'] == TWO_CHUNKS
