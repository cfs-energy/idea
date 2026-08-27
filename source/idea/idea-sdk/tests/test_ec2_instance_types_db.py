"""
Test Cases for EC2InstanceTypesDB
"""

from ideasdk.aws.ec2_instance_types_db import (
    EC2InstanceTypesDB,
    INSTANCE_TYPES_CACHE_SIZE,
)
from ideasdk.utils import Utils
from ideadatamodel import exceptions, errorcodes

import botocore.exceptions
import logging
import pytest
import threading
import time
from typing import Any, Dict, List, Optional


def instance_type_data(instance_type: str) -> Dict[str, Any]:
    return {
        'InstanceType': instance_type,
        'CurrentGeneration': True,
        'VCpuInfo': {'DefaultVCpus': 2},
        'MemoryInfo': {'SizeInMiB': 4096},
    }


def describe_pages(instance_types: List[str], page_size: int = 100) -> List[Dict]:
    return [
        {
            'InstanceTypes': [
                instance_type_data(instance_type)
                for instance_type in instance_types[offset : offset + page_size]
            ]
        }
        for offset in range(0, len(instance_types), page_size)
    ]


def client_error(error_code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {'Error': {'Code': error_code, 'Message': error_code}},
        'DescribeInstanceTypes',
    )


class MockEc2Client:
    def __init__(
        self,
        pages: Optional[List] = None,
        describe_result: Optional[Dict] = None,
        describe_error: Optional[Exception] = None,
        page_delay_secs: float = 0,
    ):
        self.pages = pages if pages is not None else []
        self.describe_result = describe_result
        self.describe_error = describe_error
        self.page_delay_secs = page_delay_secs
        self.paginate_invocations = 0
        self.describe_invocations = []

    def get_paginator(self, operation_name: str):
        assert operation_name == 'describe_instance_types'
        return self

    def paginate(self, **_):
        self.paginate_invocations += 1
        for page in self.pages:
            if isinstance(page, Exception):
                raise page
            if self.page_delay_secs:
                time.sleep(self.page_delay_secs)
            yield page

    def describe_instance_types(self, **kwargs):
        self.describe_invocations.append(kwargs)
        if self.describe_error is not None:
            raise self.describe_error
        return self.describe_result


class MockAwsClientProvider:
    def __init__(self, ec2_client: MockEc2Client):
        self._ec2_client = ec2_client

    def ec2(self) -> MockEc2Client:
        return self._ec2_client


class MockClusterConfig:
    def get_int(self, key: str, default: int = None) -> int:
        return default


class MockContext:
    def __init__(self, ec2_client: MockEc2Client):
        self._aws = MockAwsClientProvider(ec2_client=ec2_client)
        self._config = MockClusterConfig()
        self._logger = logging.getLogger('ec2-instance-types-db-tests')

    def logger(self, *_, **__) -> logging.Logger:
        return self._logger

    def config(self) -> MockClusterConfig:
        return self._config

    def aws(self) -> MockAwsClientProvider:
        return self._aws


def build_db(ec2_client: MockEc2Client) -> EC2InstanceTypesDB:
    return EC2InstanceTypesDB(context=MockContext(ec2_client=ec2_client))


def expire_cache(db: EC2InstanceTypesDB):
    db._cache_last_refresh = 0


def test_ec2_instance_types_db_populates_cache_from_paginated_describe():
    """
    all instance types returned by the paginator must be queryable without any
    additional api call
    """
    instance_types = ['t3.micro', 'c5.large', 'hpc7a.96xlarge']
    ec2_client = MockEc2Client(pages=describe_pages(instance_types))

    db = build_db(ec2_client)

    assert db.all_instance_type_names() == set(instance_types)
    assert db.get('hpc7a.96xlarge').instance_type == 'hpc7a.96xlarge'
    assert len(ec2_client.describe_invocations) == 0


def test_ec2_instance_types_db_does_not_evict_types_during_population():
    """
    regression: a bounded cache smaller than the region inventory used to evict
    instance types collected from earlier pages
    """
    instance_types = [
        f'mock{index}.large' for index in range(INSTANCE_TYPES_CACHE_SIZE + 250)
    ]
    ec2_client = MockEc2Client(pages=describe_pages(instance_types))

    db = build_db(ec2_client)

    assert db.all_instance_type_names() == set(instance_types)
    assert db.get(instance_types[0]).instance_type == instance_types[0]
    assert len(ec2_client.describe_invocations) == 0


