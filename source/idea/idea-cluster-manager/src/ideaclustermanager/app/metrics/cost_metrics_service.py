"""
The account's spend from Cost Explorer, published as metrics.

Cost Explorer prices everything the scheduler does not: desktops, the control plane, storage,
shared infrastructure. Each family partitions the bill by one grouping, so totals within a
family add up to the bill and totals across families never should:

  cost             module x project x owner. Cost Explorer groups by two keys at most, so the
                   module goes into the filter, one request per module, plus one for spend that
                   carries no module tag at all.
  cost.by_service  module x service.
  cost.storage     the storage services by usage type, tag-blind so history from before the
                   cost allocation tags survives.
  cost.by_account  module x linked account, when the account can see other accounts' spend.

Both bases are published: unblended is what the invoice shows, amortized spreads savings
plans and reservations, which arrive as untagged negatives. Every point is one day's absolute
total stamped at that day, so a day Cost Explorer has since revised is replaced on the next
run, and a run repeated is a run with no effect.
"""

from ideaclustermanager.app.metrics.collector_outbox import CollectorOutbox
from ideasdk.context import SocaContext
from ideasdk.metrics import BaseMetrics
from ideasdk.service import SocaService
from ideasdk.utils import Utils

from typing import Dict, Iterable, List, Optional, Tuple
import arrow
import threading

AWS_PARTITION_COMMERCIAL = 'aws'
CE_AMORTIZED = 'AmortizedCost'
CE_UNBLENDED = 'UnblendedCost'
FAMILY = 'cost'
FAMILY_BY_SERVICE = 'cost.by_service'
FAMILY_STORAGE = 'cost.storage'
FAMILY_BY_ACCOUNT = 'cost.by_account'
STORAGE_SERVICES = ['Amazon FSx', 'Amazon Elastic File System']
UNKNOWN = 'unknown'


def tag_value(value) -> str:
    """lower case, spaces folded; empty is 'unknown' so the family stays a partition."""
    text = str(value).strip().lower() if value is not None else ''
    return text.replace(' ', '_') if text else UNKNOWN


def strip_tag_key(key: str) -> str:
    """Cost Explorer returns a tag group as 'idea:Project$name'; bare 'idea:Project$' is untagged."""
    if '$' in key:
        return key.split('$', 1)[1]
    return key


def amount(metrics: Dict, key: str) -> float:
    value = Utils.get_value_as_dict(key, metrics, {})
    return Utils.get_value_as_float('Amount', value, 0.0)


class CostRow:
    __slots__ = ('family', 'day', 'dimensions', 'amortized', 'unblended')

    def __init__(
        self,
        family: str,
        day: str,
        dimensions: Dict[str, str],
        amortized: float,
        unblended: float,
    ):
        self.family = family
        self.day = day
        self.dimensions = dimensions
        self.amortized = amortized
        self.unblended = unblended


class CostMetrics(BaseMetrics):
    def __init__(self, context: SocaContext):
        super().__init__(context, split_dimensions=False)

    def publish(
        self,
        family: str,
        day_epoch: int,
        dimensions: Dict[str, str],
        amortized: float,
        unblended: float,
    ):
        self.push_dimensions()
        try:
            for name in sorted(dimensions):
                self.with_dimension(name, dimensions[name])
            self.count(
                MetricName=f'{family}.amortized', Value=amortized, Timestamp=day_epoch
            )
            self.count(
                MetricName=f'{family}.unblended', Value=unblended, Timestamp=day_epoch
            )
        finally:
            self.pop_dimensions()


def aggregate(rows: Iterable[CostRow]) -> List[CostRow]:
    """one row per (family, day, dimensions), summed: what a point carries."""
    totals: Dict[Tuple[str, str, Tuple[Tuple[str, str], ...]], CostRow] = {}
    for row in rows:
        key = (row.family, row.day, tuple(sorted(row.dimensions.items())))
        found = totals.get(key)
        if found is None:
            totals[key] = CostRow(
                row.family, row.day, dict(row.dimensions), row.amortized, row.unblended
            )
        else:
            found.amortized += row.amortized
            found.unblended += row.unblended
    return [totals[key] for key in sorted(totals)]


