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
Test Cases for AWSUtil instance class <-> service quota mapping
"""

from ideasdk.aws.aws_util import AWSUtil

import pytest


# every HPC family AWS has shipped, plus a synthetic next-generation name. IDEA must not
# need a code change to classify a family it has never heard of.
HPC_INSTANCE_TYPES = [
    'hpc6a.48xlarge',
    'hpc6id.32xlarge',
    'hpc7a.12xlarge',
    'hpc7a.96xlarge',
    'hpc7g.16xlarge',
    'hpc8a.96xlarge',
    'hpc9z.96xlarge',
]


@pytest.mark.parametrize('instance_type', HPC_INSTANCE_TYPES)
def test_hpc_families_map_to_hpc_class(instance_type):
    instance_class, standard = AWSUtil.get_instance_type_class(
        instance_type=instance_type
    )
    assert instance_class == 'HPC'
    assert standard is False


@pytest.mark.parametrize(
    'instance_type,expected_class',
    [
        ('h1.16xlarge', 'H'),
        ('c5n.18xlarge', 'C'),
        ('inf2.xlarge', 'Inf'),
        ('i4i.large', 'I'),
        ('dl1.24xlarge', 'DL'),
        ('d3.xlarge', 'D'),
        ('trn1.32xlarge', 'Trn'),
        ('t3.micro', 'T'),
        ('mac2.metal', 'mac'),
        ('m5.large', 'M'),
        ('u-6tb1.112xlarge', 'HighMem'),
    ],
)
def test_neighbouring_classes_unchanged(instance_type, expected_class):
    """
    the classes whose first letter collides with a longer family prefix
    """
    instance_class, _ = AWSUtil.get_instance_type_class(instance_type=instance_type)
    assert instance_class == expected_class


class _StubAWSUtil(AWSUtil):
    """AWSUtil with the instance type inventory stubbed out - no context, no AWS calls."""

    def __init__(self, instance_types):
        self._instance_types = set(instance_types)

    def get_all_instance_types(self):
        return self._instance_types


def test_instance_types_for_class_does_not_leak_across_quotas():
    """
    the Standard quota covers (A, C, D, H, I, M, R, T, Z). HPC, Inf, DL, Trn and mac each
    have their own EC2 service quota, and their family names begin with the letter of a
    Standard class - they must not be counted against it.
    """
    aws_util = _StubAWSUtil(
        [
            'hpc7a.96xlarge',
            'hpc8a.96xlarge',
            'h1.16xlarge',
            'inf2.xlarge',
            'i4i.large',
            'dl1.24xlarge',
            'd3.xlarge',
            'trn1.32xlarge',
            't3.micro',
            'mac2.metal',
            'm5.large',
            'u-6tb1.112xlarge',
        ]
    )

    assert sorted(aws_util.get_instance_types_for_class('HPC')) == [
        'hpc7a.96xlarge',
        'hpc8a.96xlarge',
    ]
    assert aws_util.get_instance_types_for_class('H') == ['h1.16xlarge']
    assert aws_util.get_instance_types_for_class('I') == ['i4i.large']
    assert aws_util.get_instance_types_for_class('D') == ['d3.xlarge']
    assert aws_util.get_instance_types_for_class('T') == ['t3.micro']
    assert aws_util.get_instance_types_for_class('M') == ['m5.large']
    assert aws_util.get_instance_types_for_class('Inf') == ['inf2.xlarge']
    assert aws_util.get_instance_types_for_class('DL') == ['dl1.24xlarge']
    assert aws_util.get_instance_types_for_class('Trn') == ['trn1.32xlarge']
    assert aws_util.get_instance_types_for_class('mac') == ['mac2.metal']
    assert aws_util.get_instance_types_for_class('HighMem') == ['u-6tb1.112xlarge']