def test_ec2_instance_types_db_get_falls_through_to_describe_on_cache_miss():
    """
    a cache miss must be resolved against the ec2 api instead of failing the caller
    """
    ec2_client = MockEc2Client(
        pages=describe_pages(['t3.micro']),
        describe_result={'InstanceTypes': [instance_type_data('hpc7a.96xlarge')]},
    )

    db = build_db(ec2_client)
    assert 'hpc7a.96xlarge' not in db.all_instance_type_names()

    ec2_instance_type = db.get('hpc7a.96xlarge')

    assert ec2_instance_type.instance_type == 'hpc7a.96xlarge'
    assert ec2_client.describe_invocations == [{'InstanceTypes': ['hpc7a.96xlarge']}]
    assert 'hpc7a.96xlarge' in db.all_instance_type_names()

    # subsequent lookups are served from the cache
    assert db.get('hpc7a.96xlarge').instance_type == 'hpc7a.96xlarge'
    assert len(ec2_client.describe_invocations) == 1


def test_ec2_instance_types_db_get_rejects_invalid_instance_type():
    ec2_client = MockEc2Client(
        pages=describe_pages(['t3.micro']),
        describe_error=client_error('InvalidInstanceType'),
    )

    db = build_db(ec2_client)

    with pytest.raises(exceptions.SocaException) as exc_info:
        db.get('not-an-instance-type')

    assert exc_info.value.error_code == errorcodes.INVALID_EC2_INSTANCE_TYPE
    assert len(ec2_client.describe_invocations) == 1


def test_ec2_instance_types_db_get_rejects_instance_type_missing_from_response():
    ec2_client = MockEc2Client(
        pages=describe_pages(['t3.micro']),
        describe_result={'InstanceTypes': []},
    )

    db = build_db(ec2_client)

    with pytest.raises(exceptions.SocaException) as exc_info:
        db.get('c5.large')

    assert exc_info.value.error_code == errorcodes.INVALID_EC2_INSTANCE_TYPE


def test_ec2_instance_types_db_transient_error_is_not_reported_as_invalid_type():
    """
    a throttled or denied lookup must not tell the user their instance type is invalid
    """
    ec2_client = MockEc2Client(
        pages=describe_pages(['t3.micro']),
        describe_error=client_error('RequestLimitExceeded'),
    )

    db = build_db(ec2_client)

    with pytest.raises(exceptions.SocaException) as exc_info:
        db.get('c5.large')

    assert exc_info.value.error_code != errorcodes.INVALID_EC2_INSTANCE_TYPE
    assert exc_info.value.error_code == errorcodes.GENERAL_ERROR


def test_ec2_instance_types_db_refreshes_cache_after_interval():
    ec2_client = MockEc2Client(pages=describe_pages(['t3.micro']))
    db = build_db(ec2_client)
    assert ec2_client.paginate_invocations == 1

    ec2_client.pages = describe_pages(['t3.micro', 'hpc7a.96xlarge'])
    expire_cache(db)

    assert db.get('hpc7a.96xlarge').instance_type == 'hpc7a.96xlarge'
    assert ec2_client.paginate_invocations == 2
    assert len(ec2_client.describe_invocations) == 0

    # cache is fresh again
    assert db.get('t3.micro').instance_type == 't3.micro'
    assert ec2_client.paginate_invocations == 2


def test_ec2_instance_types_db_failed_refresh_retains_previous_cache():
    """
    a refresh that dies part way through must not leave the scheduler with a
    truncated view of the region for the remainder of the refresh interval
    """
    instance_types = ['t3.micro', 'c5.large', 'hpc7a.96xlarge']
    ec2_client = MockEc2Client(pages=describe_pages(instance_types))
    db = build_db(ec2_client)

    ec2_client.pages = [
        {'InstanceTypes': [instance_type_data('t3.micro')]},
        client_error('RequestLimitExceeded'),
    ]
    expire_cache(db)

    assert db.get('hpc7a.96xlarge').instance_type == 'hpc7a.96xlarge'
    assert db.all_instance_type_names() == set(instance_types)
    assert len(ec2_client.describe_invocations) == 0


def test_ec2_instance_types_db_initial_population_failure_is_raised():
    ec2_client = MockEc2Client(pages=[client_error('UnauthorizedOperation')])

    with pytest.raises(botocore.exceptions.ClientError):
        build_db(ec2_client)


def test_ec2_instance_types_db_concurrent_refresh_runs_once():
    ec2_client = MockEc2Client(
        pages=describe_pages(['t3.micro', 'c5.large']), page_delay_secs=0.05
    )
    db = build_db(ec2_client)
    expire_cache(db)

    results = []

    def lookup():
        results.append(db.get('c5.large').instance_type)

    threads = [threading.Thread(target=lookup) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ['c5.large'] * 8
    assert ec2_client.paginate_invocations == 2
    assert len(ec2_client.describe_invocations) == 0


def test_ec2_instance_types_db_cache_last_refresh_is_set_on_success():
    ec2_client = MockEc2Client(pages=describe_pages(['t3.micro']))

    db = build_db(ec2_client)

    assert db._cache_last_refresh > 0
    assert Utils.current_time() - db._cache_last_refresh < 60