class CostExplorerReader:
    """the Cost Explorer requests, paginated; every one is billed."""

    def __init__(self, client, module_tag: str, project_tag: str, owner_tag: str):
        self.client = client
        self.module_tag = module_tag
        self.project_tag = project_tag
        self.owner_tag = owner_tag

    @staticmethod
    def _period(start: arrow.Arrow, end: arrow.Arrow) -> Dict[str, str]:
        return {'Start': start.format('YYYY-MM-DD'), 'End': end.format('YYYY-MM-DD')}

    def modules(self, start: arrow.Arrow, end: arrow.Arrow) -> List[str]:
        """distinct values of the module tag in the window. A value dropped here never gets
        its own request and its spend leaves the family, so every page is read."""
        values: List[str] = []
        token: Optional[str] = None
        while True:
            request = {
                'TimePeriod': self._period(start, end),
                'TagKey': self.module_tag,
            }
            if token:
                request['NextPageToken'] = token
            response = self.client.get_tags(**request)
            for value in Utils.get_value_as_list('Tags', response, []):
                if Utils.is_not_empty(value):
                    values.append(value)
            token = Utils.get_value_as_string('NextPageToken', response)
            if Utils.is_empty(token):
                return sorted(values)

    def query(
        self,
        family: str,
        start: arrow.Arrow,
        end: arrow.Arrow,
        group_by: List[Dict[str, str]],
        keys: List[str],
        filter_expression: Optional[Dict] = None,
        constant: Optional[Dict[str, str]] = None,
    ) -> List[CostRow]:
        rows: List[CostRow] = []
        token: Optional[str] = None
        while True:
            request = {
                'TimePeriod': self._period(start, end),
                'Granularity': 'DAILY',
                'Metrics': [CE_AMORTIZED, CE_UNBLENDED],
                'GroupBy': group_by,
            }
            if filter_expression is not None:
                request['Filter'] = filter_expression
            if token:
                request['NextPageToken'] = token
            response = self.client.get_cost_and_usage(**request)
            for result in Utils.get_value_as_list('ResultsByTime', response, []):
                day = Utils.get_value_as_string(
                    'Start', Utils.get_value_as_dict('TimePeriod', result, {})
                )
                if Utils.is_empty(day):
                    continue
                for group in Utils.get_value_as_list('Groups', result, []):
                    metrics = Utils.get_value_as_dict('Metrics', group, {})
                    amortized = amount(metrics, CE_AMORTIZED)
                    unblended = amount(metrics, CE_UNBLENDED)
                    dimensions = dict(constant or {})
                    group_keys = Utils.get_value_as_list('Keys', group, [])
                    for index, name in enumerate(keys):
                        raw = group_keys[index] if index < len(group_keys) else ''
                        dimensions[name] = tag_value(
                            strip_tag_key(Utils.get_as_string(raw, ''))
                        )
                    rows.append(CostRow(family, day, dimensions, amortized, unblended))
            token = Utils.get_value_as_string('NextPageToken', response)
            if Utils.is_empty(token):
                return rows

    def fetch_all(
        self, start: arrow.Arrow, end: arrow.Arrow, by_account: bool
    ) -> List[CostRow]:
        tag = lambda key: {'Type': 'TAG', 'Key': key}  # noqa: E731
        dimension = lambda key: {'Type': 'DIMENSION', 'Key': key}  # noqa: E731
        project_owner = [tag(self.project_tag), tag(self.owner_tag)]

        rows: List[CostRow] = []
        for module in self.modules(start, end):
            rows.extend(
                self.query(
                    FAMILY,
                    start,
                    end,
                    project_owner,
                    ['project', 'owner'],
                    {'Tags': {'Key': self.module_tag, 'Values': [module]}},
                    {'module': tag_value(module)},
                )
            )
        # Spend with no module tag at all. Without it the family stops being a partition of
        # the bill and every total quietly under-reports.
        rows.extend(
            self.query(
                FAMILY,
                start,
                end,
                project_owner,
                ['project', 'owner'],
                {'Tags': {'Key': self.module_tag, 'MatchOptions': ['ABSENT']}},
                {'module': UNKNOWN},
            )
        )
        rows.extend(
            self.query(
                FAMILY_BY_SERVICE,
                start,
                end,
                [tag(self.module_tag), dimension('SERVICE')],
                ['module', 'service'],
            )
        )
        rows.extend(
            self.query(
                FAMILY_STORAGE,
                start,
                end,
                [dimension('SERVICE'), dimension('USAGE_TYPE')],
                ['service', 'usage_type'],
                {'Dimensions': {'Key': 'SERVICE', 'Values': STORAGE_SERVICES}},
            )
        )
        if by_account:
            rows.extend(
                self.query(
                    FAMILY_BY_ACCOUNT,
                    start,
                    end,
                    [tag(self.module_tag), dimension('LINKED_ACCOUNT')],
                    ['module', 'account_id'],
                )
            )
        return rows


