"""
Test Cases for the EC2 instance type price read

This is reached from a page any user can open, once per distinct instance type, so
what matters is that it never calls where there is no endpoint, never waits long, and
never asks twice after a failure. No AWS calls are made.
"""

from ideadatamodel import EC2InstanceUnitPrice
from ideasdk.aws import aws_util as aws_util_module
from ideasdk.aws.aws_util import AWSUtil, PRICING_CACHE_TTL_SECS, PRICING_UNAVAILABLE
from ideasdk.aws.aws_client_provider import REPORTING_CLIENT_CONFIG

import pytest

INSTANCE_TYPE = 'm5.large'


class StubPriceList:
    """the published price list, already loaded or with nothing for this region"""

    def __init__(self, prices=None):
        self.prices = prices if prices is not None else {}
        self.lookups = []

    def get(self, instance_type):
        self.lookups.append(instance_type)
        return self.prices.get(instance_type)


@pytest.fixture(autouse=True)
def price_list(monkeypatch):
    """
    no test here may reach the price list host. the fallback is exercised through this
    stub; every other test gets one that knows nothing.
    """
    stub = StubPriceList()
    monkeypatch.setattr(
        aws_util_module, 'get_ec2_price_list', lambda region, logger: stub
    )
    return stub


class StubPricing:
    def __init__(self, raises=False):
        self.raises = raises
        self.requests = []

    def get_products(self, **kwargs):
        self.requests.append(kwargs)
        if self.raises:
            raise RuntimeError('AccessDeniedException')
        return {'PriceList': []}


class StubAwsClientProvider:
    def __init__(self, pricing, partition='aws', region='us-east-2'):
        self._pricing = pricing
        self._partition = partition
        self._region = region

    def aws_partition(self):
        return self._partition

    def aws_region(self):
        return self._region

    def pricing(self):
        return self._pricing


class StubCache:
    def __init__(self):
        self.entries = {}
        self.ttls = {}

    def get(self, key, default=None):
        return self.entries.get(key, default)

    def set(self, key, value, ttl=None):
        self.entries[key] = value
        self.ttls[key] = ttl


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
    """AWSUtil with only what the pricing read touches"""

    def __init__(self, pricing, partition='aws'):
        self._context = StubContext()
        self._logger = StubLogger()
        self._aws = StubAwsClientProvider(pricing, partition=partition)


def test_no_call_is_made_outside_the_commercial_partition():
    pricing = StubPricing()
    aws_util = StubAWSUtil(pricing, partition='aws-us-gov')

    assert aws_util.get_ec2_instance_type_unit_price(INSTANCE_TYPE) is None
    # the pricing api has no govcloud endpoint: calling it there hangs until it times
    # out, once per instance type, on a page a user is waiting on
    assert pricing.requests == []


def test_the_price_list_answers_outside_the_commercial_partition(price_list):
    price_list.prices[INSTANCE_TYPE] = EC2InstanceUnitPrice(
        ondemand=0.121, reserved=0.08
    )
    pricing = StubPricing()
    aws_util = StubAWSUtil(pricing, partition='aws-us-gov')

    unit_price = aws_util.get_ec2_instance_type_unit_price(INSTANCE_TYPE)

    assert unit_price.ondemand == 0.121
    assert unit_price.reserved == 0.08
    # still no api call: the rates came from the published offer file
    assert pricing.requests == []
    assert price_list.lookups == [INSTANCE_TYPE]


def test_a_failed_api_call_falls_back_to_the_price_list(price_list):
    price_list.prices[INSTANCE_TYPE] = EC2InstanceUnitPrice(
        ondemand=0.096, reserved=0.06
    )
    pricing = StubPricing(raises=True)
    aws_util = StubAWSUtil(pricing)

    assert aws_util.get_ec2_instance_type_unit_price(INSTANCE_TYPE).ondemand == 0.096

    # the failure is still remembered, so the second read pays for no timeout and is
    # answered from the offer file again
    assert aws_util.get_ec2_instance_type_unit_price(INSTANCE_TYPE).ondemand == 0.096
    assert len(pricing.requests) == 1
    assert price_list.lookups == [INSTANCE_TYPE, INSTANCE_TYPE]


def test_a_failure_is_remembered_as_unavailable_and_not_asked_again():
    pricing = StubPricing(raises=True)
    aws_util = StubAWSUtil(pricing)

    assert aws_util.get_ec2_instance_type_unit_price(INSTANCE_TYPE) is None
    assert len(pricing.requests) == 1

    # second read on the same page load must not pay for the timeout again
    assert aws_util.get_ec2_instance_type_unit_price(INSTANCE_TYPE) is None
    assert len(pricing.requests) == 1

    cache = aws_util._context.cache().long_term()
    key = f'aws.pricing.instance-type.{INSTANCE_TYPE}'
    # remembered as unavailable rather than as a real price of zero, and it expires
    assert cache.entries[key] == PRICING_UNAVAILABLE
    assert cache.ttls[key] == PRICING_CACHE_TTL_SECS


def test_a_failure_is_never_cached_as_a_free_instance():
    pricing = StubPricing(raises=True)
    aws_util = StubAWSUtil(pricing)
    aws_util.get_ec2_instance_type_unit_price(INSTANCE_TYPE)

    cached = (
        aws_util._context.cache()
        .long_term()
        .entries[f'aws.pricing.instance-type.{INSTANCE_TYPE}']
    )
    assert getattr(cached, 'ondemand', None) != 0.0


def test_the_pricing_client_fails_fast():
    # reached over the internet from one commercial endpoint, and never on a critical
    # path: it must not inherit the default 60s connect and read timeouts
    assert REPORTING_CLIENT_CONFIG.connect_timeout == 5
    assert REPORTING_CLIENT_CONFIG.read_timeout == 10
    assert REPORTING_CLIENT_CONFIG.retries['max_attempts'] == 2


def test_the_pricing_client_is_built_with_that_config():
    import inspect
    from ideasdk.aws import aws_client_provider

    source = inspect.getsource(aws_client_provider.AwsClientProvider)
    # the branch that assigns the fail-fast config must cover pricing, not just cost
    # explorer
    assert 'AWS_CLIENT_COST_EXPLORER, AWS_CLIENT_PRICING' in source
    assert 'config = REPORTING_CLIENT_CONFIG' in source
