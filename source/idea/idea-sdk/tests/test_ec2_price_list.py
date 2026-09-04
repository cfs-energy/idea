"""
Test Cases for the public price list fallback

Outside the commercial partition this is the only source of an instance hour rate, so
what matters is that it reads the right rows out of the offer file, never blocks the
caller on the download, and does not hammer a host that just refused it. No network
calls are made.
"""

from ideasdk.aws.ec2_price_list import (
    EC2PriceList,
    PRICE_MAP_TTL_SECS,
    PRICE_MAP_FAILURE_TTL_SECS,
    parse_ec2_offer_csv,
)

import threading
import time

REGION = 'us-gov-west-1'

# the five metadata lines the offer file opens with, before the header row
OFFER_PREAMBLE = [
    '"FormatVersion","v1.0"',
    '"Disclaimer","This pricing list is for informational purposes only."',
    '"Publication Date","2026-08-31T18:13:31Z"',
    '"Version","20260831181331"',
    '"OfferCode","AmazonEC2"',
]

HEADER = (
    '"SKU","TermType","Unit","PricePerUnit","LeaseContractLength","PurchaseOption",'
    '"OfferingClass","Instance Type","Tenancy","Operating System","Pre Installed S/W",'
    '"CapacityStatus","Region Code"'
)


def row(
    sku='SKU',
    term_type='OnDemand',
    unit='Hrs',
    price='0.1210000000',
    lease='',
    purchase='',
    offering='',
    instance_type='m6i.large',
    tenancy='Shared',
    operating_system='Linux',
    preinstalled='NA',
    capacity='Used',
    region=REGION,
):
    values = [
        sku,
        term_type,
        unit,
        price,
        lease,
        purchase,
        offering,
        instance_type,
        tenancy,
        operating_system,
        preinstalled,
        capacity,
        region,
    ]
    return ','.join(f'"{value}"' for value in values)


OFFER_ROWS = [
    # the two rows a linux instance hour is actually priced from
    row(sku='A1'),
    row(
        sku='A2',
        term_type='Reserved',
        price='0.0800400000',
        lease='1yr',
        purchase='No Upfront',
        offering='standard',
    ),
    # on-demand only: no reserved rate published for this type
    row(sku='B1', instance_type='m6g.large', price='0.0968000000'),
    # everything below must be skipped
    row(sku='C1', instance_type='c5.xlarge', operating_system='Windows', price='0.4'),
    row(sku='C2', instance_type='c5.xlarge', tenancy='Dedicated', price='0.5'),
    row(sku='C3', instance_type='c5.xlarge', preinstalled='SQL Std', price='0.9'),
    row(
        sku='C4',
        instance_type='c5.xlarge',
        capacity='UnusedCapacityReservation',
        price='0.6',
    ),
    row(sku='C5', instance_type='c5.xlarge', region='us-east-1', price='0.17'),
    row(sku='C6', instance_type='c5.xlarge', unit='Quantity', price='7.0'),
    row(sku='C7', instance_type='c5.xlarge', price='0.0000000000'),
    row(
        sku='C8',
        instance_type='m6i.large',
        term_type='Reserved',
        price='0.0500000000',
        lease='3yr',
        purchase='All Upfront',
        offering='convertible',
    ),
    row(sku='C9', instance_type='', price='0.25'),
]

OFFER_LINES = OFFER_PREAMBLE + [HEADER] + OFFER_ROWS


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class StubLogger:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))

    def info(self, message, *args, **kwargs):
        self.infos.append(str(message))


# parsing


def test_the_metadata_preamble_is_skipped_and_the_header_row_is_used():
    prices = parse_ec2_offer_csv(OFFER_LINES, REGION)

    # a parser that read the first line as the header would find no instance types
    assert set(prices) == {'m6i.large', 'm6g.large'}


def test_the_on_demand_linux_rate_is_read():
    prices = parse_ec2_offer_csv(OFFER_LINES, REGION)

    assert prices['m6i.large'].ondemand == 0.121


def test_the_one_year_no_upfront_standard_reserved_rate_is_read():
    prices = parse_ec2_offer_csv(OFFER_LINES, REGION)

    # the same term the commercial pricing api path reports, so the two agree
    assert prices['m6i.large'].reserved == 0.08004


def test_an_instance_type_with_no_reserved_rate_reports_no_saving():
    prices = parse_ec2_offer_csv(OFFER_LINES, REGION)

    # a zero here would compute to a saving of the whole hourly rate, which lands as a
    # job that cost nothing
    assert prices['m6g.large'].reserved == prices['m6g.large'].ondemand