class CostMetricsService(SocaService):
    def __init__(self, context: SocaContext):
        super().__init__(context)
        self.context = context
        self.logger = context.logger('cost-metrics')
        self._provider_warning_logged = False
        self._exit = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name='cost-metrics', daemon=True
        )

    def service_id(self) -> str:
        return 'cost-metrics'

    def _config_key(self, suffix: str) -> str:
        return f'{self.context.module_id()}.metrics.cost.{suffix}'

    def is_enabled(self) -> bool:
        if not self.context.config().get_bool(self._config_key('enabled'), False):
            return False
        provider = self.context.config().get_string('metrics.provider')
        if provider != 'dogstatsd':
            if not self._provider_warning_logged:
                self.logger.warning(
                    f'cost metrics disabled for provider {provider!r}: '
                    'absolute daily totals require timestamped replacement, '
                    'including negative corrections'
                )
                self._provider_warning_logged = True
            return False
        # Cost Explorer has no endpoint outside the commercial partition.
        return self.context.aws().aws_partition() == AWS_PARTITION_COMMERCIAL

    def get_interval_seconds(self) -> int:
        return (
            max(1, self.context.config().get_int(self._config_key('interval_hours'), 6))
            * 3600
        )

    def get_lookback_days(self) -> int:
        return max(
            1, self.context.config().get_int(self._config_key('lookback_days'), 3)
        )

    def reader(self) -> CostExplorerReader:
        config = self.context.config()
        return CostExplorerReader(
            self.context.aws().cost_explorer(),
            config.get_string(self._config_key('module_tag'), 'idea:ModuleId'),
            config.get_string(self._config_key('project_tag'), 'idea:Project'),
            config.get_string(self._config_key('owner_tag'), 'idea:JobOwner'),
        )

    def start(self):
        if not self.is_enabled():
            self.logger.debug('cost metrics are disabled. skip.')
            return
        self._thread.start()

    def stop(self):
        self._exit.set()
        if self._thread.is_alive():
            self._thread.join()

    def _loop(self):
        while not self._exit.is_set():
            try:
                self.run_once()
            except Exception as e:
                self.logger.exception(f'cost metrics failed: {e}')
            finally:
                self._exit.wait(self.get_interval_seconds())

    def window(
        self, now: Optional[arrow.Arrow] = None
    ) -> Tuple[arrow.Arrow, arrow.Arrow]:
        """the trailing full days: End is exclusive, so today, still being billed, is left out."""
        today = (now or arrow.utcnow()).floor('day')
        return today.shift(days=-self.get_lookback_days()), today

    def run_once(self):
        lock_key = f'{self.context.module_id()}-cost-metrics'
        try:
            self.context.distributed_lock().acquire(key=lock_key)
        except Exception as e:
            self.logger.info(f'cost metrics are running elsewhere: {e}')
            return
        try:
            # The standalone collector has no settings table: one task, no checkpoint.
            db = getattr(self.context.config(), 'db', None)
            checkpoint_key = self._config_key('last_collected')
            outbox = CollectorOutbox(
                self.context, self._config_key('outbox'), historical=True
            )
            outbox.replay()
            # A replica's settings cache can lag behind the previous lock holder.
            # A consistent read avoids repeating collection while saved metrics remain retryable.
            entry = (
                db.cluster_settings_table.get_item(
                    Key={'key': checkpoint_key}, ConsistentRead=True
                ).get('Item', {})
                if db is not None
                else {}
            )
            last_collected = entry.get('value')
            if last_collected is not None and (
                arrow.utcnow().timestamp() - float(last_collected)
                < self.get_interval_seconds() / 2
            ):
                return
            start, end = self.window()
            by_account = self.context.config().get_bool(
                self._config_key('by_account'), False
            )
            rows = aggregate(self.reader().fetch_all(start, end, by_account))
            metrics = CostMetrics(outbox)
            for row in rows:
                day_epoch = int(arrow.get(row.day).timestamp())
                metrics.publish(
                    row.family, day_epoch, row.dimensions, row.amortized, row.unblended
                )
            outbox.save()
            if db is not None:
                db.set_config_entry(checkpoint_key, arrow.utcnow().timestamp())
            outbox.replay()
            self.logger.info(
                f'cost metrics collected: {len(rows)} rows for {start.format("YYYY-MM-DD")}..{end.format("YYYY-MM-DD")}'
            )
        finally:
            self.context.distributed_lock().release(key=lock_key)
