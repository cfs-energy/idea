"""SP-16 / SDK-18 (reserved instance count) and DM-20 (queue values stored as decimals)"""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from ideasdk.aws.aws_util import AWSUtil
from ideasdk.utils import Utils


def reservations_util(reservations):
    util = AWSUtil.__new__(AWSUtil)
    cache = Mock()
    cache.short_term.return_value.get.return_value = None
    util._context = Mock(cache=lambda: cache)
    ec2 = Mock()
    ec2.describe_reserved_instances.return_value = {'ReservedInstances': reservations}
    util.aws = lambda: Mock(ec2=lambda: ec2)
    return util


@pytest.mark.parametrize('counts,expected', [([3], 3), ([3, 2], 5)])
def test_active_reservations_of_one_type_are_summed(counts, expected):
    util = reservations_util(
        [{'InstanceType': 'c5.large', 'InstanceCount': count} for count in counts]
    )
    assert util.ec2_describe_reserved_instances(instance_types=['c5.large']) == {
        'c5.large': expected
    }


@pytest.mark.parametrize('value', [0.1, 0.35, 1200.0, 3])
def test_a_queue_value_is_stored_as_written(value):
    from boto3.dynamodb.types import TypeSerializer

    decimal = Utils.get_as_decimal(value)
    assert decimal == Decimal(str(value))
    # DynamoDB's serializer traps inexact values; Decimal(0.1) failed here
    assert TypeSerializer().serialize(decimal) == {'N': str(Decimal(str(value)))}
