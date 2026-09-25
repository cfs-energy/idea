"""Storage estimates use provisioned capacity and public monthly rates."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideaclustermanager.app.costs.storage_rates import daily_storage_rate


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
        if request['ServiceCode'] == 'AmazonFSx':
            assert attributes['fileSystemType'] == 'ONTAP'
            index = ['Single-AZ_2N', 'Single-AZ_2N-2', 'Multi-AZ', 'Multi-AZ-2'].index(
                attributes['deploymentOption']
            )
            rate = {
                'Provisioned Throughput': [0.72, 1.6, 1.2, 2.5],
                'Provisioned IOPS': [0.017, 0.017, 0.034, 0.034],
                'SSD': [0.125, 0.125, 0.25, 0.25],
                'Capacity pool - Standard': [0.0219, 0.0219, 0.0438, 0.0438],
            }[attributes.get('storageType', attributes['productFamily'])][index]
        else:
            assert request['ServiceCode'] == 'AmazonEFS'
            assert attributes['productFamily'] == 'Storage'
            rate = {
                'General Purpose': 0.3,
                'Infrequent Access': 0.025,
                'Archive': 0.008,
            }[attributes['storageClass']]
        return {
            'PriceList': [
                json.dumps(
                    {
                        'terms': {
                            'OnDemand': {
                                'sku.term': {
                                    'priceDimensions': {
                                        'sku.term.rate': {
                                            'unit': 'GB-Mo',
                                            'pricePerUnit': {'USD': str(rate)},
                                        }
                                    }
                                }
                            }
                        }
                    }
                )
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
        context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01', 9e12, cache
    )
    assert rate == pytest.approx(821.74 / 30, abs=0.01)
    assert (
        daily_storage_rate(
            context, 'fsx_netapp_ontap', 'fs-test', '2026-09-02', 9e12, cache
        )
        == rate
    )
    assert daily_storage_rate(
        context, 'fsx_netapp_ontap', 'fs-test', '2026-10-01', 9e12, cache
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
    'deployment,ssd,throughput,pool',
    [
        ('SINGLE_AZ_2', 0.125, 1.6, 0.0219),
        ('MULTI_AZ_1', 0.25, 1.2, 0.0438),
        ('MULTI_AZ_2', 0.25, 2.5, 0.0438),
    ],
)
def test_ontap_deployment_rates_and_throughput_per_pair(
    context, deployment, ssd, throughput, pool
):
    ontap = (
        context.aws()
        .fsx()
        .describe_file_systems.return_value['FileSystems'][0]['OntapConfiguration']
    )
    ontap.update(DeploymentType=deployment, ThroughputCapacityPerHAPair=256, HAPairs=2)
    del ontap['ThroughputCapacity']
    assert daily_storage_rate(
        context, 'fsx_netapp_ontap', 'fs-test', '2026-09-01', 9e12
    ) == pytest.approx((2048 * ssd + 512 * throughput + 9000 * pool) / 30)


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
