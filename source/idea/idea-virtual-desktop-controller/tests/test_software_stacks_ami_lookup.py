#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
`ideactl update-software-stacks` AMI lookup.

Two ways it used to pick the wrong image: a vendor republishing an older minor made it the
newest by date (RHEL-9.6.0 dated 2026-08-11 beat RHEL-9.8.0 dated 2026-07-28), and in the
aws-us-gov partition Red Hat and Canonical publish from different accounts, so the commercial
owner ids matched nothing there.
"""

from unittest.mock import Mock

from ideavirtualdesktopcontroller.cli.software_stacks import (
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


def test_govcloud_uses_the_gov_partition_publishers():
    client = fake_ec2('us-gov-west-1', [])
    find_latest_ami(client, 'RHEL-9.*', ['amazon'], Mock(), 'rhel9')
    assert client.describe_images.call_args.kwargs['Owners'] == ['219670896067']
    client = fake_ec2('us-east-2', [])
    find_latest_ami(client, 'RHEL-9.*', ['amazon'], Mock(), 'rhel9')
    assert client.describe_images.call_args.kwargs['Owners'] == ['309956199498']
