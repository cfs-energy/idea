"""Storage estimates use provisioned capacity and public monthly rates."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideaclustermanager.app.costs.storage_rates import daily_storage_rate


def offer(service, attributes, usage, unit, rate, sku='sku'):
    return json.dumps(
        {
            'serviceCode': service,
            'product': {
                'sku': sku,
                'productFamily': attributes['productFamily'],
                'attributes': dict(attributes, usagetype=usage),
            },
            'terms': {
                'OnDemand': {
                    'term': {
                        'priceDimensions': {
                            'rate': {
                                'rateCode': sku + '.rate',
                                'unit': unit,
                                'beginRange': '0',
                                'endRange': 'Inf',
                                'pricePerUnit': {'USD': str(rate)},
                            }
                        }
                    }
                }
            },
        }
    )


@pytest.fixture
def context():
    filesystem = {
        'StorageCapacity': 2048,
        'OntapConfiguration': {
            'DeploymentType': 'SINGLE_AZ_1',
            'ThroughputCapacity': 512,
            'DiskIopsConfiguration': {'Mode': 'AUTOMATIC', 'Iops': 6144},
        },
    }

    def products(**request):
        attributes = {item['Field']: item['Value'] for item in request['Filters']}
        assert all(item['Type'] == 'TERM_MATCH' for item in request['Filters'])
        assert attributes['regionCode'] == 'us-east-2'
        family = attributes['productFamily']
        if request['ServiceCode'] == 'AmazonFSx':
            assert attributes['fileSystemType'] == 'ONTAP'
            deployment = attributes['deploymentOption']
            storage_type = attributes.get('storageType')
            if storage_type == 'Capacity pool - Standard' and deployment.endswith('-2'):
                return {'PriceList': []}
            index = ['Single-AZ_2N', 'Single-AZ_2N-2', 'Multi-AZ', 'Multi-AZ-2'].index(
                deployment
            )
            rate = {
                'Provisioned Throughput': [0.72, 1.6, 1.2, 2.5],
                'Provisioned IOPS': [0.017, 0.017, 0.034, 0.034],
                'SSD': [0.125, 0.125, 0.25, 0.25],
                'Capacity pool - Standard': [0.0219, 0.0219, 0.0438, 0.0438],
            }[storage_type or family][index]
            unit = {
                'Provisioned Throughput': 'MiBps-Mo',
                'Provisioned IOPS': 'IOPS-Mo',
            }.get(family, 'GB-Mo')
            offers = [('Storage', unit, rate)]
        else:
            assert request['ServiceCode'] == 'AmazonEFS'
            assert family == 'Storage'
            storage_class = attributes['storageClass']
            rate = {
                'General Purpose': 0.3,
                'Infrequent Access': 0.025,
                'Archive': 0.008,
                'One Zone-General Purpose': 0.16,
                'One Zone-Infrequent Access': 0.0133,
            }[storage_class]
            offers = [('TimedStorage', 'GB-Mo', rate)]
            if storage_class in ('Infrequent Access', 'Archive'):
                prefix = 'IA' if storage_class == 'Infrequent Access' else 'Archive'
                access_rate = 0.01 if prefix == 'IA' else 0.03
                offers = [(prefix + 'DataAccess', 'GB', access_rate)] * 2 + [
                    (prefix + 'TimedStorage', 'GB-Mo', rate)
                ]
        return {
            'PriceList': [
                offer(request['ServiceCode'], attributes, usage, unit, rate, str(index))
                for index, (usage, unit, rate) in enumerate(offers)
            ]
        }

    pricing = Mock()
    pricing.get_products.side_effect = products
    fsx, efs = Mock(), Mock()
    fsx.describe_file_systems.return_value = {'FileSystems': [filesystem]}
    efs.describe_file_systems.return_value = {
        'FileSystems': [
            {
                'SizeInBytes': {
                    'ValueInStandard': 100 * 2**30,
                    'ValueInIA': 200 * 2**30,
                    'ValueInArchive': 300 * 2**30,
                }
            }
        ]
    }
    aws = SimpleNamespace(
        aws_partition=lambda: 'aws',
        aws_region=lambda: 'us-east-2',
        fsx=lambda: fsx,
        efs=lambda: efs,
        pricing=lambda: pricing,
    )
    return SimpleNamespace(aws=lambda: aws, logger=Mock(return_value=Mock()))


def test_ontap_includes_provisioned_ssd_throughput_and_capacity_pool(context):
    cache = {}
    rate = daily_storage_rate(
        context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01', 9000 * 2**30, cache
    )
    assert rate == pytest.approx(821.74 / 30, abs=0.01)
    assert (
        daily_storage_rate(
            context, 'fsx_netapp_ontap', 'fs-test', '2026-09-02', 9000 * 2**30, cache
        )
        == rate
    )
    assert daily_storage_rate(
        context, 'fsx_netapp_ontap', 'fs-test', '2026-10-01', 9000 * 2**30, cache
    ) == pytest.approx(821.74 / 31)
    context.aws().fsx().describe_file_systems.assert_called_once_with(
        FileSystemIds=['fs-test']
    )
    assert context.aws().pricing().get_products.call_count == 3


@pytest.mark.parametrize('iops,extra', [(7144, 17), (6000, 0)])
def test_ontap_only_charges_iops_above_included_allowance(context, iops, extra):
    ontap = (
        context.aws()
        .fsx()
        .describe_file_systems.return_value['FileSystems'][0]['OntapConfiguration']
    )
    ontap['DiskIopsConfiguration'] = {'Mode': 'USER_PROVISIONED', 'Iops': iops}
    assert daily_storage_rate(
        context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01'
    ) == pytest.approx((256 + 368.64 + extra) / 30)


@pytest.mark.parametrize(
    'deployment,ssd,throughput,pool,iops',
    [
        ('SINGLE_AZ_2', 0.125, 1.6, 0.0219, 0.017),
        ('MULTI_AZ_1', 0.25, 1.2, 0.0438, 0.034),
        ('MULTI_AZ_2', 0.25, 2.5, 0.0438, 0.034),
    ],
)
def test_ontap_deployment_rates_and_throughput_per_pair(
    context, deployment, ssd, throughput, pool, iops
):
    ontap = (
        context.aws()
        .fsx()
        .describe_file_systems.return_value['FileSystems'][0]['OntapConfiguration']
    )
    ontap.update(DeploymentType=deployment, ThroughputCapacityPerHAPair=256, HAPairs=2)
    ontap['DiskIopsConfiguration'] = {'Mode': 'USER_PROVISIONED', 'Iops': 7144}
    del ontap['ThroughputCapacity']
    assert daily_storage_rate(
        context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01', 9000 * 2**30
    ) == pytest.approx((2048 * ssd + 512 * throughput + 9000 * pool + 1000 * iops) / 30)


def test_efs_prices_each_storage_class(context):
    assert daily_storage_rate(context, 'efs', 'fs-test', '2026-09-01') == pytest.approx(
        (30 + 5 + 2.4) / 30
    )
    context.aws().efs().describe_file_systems.assert_called_once_with(
        FileSystemId='fs-test'
    )
    assert context.aws().pricing().get_products.call_count == 3


@pytest.mark.parametrize('provider', ['fsx_lustre', 'fsx_windows_file_server'])
def test_unsupported_provider_has_no_rate(context, provider):
    assert daily_storage_rate(context, provider, 'fs-test', '2026-09-01') is None
    context.aws().pricing().get_products.assert_not_called()


def test_non_commercial_partition_has_no_rate(context):
    context.aws().aws_partition = lambda: 'aws-us-gov'
    assert daily_storage_rate(context, 'efs', 'fs-test', '2026-09-01') is None
    context.aws().efs().describe_file_systems.assert_not_called()
    context.aws().pricing().get_products.assert_not_called()


@pytest.mark.parametrize('response', [RuntimeError('unavailable'), {'PriceList': []}])
def test_pricing_failure_is_cached_and_logged_once(context, response):
    context.aws().pricing().get_products.side_effect = [response]
    cache = {}
    for day in ('2026-09-01', '2026-09-02', '2026-10-01'):
        assert daily_storage_rate(context, 'efs', 'fs-test', day, cache=cache) is None
    context.logger().warning.assert_called_once()
    assert context.aws().pricing().get_products.call_count == 1


def test_description_failure_is_unavailable(context):
    context.aws().fsx().describe_file_systems.side_effect = RuntimeError('unavailable')
    assert (
        daily_storage_rate(context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01') is None
    )
    context.logger().warning.assert_called_once()


@pytest.mark.parametrize('provider', ['efs', 'fsx_netapp_ontap'])
def test_daily_storage_keeps_calendar_month_units(context, provider):
    february = daily_storage_rate(context, provider, 'fs-test', '2026-02-01')
    march = daily_storage_rate(context, provider, 'fs-test', '2026-03-01')
    assert february * 28 == pytest.approx(march * 31)
    assert february > march > 0
    assert all(
        item['Value'] != 'Lustre'
        for call in context.aws().pricing().get_products.call_args_list
        for item in call.kwargs['Filters']
    )


def test_efs_one_zone_uses_one_zone_storage_classes(context):
    filesystem = (
        context.aws().efs().describe_file_systems.return_value['FileSystems'][0]
    )
    filesystem['AvailabilityZoneName'] = 'us-east-2a'
    filesystem['SizeInBytes']['ValueInArchive'] = 0
    assert daily_storage_rate(context, 'efs', 'fs-test', '2026-09-01') == pytest.approx(
        (100 * 0.16 + 200 * 0.0133) / 30
    )


@pytest.mark.parametrize('deployment', ['SINGLE_AZ_2', 'MULTI_AZ_2'])
def test_gen2_missing_capacity_pool_price_is_unavailable(context, deployment):
    context.aws().fsx().describe_file_systems.return_value['FileSystems'][0][
        'OntapConfiguration'
    ]['DeploymentType'] = deployment
    pricing = context.aws().pricing()
    products = pricing.get_products.side_effect

    def without_pool(**request):
        if any(
            item['Value'] == 'Capacity pool - Standard' for item in request['Filters']
        ):
            return {'PriceList': []}
        return products(**request)

    pricing.get_products.side_effect = without_pool
    assert (
        daily_storage_rate(
            context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01', 9000 * 2**30
        )
        is None
    )


@pytest.mark.parametrize(
    'provider,invalid',
    [
        (provider, invalid)
        for provider in ('efs', 'fsx_netapp_ontap')
        for invalid in [
            'unit',
            'tier',
            'currency',
            'terms',
            'dimensions',
            'duplicate_page',
        ]
    ]
    + [('efs', 'missing_timed_storage')],
)
def test_storage_rejects_invalid_or_ambiguous_prices(context, invalid, provider):
    pricing = context.aws().pricing()
    products = pricing.get_products.side_effect

    def invalid_products(**request):
        response = products(**request)
        product = json.loads(response['PriceList'][-1])
        term = product['terms']['OnDemand']['term']
        dimension = term['priceDimensions']['rate']
        if invalid == 'unit':
            dimension['unit'] = 'GB'
        elif invalid == 'tier':
            dimension['endRange'] = '100'
        elif invalid == 'currency':
            dimension['pricePerUnit'] = {'EUR': '0.3'}
        elif invalid == 'terms':
            product['terms']['OnDemand']['other'] = term
        elif invalid == 'dimensions':
            term['priceDimensions']['other'] = dimension
        elif invalid == 'missing_timed_storage':
            product['product']['attributes']['usagetype'] = 'DataAccess'
        response['PriceList'] = [json.dumps(product)]
        if invalid == 'duplicate_page' and 'NextToken' not in request:
            response['NextToken'] = 'next'
        return response

    pricing.get_products.side_effect = invalid_products
    assert daily_storage_rate(context, provider, 'fs-test', '2026-09-01') is None


def test_efs_timed_storage_can_be_on_a_later_page(context):
    pricing = context.aws().pricing()
    products = pricing.get_products.side_effect

    def paginated(**request):
        response = products(**request)
        if len(response['PriceList']) == 3:
            if 'NextToken' in request:
                response['PriceList'] = response['PriceList'][2:]
            else:
                response['PriceList'] = response['PriceList'][:2]
                response['NextToken'] = 'storage'
        return response

    pricing.get_products.side_effect = paginated
    assert daily_storage_rate(context, 'efs', 'fs-test', '2026-09-01') == pytest.approx(
        (30 + 5 + 2.4) / 30
    )
    assert pricing.get_products.call_count == 5


def test_capacity_pool_uses_binary_gigabytes(context):
    base = daily_storage_rate(context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01')
    with_pool = daily_storage_rate(
        context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01', 100 * 2**30
    )
    assert with_pool - base == pytest.approx(100 * 0.0219 / 30)
