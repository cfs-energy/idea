"""One instance-monitor pass: page size and NextToken handling against a fake EC2 client."""

from unittest.mock import Mock

from ideasdk.aws.aws_util import AWSUtil
from ideascheduler.app.aws.instance_monitor import InstanceMonitor


def instance(number):
    return {'InstanceId': f'i-{number:017x}', 'State': {'Name': 'running'}}


def test_one_pass_reads_every_page_at_the_api_maximum():
    pages = [
        {
            'Reservations': [{'Instances': [instance(n) for n in range(1000)]}],
            'NextToken': 'page-2',
        },
        {'Reservations': [{'Instances': [instance(1000), instance(1001)]}]},
    ]
    ec2 = Mock()
    ec2.describe_instances.side_effect = pages
    aws_util = object.__new__(AWSUtil)
    aws_util._aws = Mock(ec2=Mock(return_value=ec2))
    context = Mock()
    context.aws_util.return_value = aws_util
    cache = Mock()
    monitor = InstanceMonitor(context, cache)
    monitor._topic = Mock()
    monitor._exit = Mock()
    monitor._exit.is_set.side_effect = [False, True]

    monitor._monitor_instances()

    assert [call.kwargs for call in ec2.describe_instances.call_args_list] == [
        {'Filters': [], 'MaxResults': 1000},
        {'Filters': [], 'MaxResults': 1000, 'NextToken': 'page-2'},
    ]
    synced = [
        item.instance_id
        for call in cache.sync.call_args_list
        for item in call.kwargs['instances']
    ]
    assert synced == [instance(n)['InstanceId'] for n in range(1002)]
    cache.sync_commit.assert_called_once()
    cache.sync_abort.assert_not_called()
