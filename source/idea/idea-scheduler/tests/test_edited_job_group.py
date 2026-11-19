"""
An accepted edit of a waiting job takes the group of its edited request (dev rc.16 3b).

the group computed at submission was carried into the edit, so a batch job edited to
another instance type kept the old group, was placed on that group's stack and refused
there on every cycle, and its wait kept that stack's idle nodes up.
"""

from ideadatamodel import SocaJobParams
from ideascheduler.app.scheduler.openpbs.openpbs_model import OpenPBSJob
from test_validate_job_ncpus import submit  # noqa: F401 (fixture)

DEFAULTS = SocaJobParams(
    instance_types=['c5.large'], instance_ami='ami-mock', base_os='amazonlinux2023'
)


def _submitted_group(submit, instance_type):  # noqa: F811
    result, _ = submit(
        {'select': '1:ncpus=1', 'instance_type': instance_type}, defaults=DEFAULTS
    )
    assert result.accept is True, result.formatted_user_message
    return result.resources_updated['job_group']


def _edit(submit, job_o_resources, edit):  # noqa: F811
    result, _ = submit(
        edit,
        event_type='modifyjob',
        defaults=DEFAULTS,
        job_o=OpenPBSJob(
            id='508.scheduler',
            queue='normal',
            project='project-a',
            Resource_List=job_o_resources,
        ),
    )
    assert result.accept is True, result.formatted_user_message
    return result


def test_an_edit_that_changes_the_request_takes_the_edited_requests_group(submit):  # noqa: F811
    old_group = _submitted_group(submit, 'c5.large')
    new_group = _submitted_group(submit, 'c5.xlarge')
    assert old_group != new_group

    result = _edit(
        submit,
        {
            'select': '1:ncpus=1:compute_node=tbd',
            'instance_type': 'c5.large',
            'job_group': old_group,
        },
        {'instance_type': 'c5.xlarge'},
    )

    # grouped and placed as a new c5.xlarge request, not on the c5.large group's stack
    assert result.resources_updated['job_group'] == new_group


def test_an_edit_that_leaves_the_request_keeps_its_group(submit):  # noqa: F811
    group = _submitted_group(submit, 'c5.large')
    result = _edit(
        submit,
        {
            'select': '1:ncpus=1:compute_node=tbd',
            'instance_type': 'c5.large',
            'job_group': group,
        },
        {'walltime': '00:10:00'},
    )
    assert result.resources_updated['job_group'] == group


def test_a_custom_group_is_kept(submit):  # noqa: F811
    # a custom group is the submitter's or the queue's
    result = _edit(
        submit,
        {
            'select': '1:ncpus=1:compute_node=tbd',
            'instance_type': 'c5.large',
            'job_group': 'custom-batch',
        },
        {'instance_type': 'c5.xlarge'},
    )
    assert result.resources_updated['job_group'] == 'custom-batch'
