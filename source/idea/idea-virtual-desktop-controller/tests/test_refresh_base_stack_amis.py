"""
Server-side base stack AMI refresh (VirtualDesktopAdmin.RefreshBaseSoftwareStackAmis).

The utils method walks ss-base-* rows, resolves the newest AMI through the same
resolver ideactl uses, updates changed rows and reindexes them. One stack's EC2
failure must not stop the others. Elevated access is enforced centrally in
VirtualDesktopAdminAPI.invoke, so authorization is not re-tested here.
"""

from typing import Optional
from unittest.mock import Mock

from ideadatamodel import SocaListingPayload, VirtualDesktopSoftwareStack
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_utils import (
    VirtualDesktopSoftwareStackUtils,
)


class FakeStackDb:
    def __init__(self, stacks):
        self.stacks = stacks
        self.updated = []

    def list_all_from_db(self, request):
        return SocaListingPayload(listing=list(self.stacks))

    def update(self, software_stack):
        self.updated.append(software_stack)
        return software_stack


class FakeEc2Meta:
    region_name = 'us-east-2'


class FakeEc2:
    """returns one image per describe_images call; a name in FAIL raises instead"""

    def __init__(
        self,
        image_id: str,
        name: str,
        fail_patterns=(),
        architecture='x86_64',
        owned=(),
    ):
        self.image_id = image_id
        self.name = name
        self.fail_patterns = tuple(fail_patterns)
        self.architecture = architecture
        self.owned = set(owned)
        self.meta = FakeEc2Meta()

    def describe_images(self, **kwargs):
        if 'ImageIds' in kwargs:
            # the built-image check: only ids this account owns come back
            return {
                'Images': [
                    {'ImageId': i} for i in kwargs['ImageIds'] if i in self.owned
                ]
            }
        pattern = kwargs['Filters'][0]['Values'][0]
        for fail in self.fail_patterns:
            if fail in pattern:
                raise RuntimeError('ec2 unavailable')
        return {
            'Images': [
                {
                    'ImageId': self.image_id,
                    'Name': self.name,
                    'CreationDate': '2026-08-01T00:00:00.000Z',
                    'ImageLocation': f'amazon/{self.name}',
                    'State': 'available',
                    'Architecture': self.architecture,
                }
            ]
        }


def a_stack(
    stack_id: str, ami_id: str, base_ami_id: str = None
) -> VirtualDesktopSoftwareStack:
    return VirtualDesktopSoftwareStack(
        stack_id=stack_id, ami_id=ami_id, base_ami_id=base_ami_id
    )


def build_utils(
    db: FakeStackDb, ec2: Optional[FakeEc2] = None
) -> VirtualDesktopSoftwareStackUtils:
    utils = object.__new__(VirtualDesktopSoftwareStackUtils)
    utils._software_stack_db = db
    utils._logger = Mock()
    context = Mock()
    context.aws.return_value.ec2.return_value = ec2
    utils.context = context
    utils.index_software_stack_entry_to_opensearch = Mock()
    return utils


def test_a_newer_ami_updates_the_row_and_reindexes():
    db = FakeStackDb(
        [a_stack('ss-base-amazonlinux2023-x86-64-base', 'ami-oldoldoldoldold01')]
    )
    ec2 = FakeEc2(
        'ami-newnewnewnewnew01', 'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64'
    )
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis()

    assert [r.status for r in results] == ['updated']
    assert results[0].old_ami == 'ami-oldoldoldoldold01'
    assert results[0].new_ami == 'ami-newnewnewnewnew01'
    assert results[0].new_base_ami == 'ami-newnewnewnewnew01'
    assert db.updated[0].ami_id == 'ami-newnewnewnewnew01'
    assert db.updated[0].base_ami_id == 'ami-newnewnewnewnew01'
    utils.index_software_stack_entry_to_opensearch.assert_called_once()


def test_a_current_ami_is_left_alone():
    db = FakeStackDb(
        [a_stack('ss-base-amazonlinux2023-x86-64-base', 'ami-newnewnewnewnew01')]
    )
    ec2 = FakeEc2(
        'ami-newnewnewnewnew01', 'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64'
    )
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis()

    assert [r.status for r in results] == ['up_to_date']
    assert db.updated == []
    utils.index_software_stack_entry_to_opensearch.assert_not_called()


def test_one_failing_stack_does_not_stop_the_rest():
    db = FakeStackDb(
        [
            a_stack('ss-base-rhel9-x86-64-base', 'ami-oldoldoldoldold01'),
            a_stack('ss-base-amazonlinux2023-x86-64-base', 'ami-oldoldoldoldold02'),
        ]
    )
    ec2 = FakeEc2(
        'ami-newnewnewnewnew01',
        'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64',
        fail_patterns=('RHEL-9',),
    )
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis()

    by_id = {r.stack_id: r for r in results}
    assert by_id['ss-base-rhel9-x86-64-base'].status == 'error'
    # the cli resolver swallows the EC2 error and returns None, so the message
    # is the no-match one; what matters is a truthy message and isolation
    assert by_id['ss-base-rhel9-x86-64-base'].message
    assert by_id['ss-base-amazonlinux2023-x86-64-base'].status == 'updated'
    assert len(db.updated) == 1


