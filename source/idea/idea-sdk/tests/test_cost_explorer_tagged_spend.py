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
Test Cases for the Cost Explorer per service read

Every request to Cost Explorer is billed, so what is asked for and how often is
asserted alongside what is parsed. No AWS calls are made.
"""

from ideasdk.aws.aws_util import AWSUtil, COST_EXPLORER_UNAVAILABLE

import arrow
import pytest

TAG_KEY = 'idea:Project'
TAG_VALUE = 'research'

BEDROCK_SERVICE = 'Amazon Bedrock'
EC2_SERVICE = 'Amazon Elastic Compute Cloud - Compute'


def group(service: str, amount: str):
    return {'Keys': [service], 'Metrics': {'UnblendedCost': {'Amount': amount}}}


class StubCostExplorer:
    def __init__(self, response=None, raises=False):
        self.response = response if response is not None else {'ResultsByTime': []}
        self.raises = raises
        self.requests = []

    def get_cost_and_usage(self, **kwargs):
        self.requests.append(kwargs)
        if self.raises:
            raise RuntimeError('AccessDeniedException')
        return self.response


class StubAwsClientProvider:
    def __init__(self, cost_explorer, partition='aws'):
        self._cost_explorer = cost_explorer
        self._partition = partition

    def aws_partition(self):
        return self._partition

    def cost_explorer(self):
        return self._cost_explorer


class StubCache:
    def __init__(self):
        self.entries = {}
        self.ttls = {}

    def get(self, key, default=None):
        return self.entries.get(key, default)

    def set(self, key, value, ttl=None):
        self.entries[key] = value
        self.ttls[key] = ttl

    def delete(self, key):
        self.entries.pop(key, None)


class StubCacheProvider:
    def __init__(self):
        self._long_term = StubCache()

    def long_term(self):
        return self._long_term

    def short_term(self):
        return self._long_term


class StubLogger:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args, **kwargs):
        self.messages.append(str(message))


class StubContext:
    def __init__(self):
        self._cache = StubCacheProvider()

    def cache(self):
        return self._cache


class StubAWSUtil(AWSUtil):
    """AWSUtil with only what the cost explorer read touches - no AWS, no instance db"""

    def __init__(self, cost_explorer, partition='aws'):
        self._context = StubContext()
        self._logger = StubLogger()
        self._aws = StubAwsClientProvider(cost_explorer, partition=partition)


def test_spend_is_reported_per_service_for_the_tag_asked_for():
    cost_explorer = StubCostExplorer(
        {
            'ResultsByTime': [
                {'Groups': [group(BEDROCK_SERVICE, '30.5'), group(EC2_SERVICE, '69.5')]}
            ]
        }
    )
    aws_util = StubAWSUtil(cost_explorer)

    spend = aws_util.cost_explorer_get_tagged_service_spend(
        tag_key=TAG_KEY, tag_value=TAG_VALUE
    )
    assert spend == {
        BEDROCK_SERVICE: pytest.approx(30.5),
        EC2_SERVICE: pytest.approx(69.5),
    }

    request = cost_explorer.requests[0]
    assert request['Filter'] == {'Tags': {'Key': TAG_KEY, 'Values': [TAG_VALUE]}}
    assert request['GroupBy'] == [{'Type': 'DIMENSION', 'Key': 'SERVICE'}]
    # month to date, and End is exclusive so today is only included by asking past it
    assert request['TimePeriod']['Start'] == arrow.utcnow().floor('month').format(
        'YYYY-MM-DD'
    )
    assert request['TimePeriod']['End'] > arrow.utcnow().format('YYYY-MM-DD')


def test_a_tag_nothing_was_priced_against_reads_as_no_spend():
    aws_util = StubAWSUtil(StubCostExplorer({'ResultsByTime': [{'Groups': []}]}))
    assert (
        aws_util.cost_explorer_get_tagged_service_spend(
            tag_key=TAG_KEY, tag_value=TAG_VALUE
        )
        == {}
    )


def test_the_answer_is_read_once_and_then_served_from_cache():
    cost_explorer = StubCostExplorer(
        {'ResultsByTime': [{'Groups': [group(BEDROCK_SERVICE, '1.0')]}]}
    )
    aws_util = StubAWSUtil(cost_explorer)

    first = aws_util.cost_explorer_get_tagged_service_spend(
        tag_key=TAG_KEY, tag_value=TAG_VALUE
    )
    second = aws_util.cost_explorer_get_tagged_service_spend(
        tag_key=TAG_KEY, tag_value=TAG_VALUE
    )
    assert first == second
    assert len(cost_explorer.requests) == 1


def test_a_failed_read_is_no_answer_and_is_not_retried_on_every_call():
    cost_explorer = StubCostExplorer(raises=True)
    aws_util = StubAWSUtil(cost_explorer)

    assert (
        aws_util.cost_explorer_get_tagged_service_spend(
            tag_key=TAG_KEY, tag_value=TAG_VALUE
        )
        is None
    )
    assert (
        aws_util.cost_explorer_get_tagged_service_spend(
            tag_key=TAG_KEY, tag_value=TAG_VALUE
        )
        is None
    )
    assert len(cost_explorer.requests) == 1
    cached = aws_util._context.cache().long_term().entries
    assert COST_EXPLORER_UNAVAILABLE in cached.values()
    assert len(aws_util._logger.messages) == 1


def test_nothing_is_asked_of_a_partition_that_has_no_cost_explorer():
    cost_explorer = StubCostExplorer()
    aws_util = StubAWSUtil(cost_explorer, partition='aws-us-gov')
    assert (
        aws_util.cost_explorer_get_tagged_service_spend(
            tag_key=TAG_KEY, tag_value=TAG_VALUE
        )
        is None
    )
    assert cost_explorer.requests == []
