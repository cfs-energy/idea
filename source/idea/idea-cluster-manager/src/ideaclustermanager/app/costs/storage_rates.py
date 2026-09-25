"""Daily file-system estimates from public monthly storage rates."""

import calendar
import json
from typing import Optional

import arrow


def daily_storage_rate(
    context, provider, file_system_id, day, capacity_pool_bytes=0, cache=None
) -> Optional[float]:
    cache = {} if cache is None else cache
    try:
        aws = context.aws()
        if aws.aws_partition() != 'aws' or provider not in ('fsx_netapp_ontap', 'efs'):
            return None
        day = arrow.get(day)
        key = (
            'daily',
            provider,
            file_system_id,
            day.year,
            day.month,
            capacity_pool_bytes,
        )
        if key in cache:
            return cache[key]
        cache[key] = None
        region = aws.aws_region()

        def price(service, **attributes):
            attributes['regionCode'] = region
            price_key = ('price', service, tuple(sorted(attributes.items())))
            if price_key not in cache:
                cache[price_key] = None
                request = dict(
                    ServiceCode=service,
                    Filters=[
                        {'Type': 'TERM_MATCH', 'Field': field, 'Value': value}
                        for field, value in attributes.items()
                    ],
                )
                while True:
                    response = aws.pricing().get_products(**request)
                    for product in response['PriceList']:
                        for term in (
                            json.loads(product)['terms'].get('OnDemand', {}).values()
                        ):
                            for dimension in term['priceDimensions'].values():
                                cache[price_key] = float(
                                    dimension['pricePerUnit']['USD']
                                )
                                return cache[price_key]
                    if not response.get('NextToken'):
                        break
                    request['NextToken'] = response['NextToken']
            if cache[price_key] is None:
                raise ValueError('Storage price unavailable')
            return cache[price_key]

        filesystem_key = ('filesystem', provider, file_system_id)
        if filesystem_key not in cache:
            cache[filesystem_key] = None
            response = (
                aws.fsx().describe_file_systems(FileSystemIds=[file_system_id])
                if provider == 'fsx_netapp_ontap'
                else aws.efs().describe_file_systems(FileSystemId=file_system_id)
            )
            cache[filesystem_key] = response['FileSystems'][0]
        filesystem = cache[filesystem_key]
        if provider == 'fsx_netapp_ontap':
            capacity = filesystem['StorageCapacity']
            ontap = filesystem['OntapConfiguration']
            deployment = {
                'SINGLE_AZ_1': 'Single-AZ_2N',
                'SINGLE_AZ_2': 'Single-AZ_2N-2',
                'MULTI_AZ_1': 'Multi-AZ',
                'MULTI_AZ_2': 'Multi-AZ-2',
            }[ontap['DeploymentType']]
            attributes = dict(fileSystemType='ONTAP', deploymentOption=deployment)
            throughput = ontap.get('ThroughputCapacity')
            if throughput is None:
                throughput = ontap['ThroughputCapacityPerHAPair'] * ontap['HAPairs']
            monthly = capacity * price(
                'AmazonFSx', productFamily='Storage', storageType='SSD', **attributes
            ) + throughput * price(
                'AmazonFSx', productFamily='Provisioned Throughput', **attributes
            )
            iops = ontap.get('DiskIopsConfiguration', {})
            if iops.get('Mode') == 'USER_PROVISIONED':
                extra_iops = max(0, iops['Iops'] - 3 * capacity)
                if extra_iops:
                    monthly += extra_iops * price(
                        'AmazonFSx', productFamily='Provisioned IOPS', **attributes
                    )
            if capacity_pool_bytes:
                monthly += (
                    capacity_pool_bytes
                    / 1e9
                    * price(
                        'AmazonFSx',
                        productFamily='Storage',
                        storageType='Capacity pool - Standard',
                        **attributes,
                    )
                )
        else:
            sizes = filesystem['SizeInBytes']
            monthly = 0
            for field, storage_class in (
                ('ValueInStandard', 'General Purpose'),
                ('ValueInIA', 'Infrequent Access'),
                ('ValueInArchive', 'Archive'),
            ):
                size = sizes.get(field, 0)
                if size:
                    monthly += (
                        size
                        / 2**30
                        * price(
                            'AmazonEFS',
                            productFamily='Storage',
                            storageClass=storage_class,
                        )
                    )
        hours = calendar.monthrange(day.year, day.month)[1] * 24
        cache[key] = monthly * 24 / hours
        return cache[key]
    except Exception:
        if 'warning' not in cache:
            cache['warning'] = True
            context.logger('storage-rates').warning('Storage rate estimate unavailable')
        return None