def test_rows_for_anything_other_than_a_shared_linux_instance_hour_are_skipped():
    prices = parse_ec2_offer_csv(OFFER_LINES, REGION)

    # windows, dedicated tenancy, bundled software, an idle reservation, another
    # region, a non hourly unit and a zero price are all present in the fixture
    assert 'c5.xlarge' not in prices


def test_an_empty_offer_file_parses_to_an_empty_map():
    assert parse_ec2_offer_csv(OFFER_PREAMBLE + [HEADER], REGION) == {}


# cache and background refresh


class StubFetch:
    """stands in for the download. blocks until released, and counts its calls."""

    def __init__(self, prices=None, raises=False):
        self.prices = prices if prices is not None else {}
        self.raises = raises
        self.calls = 0
        self.release = threading.Event()
        self.release.set()

    def __call__(self):
        self.calls += 1
        self.release.wait(timeout=5)
        if self.raises:
            raise RuntimeError('connection refused')
        return dict(self.prices)


def build_offer_fetch():
    return StubFetch(prices=parse_ec2_offer_csv(OFFER_LINES, REGION))


def build_price_list(fetch):
    price_list = EC2PriceList(region=REGION, logger=StubLogger())
    price_list._fetch = fetch
    return price_list


def test_the_first_read_reports_no_price_rather_than_waiting_for_the_download():
    fetch = build_offer_fetch()
    fetch.release.clear()
    price_list = build_price_list(fetch)

    # the offer file is a few hundred megabytes and this is a page a user is waiting on
    assert price_list.get('m6i.large') is None
    fetch.release.set()

    assert wait_for(lambda: price_list.get('m6i.large') is not None)
    assert price_list.get('m6i.large').ondemand == 0.121


def test_a_loaded_map_is_served_without_fetching_again():
    fetch = build_offer_fetch()
    price_list = build_price_list(fetch)
    assert wait_for(lambda: price_list.get('m6i.large') is not None)

    for _ in range(5):
        assert price_list.get('m6i.large').ondemand == 0.121
    assert fetch.calls == 1


def test_the_previous_map_is_served_while_a_refresh_runs():
    fetch = build_offer_fetch()
    price_list = build_price_list(fetch)
    assert wait_for(lambda: price_list.get('m6i.large') is not None)

    # a day has passed, so the next read starts a rebuild
    price_list._loaded_at = time.time() - PRICE_MAP_TTL_SECS - 1
    fetch.release.clear()

    assert price_list.get('m6i.large').ondemand == 0.121
    fetch.release.set()
    assert wait_for(lambda: fetch.calls == 2)


def test_a_failed_fetch_is_not_retried_on_the_next_read():
    fetch = StubFetch(raises=True)
    price_list = build_price_list(fetch)

    assert price_list.get('m6i.large') is None
    assert wait_for(lambda: fetch.calls == 1)

    for _ in range(5):
        assert price_list.get('m6i.large') is None
    # a host that refused once must not be asked again on every page load
    assert fetch.calls == 1


def test_a_failed_fetch_is_tried_again_once_the_failure_window_passes():
    fetch = StubFetch(raises=True)
    price_list = build_price_list(fetch)
    price_list.get('m6i.large')
    assert wait_for(lambda: fetch.calls == 1)

    price_list._failed_at = time.time() - PRICE_MAP_FAILURE_TTL_SECS - 1
    price_list.get('m6i.large')

    assert wait_for(lambda: fetch.calls == 2)


def test_a_failure_is_logged_with_the_url_and_never_as_a_price():
    fetch = StubFetch(raises=True)
    price_list = build_price_list(fetch)
    price_list.get('m6i.large')
    assert wait_for(lambda: len(price_list._logger.warnings) == 1)

    assert price_list.offer_url() in price_list._logger.warnings[0]
    assert price_list.get('m6i.large') is None


def test_a_load_is_logged_with_the_row_count():
    fetch = build_offer_fetch()
    price_list = build_price_list(fetch)
    price_list.get('m6i.large')
    assert wait_for(lambda: len(price_list._logger.infos) == 1)

    assert '2 ec2 instance type prices' in price_list._logger.infos[0]


def test_an_instance_type_the_region_does_not_sell_has_no_price():
    fetch = build_offer_fetch()
    price_list = build_price_list(fetch)
    assert wait_for(lambda: price_list.get('m6i.large') is not None)

    assert price_list.get('p5.48xlarge') is None


def test_the_offer_url_names_the_cluster_region():
    price_list = EC2PriceList(region=REGION, logger=StubLogger())

    assert price_list.offer_url().endswith(f'/AmazonEC2/current/{REGION}/index.csv')
    assert price_list.offer_url().startswith('https://')
