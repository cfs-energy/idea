"""
`ideactl update-software-stacks` AMI lookup.

Two ways it used to pick the wrong image: a vendor republishing an older minor made it the
newest by date (RHEL-9.6.0 dated 2026-08-11 beat RHEL-9.8.0 dated 2026-07-28), and in the
aws-us-gov partition Red Hat and Canonical publish from different accounts, so the commercial
owner ids matched nothing there.
"""

from fnmatch import fnmatch
from unittest.mock import Mock

import pytest

from ideavirtualdesktopcontroller.cli.software_stacks import (
    AMI_PATTERNS,
    find_latest_ami,
    image_sort_key,
)


def fake_ec2(region: str, images: list) -> Mock:
    client = Mock()
    client.meta.region_name = region
    client.describe_images.return_value = {'Images': images}
    return client


def test_highest_minor_beats_newer_date():
    images = [
        {
            'ImageId': 'ami-96',
            'Name': 'RHEL-9.6.0_HVM-20260811-x86_64-0-Hourly2-GP3',
            'CreationDate': '2026-08-11T00:00:00.000Z',
        },
        {
            'ImageId': 'ami-98',
            'Name': 'RHEL-9.8.0_HVM-20260728-x86_64-0-Hourly2-GP3',
            'CreationDate': '2026-07-28T00:00:00.000Z',
        },
    ]
    assert (
        find_latest_ami(
            fake_ec2('us-east-2', images), 'RHEL-9.*', ['amazon'], Mock(), 'rhel9'
        )
        == 'ami-98'
    )


def test_names_without_a_minor_sort_by_date():
    older = {
        'Name': 'Windows_Server-2022-English-Full-Base-2026.07.16',
        'CreationDate': '2026-07-16T00:00:00.000Z',
    }
    newer = {
        'Name': 'Windows_Server-2022-English-Full-Base-2026.08.12',
        'CreationDate': '2026-08-12T00:00:00.000Z',
    }
    assert image_sort_key(newer) > image_sort_key(older)


@pytest.mark.parametrize(
    ('pattern_key', 'sample_name'),
    [
        ('rocky8/x86-64', 'Rocky-8-EC2-Base-8.10-20250612.0.x86_64'),
        ('rocky9/x86-64', 'Rocky-9-EC2-Base-9.6-20250531.0.x86_64'),
        ('rocky10/x86-64', 'Rocky-10-EC2-Base-10.0-20250612.0.x86_64'),
    ],
)
def test_rocky_patterns_match_resf_image_names(pattern_key, sample_name):
    pattern = AMI_PATTERNS[pattern_key]
    assert fnmatch(sample_name, pattern)
    assert not pattern.endswith('-*')


def test_govcloud_uses_the_gov_partition_publishers():
    client = fake_ec2('us-gov-west-1', [])
    find_latest_ami(client, 'RHEL-9.*', ['amazon'], Mock(), 'rhel9')
    assert client.describe_images.call_args.kwargs['Owners'] == ['219670896067']
    client = fake_ec2('us-east-2', [])
    find_latest_ami(client, 'RHEL-9.*', ['amazon'], Mock(), 'rhel9')
    assert client.describe_images.call_args.kwargs['Owners'] == ['309956199498']