def test_non_base_stacks_are_skipped():
    db = FakeStackDb([a_stack('my-custom-stack', 'ami-oldoldoldoldold01')])
    utils = build_utils(db, FakeEc2('ami-x', 'irrelevant'))

    results = utils.refresh_base_software_stack_amis()

    assert results == []
    assert db.updated == []


def test_selected_ids_refresh_only_those():
    db = FakeStackDb(
        [
            a_stack('ss-base-amazonlinux2023-x86-64-base', 'ami-oldoldoldoldold01'),
            a_stack('ss-base-amazonlinux2023-arm64-base', 'ami-oldoldoldoldold02'),
        ]
    )
    ec2 = FakeEc2(
        'ami-newnewnewnewnew01', 'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64'
    )
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis(
        stack_ids=['ss-base-amazonlinux2023-x86-64-base']
    )

    assert [r.stack_id for r in results] == ['ss-base-amazonlinux2023-x86-64-base']
    assert results[0].status == 'updated'
    assert len(db.updated) == 1


def test_unknown_and_non_base_ids_error_without_stopping_the_rest():
    db = FakeStackDb(
        [a_stack('ss-base-amazonlinux2023-x86-64-base', 'ami-oldoldoldoldold01')]
    )
    ec2 = FakeEc2(
        'ami-newnewnewnewnew01', 'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64'
    )
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis(
        stack_ids=[
            'ss-base-amazonlinux2023-x86-64-base',
            'my-custom-stack',
            'ss-base-ghost-x86-64-base',
        ]
    )

    by_id = {r.stack_id: r for r in results}
    assert by_id['ss-base-amazonlinux2023-x86-64-base'].status == 'updated'
    assert by_id['my-custom-stack'].status == 'error'
    assert by_id['my-custom-stack'].message == 'not a refreshable base stack'
    assert by_id['ss-base-ghost-x86-64-base'].status == 'error'
    assert by_id['ss-base-ghost-x86-64-base'].message == 'not a refreshable base stack'
    assert len(db.updated) == 1


def test_the_row_reports_updated_even_when_the_index_write_fails():
    db = FakeStackDb(
        [a_stack('ss-base-amazonlinux2023-x86-64-base', 'ami-oldoldoldoldold01')]
    )
    ec2 = FakeEc2(
        'ami-newnewnewnewnew01', 'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64'
    )
    utils = build_utils(db, ec2)
    utils.index_software_stack_entry_to_opensearch = Mock(
        side_effect=RuntimeError('analytics sink down')
    )

    results = utils.refresh_base_software_stack_amis()

    assert results[0].status == 'updated'
    assert results[0].new_ami == 'ami-newnewnewnewnew01'
    assert 'index' in results[0].message
    assert db.updated[0].ami_id == 'ami-newnewnewnewnew01'


def test_an_arm64_match_never_lands_on_an_x86_64_stack():
    db = FakeStackDb(
        [a_stack('ss-base-windows2022-x86-64-base', 'ami-oldoldoldoldold01')]
    )
    ec2 = FakeEc2(
        'ami-armarmarmarmarm01',
        'Windows_Server-2022-English-Full-Base-2026.08.12',
        architecture='arm64',
    )
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis()

    assert results[0].status == 'error'
    assert 'is arm64' in results[0].message
    assert db.updated == []


def test_gov_rocky_rows_say_unsupported_instead_of_no_match():
    db = FakeStackDb([a_stack('ss-base-rocky9-x86-64-base', 'ami-oldoldoldoldold01')])
    ec2 = FakeEc2('ami-x', 'irrelevant')
    ec2.meta = Mock(region_name='us-gov-west-1')
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis()

    assert results[0].status == 'error'
    assert 'GovCloud' in results[0].message
    assert db.updated == []


def test_a_built_image_survives_the_refresh_and_only_the_base_moves():
    db = FakeStackDb(
        [
            a_stack(
                'ss-base-amazonlinux2023-x86-64-base',
                'ami-builtbuiltbuilt1',
                base_ami_id='ami-oldoldoldoldold01',
            )
        ]
    )
    ec2 = FakeEc2(
        'ami-newnewnewnewnew01',
        'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64',
        owned={'ami-builtbuiltbuilt1'},
    )
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis()

    assert results[0].status == 'base_updated'
    assert results[0].new_ami is None
    assert results[0].new_base_ami == 'ami-newnewnewnewnew01'
    assert 'still launches from built image ami-builtbuiltbuilt1' in results[0].message
    assert 'rebuild' in results[0].message
    assert db.updated[0].ami_id == 'ami-builtbuiltbuilt1'
    assert db.updated[0].base_ami_id == 'ami-newnewnewnewnew01'


def test_a_built_image_with_the_newest_base_is_up_to_date():
    db = FakeStackDb(
        [
            a_stack(
                'ss-base-amazonlinux2023-x86-64-base',
                'ami-builtbuiltbuilt1',
                base_ami_id='ami-newnewnewnewnew01',
            )
        ]
    )
    ec2 = FakeEc2(
        'ami-newnewnewnewnew01',
        'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64',
        owned={'ami-builtbuiltbuilt1'},
    )
    utils = build_utils(db, ec2)

    results = utils.refresh_base_software_stack_amis()

    assert results[0].status == 'up_to_date'
    assert db.updated == []
