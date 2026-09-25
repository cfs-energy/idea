"""Pricing proposals validate products and never modify stored settings."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from ideadatamodel import exceptions
from ideadatamodel.cluster_settings import (
    FetchPricingRatesRequest,
    FetchPricingRatesResult,
    OPEN_API_SPEC_ENTRIES_CLUSTER_SETTINGS,
)
from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI
from ideaclustermanager.app.costs.pricing_rates import fetch_pricing_rates
from ideasdk.aws.aws_client_provider import REPORTING_CLIENT_CONFIG


def offer(volume='gp3', iops=False, lustre=False):
    attributes = {'regionCode': 'us-east-2', 'locationType': 'AWS Region'}
    family, service, unit, rate = 'Storage', 'AmazonEC2', 'GB-Mo', '0.08'
    if lustre:
        service, rate = 'AmazonFSx', '0.14'
        attributes.update(
            fileSystemType='Lustre',
            operation='CreateFileSystem:Lustre',
            deploymentOption='Single-AZ',
            storageType='SSD',
            throughputCapacity='N/A',
        )
    else:
        attributes['volumeApiName'] = volume
        if volume == 'io1':
            rate = '0.125'
        if iops:
            family, unit, rate = 'System Operation', 'IOPS-Mo', '0.065'
            attributes.update(group='EBS IOPS', usagetype='USE2-EBS:VolumeP-IOPS.piops')
    return {
        'serviceCode': service,
        'product': {
            'sku': 'product',
            'productFamily': family,
            'attributes': attributes,
        },
        'terms': {
            'OnDemand': {
                'term': {
                    'priceDimensions': {
                        'rate': {
                            'rateCode': 'product.term.rate',
                            'unit': unit,
                            'beginRange': '0',
                            'endRange': 'Inf',
                            'appliesTo': [],
                            'pricePerUnit': {'USD': rate},
                        }
                    }
                }
            }
        },
    }


def dimension(product):
    return product['terms']['OnDemand']['term']['priceDimensions']['rate']


@pytest.fixture
def context():
    app = Mock()
    app.module_id.return_value = 'cluster-manager'
    app.aws.return_value.aws_partition.return_value = 'aws'

    def products(**request):
        attributes = {f['Field']: f['Value'] for f in request['Filters']}
        assert all(f['Type'] == 'TERM_MATCH' for f in request['Filters'])
        assert attributes['regionCode'] == 'us-east-2'
        assert attributes['locationType'] == 'AWS Region'
        assert request['FormatVersion'] == 'aws_v1'
        assert request['MaxResults'] == 100
        product = offer(
            attributes.get('volumeApiName', 'gp3'),
            iops=attributes['productFamily'] == 'System Operation',
            lustre=request['ServiceCode'] == 'AmazonFSx',
        )
        expected = dict(
            product['product']['attributes'],
            productFamily=product['product']['productFamily'],
        )
        expected.pop('usagetype', None)
        assert attributes == expected
        return {'PriceList': [json.dumps(product)]}

    app.aws.return_value.get_client.return_value.get_products.side_effect = products
    return app


def test_fetch_units_filters_endpoint_and_retrieval_time(context):
    start = datetime.now(timezone.utc)
    result = fetch_pricing_rates(context, 'us-east-2')
    assert result.region == 'us-east-2'
    assert start <= datetime.fromisoformat(result.as_of) <= datetime.now(timezone.utc)
    assert result.rates == {
        'ebs_gp3_storage': 0.08,
        'ebs_io1_storage': 0.125,
        'provisioned_iops': 0.065,
        'fsx_lustre': 0.14 / 730,
    }
    assert result.unavailable == {}
    assert any('SCRATCH_2/SSD' in text and '730' in text for text in result.assumptions)
    for key in result.rates:
        assert any(key in text and '0..Inf' in text for text in result.assumptions)
    context.aws().get_client.assert_called_once_with(
        service_name='pricing', region_name='us-east-1'
    )
    assert REPORTING_CLIENT_CONFIG.connect_timeout == 5
    assert REPORTING_CLIENT_CONFIG.read_timeout == 10
    assert REPORTING_CLIENT_CONFIG.retries == {'max_attempts': 2, 'mode': 'standard'}
    context.config.assert_not_called()


@pytest.mark.parametrize(
    'region',
    ['', 'us-east-2 ', 'https://example.invalid', 'us-fake-1', 'US-EAST-2', None, 1],
)
def test_invalid_regions_fail_before_aws(context, region):
    with pytest.raises(exceptions.SocaException):
        fetch_pricing_rates(context, region)
    context.aws.assert_not_called()


@pytest.mark.parametrize('region', ['us-east-2', 'us-gov-west-1'])
def test_govcloud_caller_never_creates_a_signed_client(context, region):
    context.aws().aws_partition.return_value = 'aws-us-gov'
    result = fetch_pricing_rates(context, region)
    assert not result.rates
    assert len(result.unavailable) == 4
    context.aws().get_client.assert_not_called()
    context.config.assert_not_called()


def test_pagination_is_complete_before_accepting_a_rate(context):
    client = context.aws().get_client.return_value
    original = client.get_products.side_effect

    def pages(**request):
        if not request.get('NextToken'):
            return {'PriceList': [], 'NextToken': 'next'}
        assert request['NextToken'] == 'next'
        return original(**request)

    client.get_products.side_effect = pages
    assert len(fetch_pricing_rates(context, 'us-east-2').rates) == 4
    assert client.get_products.call_count == 8


@pytest.mark.parametrize('repeat', [True, False])
def test_pagination_cannot_loop_or_accept_an_incomplete_lookup(context, repeat):
    client = context.aws().get_client.return_value
    client.get_products.side_effect = lambda **kw: {
        'PriceList': [],
        'NextToken': 'same' if repeat else str(client.get_products.call_count),
    }
    result = fetch_pricing_rates(context, 'us-east-2')
    assert len(result.unavailable) == 4
    assert not result.rates
    assert client.get_products.call_count <= 40


@pytest.mark.parametrize(
    'field,value',
    [
        ('unit', 'GB-Hrs'),
        ('beginRange', '1'),
        ('endRange', '1000'),
        ('pricePerUnit', {'EUR': '0.08'}),
        ('pricePerUnit', {'USD': 'NaN'}),
        ('pricePerUnit', {'USD': 'Infinity'}),
        ('pricePerUnit', {'USD': '-1'}),
        ('pricePerUnit', {'USD': True}),
        ('pricePerUnit', {'USD': '0.08', 'EUR': '0.07'}),
        ('appliesTo', ['other']),
    ],
)
def test_invalid_dimension_leaves_other_rates_available(context, field, value):
    client = context.aws().get_client.return_value
    original = client.get_products.side_effect

    def products(**request):
        response = original(**request)
        product = json.loads(response['PriceList'][0])
        if product['product']['attributes'].get('volumeApiName') == 'gp3':
            dimension(product)[field] = value
        return {'PriceList': [json.dumps(product)]}

    client.get_products.side_effect = products
    result = fetch_pricing_rates(context, 'us-east-2')
    assert set(result.unavailable) == {'ebs_gp3_storage'}
    assert len(result.rates) == 3


@pytest.mark.parametrize(
    'field,value',
    [
        ('regionCode', 'us-west-2'),
        ('locationType', 'AWS Local Zone'),
        ('volumeApiName', 'io2'),
        ('productFamily', 'Compute Instance'),
    ],
)
def test_wrong_products_are_rejected(context, field, value):
    product = offer()
    target = (
        product['product']
        if field == 'productFamily'
        else product['product']['attributes']
    )
    target[field] = value
    context.aws().get_client.return_value.get_products.return_value = {
        'PriceList': [json.dumps(product)]
    }
    context.aws().get_client.return_value.get_products.side_effect = None
    assert not fetch_pricing_rates(context, 'us-east-2').rates


@pytest.mark.parametrize(
    'change',
    ['missing', 'ambiguous', 'terms', 'tiers', 'reserved', 'malformed', 'exception'],
)
def test_bad_or_missing_offers_are_safe(context, change):
    product = offer()
    response = {'PriceList': [json.dumps(product)]}
    if change == 'missing':
        response['PriceList'] = []
    elif change == 'ambiguous':
        other = deepcopy(product)
        other['product']['sku'] = 'other'
        response['PriceList'].append(json.dumps(other))
    elif change == 'terms':
        product['terms']['OnDemand']['other'] = deepcopy(
            product['terms']['OnDemand']['term']
        )
        response['PriceList'] = [json.dumps(product)]
    elif change == 'tiers':
        product['terms']['OnDemand']['term']['priceDimensions']['other'] = deepcopy(
            dimension(product)
        )
        response['PriceList'] = [json.dumps(product)]
    elif change == 'reserved':
        product['terms'] = {'Reserved': product['terms']['OnDemand']}
        response['PriceList'] = [json.dumps(product)]
    elif change == 'malformed':
        response['PriceList'] = ['invalid json']
    client = context.aws().get_client.return_value
    client.get_products.side_effect = (
        RuntimeError('private failure detail') if change == 'exception' else None
    )
    client.get_products.return_value = response
    result = fetch_pricing_rates(context, 'us-east-2')
    assert not result.rates
    assert len(result.unavailable) == 4
    assert 'private failure' not in result.model_dump_json()


@pytest.mark.parametrize(
    'usage',
    [
        'USE2-EBS:VolumeP-IOPS.gp3',
        'USE2-EBS:VolumeP-IOPS.io2',
        'USE2-EBS:VolumeUsage.piops',
        '',
    ],
)
def test_only_io1_iops_usage_is_accepted(context, usage):
    client = context.aws().get_client.return_value
    original = client.get_products.side_effect

    def products(**request):
        response = original(**request)
        product = json.loads(response['PriceList'][0])
        if product['product']['productFamily'] == 'System Operation':
            product['product']['attributes']['usagetype'] = usage
        return {'PriceList': [json.dumps(product)]}

    client.get_products.side_effect = products
    result = fetch_pricing_rates(context, 'us-east-2')
    assert set(result.unavailable) == {'provisioned_iops'}
    assert len(result.rates) == 3


def test_client_failure_is_safe(context):
    context.aws().get_client.side_effect = RuntimeError('private client detail')
    result = fetch_pricing_rates(context, 'us-east-2')
    assert len(result.unavailable) == 4
    assert 'private client' not in result.model_dump_json()


def test_explicit_api_dispatch_and_no_writes(context):
    invocation = Mock(namespace='ClusterSettings.FetchPricingRates')
    invocation.get_request_payload_as.return_value = FetchPricingRatesRequest(
        region='us-east-2'
    )
    ClusterSettingsAPI(context).invoke(invocation)
    invocation.is_authorized.assert_called_once_with(
        elevated_access=True, scopes=['cluster-manager/read']
    )
    invocation.get_request_payload_as.assert_called_once_with(FetchPricingRatesRequest)
    assert len(invocation.success.call_args.args[0].rates) == 4
    context.config.assert_not_called()
    entry = next(
        e
        for e in OPEN_API_SPEC_ENTRIES_CLUSTER_SETTINGS
        if e.namespace == invocation.namespace
    )
    assert entry.request is FetchPricingRatesRequest
    assert entry.result is FetchPricingRatesResult


@pytest.mark.parametrize(
    'rates', [{'unknown': 1}, {'fsx_lustre': -1}, {'provisioned_iops': float('nan')}]
)
def test_result_contract_rejects_unknown_or_invalid_rates(rates):
    with pytest.raises(ValidationError):
        FetchPricingRatesResult(
            region='us-east-2', as_of='2026-09-01T00:00:00Z', rates=rates
        )


@pytest.mark.parametrize(
    'field,value',
    [
        ('fileSystemType', 'ONTAP'),
        ('deploymentOption', 'Multi-AZ'),
        ('storageType', 'HDD'),
        ('throughputCapacity', '200'),
        ('operation', 'CreateFileSystem:Ontap'),
    ],
)
def test_lustre_attributes_must_match_scratch_ssd(context, field, value):
    client = context.aws().get_client.return_value
    original = client.get_products.side_effect

    def products(**request):
        response = original(**request)
        product = json.loads(response['PriceList'][0])
        if request['ServiceCode'] == 'AmazonFSx':
            product['product']['attributes'][field] = value
        return {'PriceList': [json.dumps(product)]}

    client.get_products.side_effect = products
    result = fetch_pricing_rates(context, 'us-east-2')
    assert set(result.unavailable) == {'fsx_lustre'}
    assert len(result.rates) == 3


def test_failure_of_one_lookup_preserves_other_rates(context):
    client = context.aws().get_client.return_value
    original = client.get_products.side_effect

    def products(**request):
        if request['ServiceCode'] == 'AmazonFSx':
            raise RuntimeError('private failure detail')
        return original(**request)

    client.get_products.side_effect = products
    result = fetch_pricing_rates(context, 'us-east-2')
    assert set(result.unavailable) == {'fsx_lustre'}
    assert len(result.rates) == 3
    assert 'private failure detail' not in result.model_dump_json()


def test_later_page_can_make_a_product_ambiguous(context):
    client = context.aws().get_client.return_value
    original = client.get_products.side_effect

    def pages(**request):
        response = original(**request)
        if not request.get('NextToken'):
            response['NextToken'] = 'next'
        return response

    client.get_products.side_effect = pages
    result = fetch_pricing_rates(context, 'us-east-2')
    assert not result.rates
    assert len(result.unavailable) == 4
    assert client.get_products.call_count == 8
