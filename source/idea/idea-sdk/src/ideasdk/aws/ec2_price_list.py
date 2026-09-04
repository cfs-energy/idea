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
EC2 hourly rates read from the public AWS price list.

The Pricing API is a single commercial endpoint, so a cluster outside that partition
cannot price an instance hour through boto. AWS publishes the same rates as unauthenticated
per region offer files, which is what this reads over plain outbound HTTPS: no credentials
are sent. A file is around 200 MB, so it is streamed a row at a time and the map it
produces is rebuilt daily on a background thread; a caller that asks before the first load
finishes is told the price is unavailable rather than made to wait.
"""

__all__ = (
    'PRICE_LIST_BASE_URL',
    'OFFER_FILE_METADATA_LINES',
    'PRICE_MAP_TTL_SECS',
    'PRICE_MAP_FAILURE_TTL_SECS',
    'EC2PriceList',
    'parse_ec2_offer_csv',
    'get_ec2_price_list',
    'reset_ec2_price_lists',
)

from ideadatamodel import EC2InstanceUnitPrice

import csv
import requests
import threading
import time
from typing import Dict, Iterable, Optional

# the published offer files. one directory per service, then per region.
PRICE_LIST_BASE_URL = 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws'

# the offer csv opens with format version, disclaimer, publication date, version and
# offer code, one per line, before the header row the columns are named on.
OFFER_FILE_METADATA_LINES = 5

# public list prices change rarely, and a rebuild reads a few hundred megabytes.
PRICE_MAP_TTL_SECS = 24 * 60 * 60

# mirrors the pricing api failure cache in aws_util: a fetch that failed is not tried
# again on the next page load.
PRICE_MAP_FAILURE_TTL_SECS = 6 * 60 * 60

CONNECT_TIMEOUT_SECS = 10
READ_TIMEOUT_SECS = 60


def _as_price(value: Optional[str]) -> Optional[float]:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _is_standard_1yr_no_upfront(row: Dict[str, str]) -> bool:
    """the reserved term the commercial pricing api path reports, so the two agree."""
    return (
        row.get('LeaseContractLength') == '1yr'
        and row.get('PurchaseOption') == 'No Upfront'
        and row.get('OfferingClass') == 'standard'
    )


def parse_ec2_offer_csv(
    lines: Iterable[str], region: str
) -> Dict[str, EC2InstanceUnitPrice]:
    """
    the on-demand and reserved hourly rates per instance type, from the lines of an EC2
    offer file including its metadata preamble. the file carries every operating system,
    tenancy, license and capacity combination, so the rows are narrowed to what IDEA
    launches: shared tenancy Linux, no bundled software, charged for a running instance.
    """
    rows = iter(lines)
    for _ in range(OFFER_FILE_METADATA_LINES):
        next(rows, None)

    ondemand: Dict[str, float] = {}
    reserved: Dict[str, float] = {}

    for row in csv.DictReader(rows):
        instance_type = row.get('Instance Type')
        if not instance_type:
            continue
        if (
            row.get('Region Code') != region
            or row.get('Operating System') != 'Linux'
            or row.get('Tenancy') != 'Shared'
            or row.get('Pre Installed S/W') != 'NA'
            or row.get('Unit') != 'Hrs'
        ):
            continue
        price = _as_price(row.get('PricePerUnit'))
        if price is None:
            continue

        term_type = row.get('TermType')
        if term_type == 'OnDemand':
            # the same instance type also has an unused capacity reservation rate,
            # which is what an idle reservation costs rather than what a job costs.
            if row.get('CapacityStatus') == 'Used':
                ondemand[instance_type] = price
        elif term_type == 'Reserved' and _is_standard_1yr_no_upfront(row):
            reserved[instance_type] = price

    return {
        instance_type: EC2InstanceUnitPrice(
            ondemand=price,
            # no reserved rate published for this type: report no saving rather than a
            # saving of the entire hourly rate, which is what a zero would compute to.
            reserved=reserved.get(instance_type, price),
        )
        for instance_type, price in ondemand.items()
    }


class EC2PriceList:
    """
    one region's instance hour rates, refreshed daily in the background. every read is
    served from the map already in hand, so the first read of the process reports no price
    and a read during a refresh gets the previous map rather than waiting.
    """

    def __init__(self, region: str, logger):
        self._region = region
        self._logger = logger
        self._lock = threading.Lock()
        self._prices: Optional[Dict[str, EC2InstanceUnitPrice]] = None
        self._loaded_at = 0.0
        self._failed_at = 0.0
        self._refreshing = False

    def offer_url(self) -> str:
        return f'{PRICE_LIST_BASE_URL}/AmazonEC2/current/{self._region}/index.csv'

    def get(self, instance_type: str) -> Optional[EC2InstanceUnitPrice]:
        self._start_refresh_if_due()
        with self._lock:
            prices = self._prices
        if prices is None:
            return None
        return prices.get(instance_type)

    def _start_refresh_if_due(self):
        with self._lock:
            if self._refreshing:
                return
            now = time.time()
            if self._prices is not None and now - self._loaded_at < PRICE_MAP_TTL_SECS:
                return
            if now - self._failed_at < PRICE_MAP_FAILURE_TTL_SECS:
                return
            self._refreshing = True
        threading.Thread(
            target=self._refresh, name=f'ec2-price-list-{self._region}', daemon=True
        ).start()

    def _refresh(self):
        started = time.time()
        try:
            prices = self._fetch()
        except Exception as e:
            with self._lock:
                self._failed_at = time.time()
                self._refreshing = False
            # the url is public and carries no credentials, so it is safe to log.
            self._logger.warning(
                f'failed to read the ec2 price list for {self._region} from '
                f'{self.offer_url()}: {type(e).__name__}: {e}'
            )
            return

        elapsed = time.time() - started
        with self._lock:
            self._prices = prices
            self._loaded_at = time.time()
            self._failed_at = 0.0
            self._refreshing = False
        self._logger.info(
            f'loaded {len(prices)} ec2 instance type prices for {self._region} from '
            f'the public price list in {elapsed:.1f}s'
        )

    def _fetch(self) -> Dict[str, EC2InstanceUnitPrice]:
        with requests.get(
            self.offer_url(),
            stream=True,
            timeout=(CONNECT_TIMEOUT_SECS, READ_TIMEOUT_SECS),
        ) as response:
            response.raise_for_status()
            # iter_lines only decodes when the response declares a charset and this one
            # does not, so the rows arrive as bytes.
            lines = (line.decode('utf-8') for line in response.iter_lines())
            return parse_ec2_offer_csv(lines, self._region)


# one map per region per process, so two AWSUtil instances do not each download the file.
_price_lists: Dict[str, EC2PriceList] = {}
_price_lists_lock = threading.Lock()


def get_ec2_price_list(region: str, logger) -> EC2PriceList:
    with _price_lists_lock:
        price_list = _price_lists.get(region)
        if price_list is None:
            price_list = EC2PriceList(region=region, logger=logger)
            _price_lists[region] = price_list
        return price_list


def reset_ec2_price_lists():
    """drop the per region maps; used by tests."""
    with _price_lists_lock:
        _price_lists.clear()
