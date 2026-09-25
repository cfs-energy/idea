"""Public On-Demand rates in the units stored by job cost estimation."""

import json
import math
import re
from datetime import datetime, timezone

from botocore.loaders import create_loader

from ideadatamodel import exceptions
from ideadatamodel.cluster_settings import FetchPricingRatesResult
from ideasdk.aws.aws_client_provider import (
    AWS_CLIENT_PRICING,
    DEFAULT_PRICING_API_REGION,
)


def fetch_pricing_rates(context, region):
    endpoints = create_loader().load_data('endpoints')
    if (
        not isinstance(region, str)
        or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d+', region)
        or not any(region in p['regions'] for p in endpoints['partitions'])
    ):
        raise exceptions.invalid_params('region must be a valid AWS region')

    products = {
        'ebs_gp3_storage': (
            'AmazonEC2',
            'GB-Mo',
            {
                'productFamily': 'Storage',
                'volumeApiName': 'gp3',
            },
        ),
        'ebs_io1_storage': (
            'AmazonEC2',
            'GB-Mo',
            {
                'productFamily': 'Storage',
                'volumeApiName': 'io1',
            },
        ),
        'provisioned_iops': (
            'AmazonEC2',
            'IOPS-Mo',
            {
                'productFamily': 'System Operation',
                'group': 'EBS IOPS',
                'volumeApiName': 'io1',
            },
        ),
        'fsx_lustre': (
            'AmazonFSx',
            'GB-Mo',
            {
                'productFamily': 'Storage',
                'fileSystemType': 'Lustre',
                'operation': 'CreateFileSystem:Lustre',
                'deploymentOption': 'Single-AZ',
                'storageType': 'SSD',
                'throughputCapacity': 'N/A',
            },
        ),
    }
    result = FetchPricingRatesResult(
        region=region,
        as_of=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        assumptions=[
            'Public OnDemand USD prices; discounts and taxes are excluded.',
            'FSx for Lustre assumes SCRATCH_2/SSD. Monthly GB-Mo is divided by 730 '
            'to store USD/GB-hour as an estimation convention.',
        ],
    )
    aws = context.aws()
    if aws.aws_partition() != 'aws':
        result.unavailable = dict.fromkeys(
            products, 'Pricing lookup is unavailable in this AWS partition.'
        )
        return result
    try:
        # The reporting client has bounded retries and connection/read timeouts.
        client = aws.get_client(
            service_name=AWS_CLIENT_PRICING, region_name=DEFAULT_PRICING_API_REGION
        )
    except Exception:
        result.unavailable = dict.fromkeys(products, 'Pricing service unavailable.')
        return result

    for key, (service, unit, attributes) in products.items():
        attributes = dict(attributes, regionCode=region, locationType='AWS Region')
        request = dict(
            ServiceCode=service,
            FormatVersion='aws_v1',
            MaxResults=100,
            Filters=[
                {'Type': 'TERM_MATCH', 'Field': field, 'Value': value}
                for field, value in attributes.items()
            ],
        )
        try:
            matches = []
            tokens = set()
            for _ in range(10):
                response = client.get_products(**request)
                for raw in response['PriceList']:
                    offer = json.loads(raw)
                    product = offer['product']
                    returned = dict(
                        product['attributes'], productFamily=product['productFamily']
                    )
                    if offer['serviceCode'] != service or any(
                        returned.get(field) != value
                        for field, value in attributes.items()
                    ):
                        raise ValueError('Unexpected product')
                    if key == 'provisioned_iops' and not re.fullmatch(
                        r'(?:[A-Z0-9]+-)?EBS:VolumeP-IOPS\.piops',
                        returned.get('usagetype', ''),
                    ):
                        raise ValueError('Unexpected IOPS product')
                    terms = offer['terms']['OnDemand']
                    if len(terms) != 1:
                        raise ValueError('Ambiguous terms')
                    term = next(iter(terms.values()))
                    dimensions = term['priceDimensions']
                    if len(dimensions) != 1:
                        raise ValueError('Ambiguous dimensions')
                    dimension = next(iter(dimensions.values()))
                    if (
                        dimension['unit'] != unit
                        or dimension['beginRange'] != '0'
                        or dimension['endRange'] != 'Inf'
                        or dimension.get('appliesTo', [])
                        or set(dimension['pricePerUnit']) != {'USD'}
                        or isinstance(dimension['pricePerUnit'].get('USD'), bool)
                    ):
                        raise ValueError('Unexpected unit, currency or tier')
                    rate = float(dimension['pricePerUnit']['USD'])
                    if not math.isfinite(rate) or rate < 0:
                        raise ValueError('Invalid rate')
                    matches.append((rate, product['sku'], dimension['rateCode']))
                token = response.get('NextToken')
                if not token:
                    break
                if token in tokens:
                    raise ValueError('Repeated page')
                tokens.add(token)
                request['NextToken'] = token
            else:
                raise ValueError('Incomplete product list')
            if len(matches) != 1:
                raise ValueError('Missing or ambiguous product')
            rate, sku, rate_code = matches[0]
            result.rates[key] = rate / 730 if key == 'fsx_lustre' else rate
            result.assumptions.append(
                f'{key}: {service}, SKU {sku}, rate {rate_code}; '
                f'OnDemand USD/{unit}, untiered 0..Inf; source rate {rate}.'
            )
        except Exception:
            result.unavailable[key] = (
                'No unambiguous untiered USD rate available for the requested product.'
            )
    return result
