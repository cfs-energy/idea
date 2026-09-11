from ideasdk.protocols import MetricsProviderProtocol, SocaContextProtocol
from ideasdk.utils import Utils

from typing import Dict, List, Optional, Tuple
from threading import RLock
from urllib.parse import urlparse
import os
import re
import socket

DEFAULT_URL = 'udp://127.0.0.1:8125'
METRIC_PREFIX = 'idea'
# The agent reads datagrams of up to 8 KiB; one datagram carries many newline-separated lines.
MAX_DATAGRAM_BYTES = 8192
# Tag values may not carry the characters the wire format uses to delimit.
_TAG_UNSAFE = re.compile(r'[,|#\n\r\t ]+')

# Counters become counts and Summaries distributions, so the agent aggregates across
# tasks; anything else is a gauge.
_METRIC_TYPES = {'Counter': 'c', 'Summary': 'd'}


class DogStatsdMetrics(MetricsProviderProtocol):
    """
    Ship IDEA metrics to a Datadog agent over DogStatsD.

    The namespace (cluster/module[/component]) and the metric dimensions travel as tags.
    BaseMetrics publishes one entry per dimension plus one carrying all of them; only the
    complete entry is sent, since a tagged metric already slices by every tag on it.

    The target is metrics.dogstatsd.url, then DD_DOGSTATSD_URL, then UDP on localhost:
    udp://host:port, or unix:///path/to/dsd.socket when the agent shares a volume with
    the task. Sending never raises: a missing agent costs a warning, not the request.
    """

    def __init__(self, context: SocaContextProtocol, namespace: str):
        self.context = context
        self.logger = context.logger('dogstatsd-metrics')
        self.namespace = namespace

        self.base_tags: List[str] = []
        for key, value in zip(
            ('idea_cluster', 'idea_module', 'component'), namespace.split('/')
        ):
            if Utils.is_not_empty(value):
                self.base_tags.append(f'{key}:{self._tag_value(value)}')

        url = context.config().get_string('metrics.dogstatsd.url')
        if Utils.is_empty(url):
            url = os.environ.get('DD_DOGSTATSD_URL', DEFAULT_URL)
        self._family, self._address = self._parse_url(url)

        self._socket: Optional[socket.socket] = None
        self._lock = RLock()
        self._send_failures = 0

    @staticmethod
    def _parse_url(url: str) -> Tuple[int, object]:
        parsed = urlparse(url)
        if parsed.scheme == 'unix' and Utils.is_not_empty(parsed.path):
            return socket.AF_UNIX, parsed.path
        if parsed.scheme == 'udp' and Utils.is_not_empty(parsed.hostname):
            return socket.AF_INET, (parsed.hostname, parsed.port or 8125)
        raise ValueError(
            f'metrics.dogstatsd.url must be udp://host:port or unix:///path, got: {url}'
        )

    @staticmethod
    def _tag_value(value) -> str:
        text = _TAG_UNSAFE.sub('_', str(value).strip())
        return text[:200] if Utils.is_not_empty(text) else 'unknown'

    @staticmethod
    def _value(value) -> str:
        # plain decimals: no exponent, no trailing zeros, so 86514.0 travels as 86514
        text = f'{float(value):.6f}'.rstrip('0').rstrip('.')
        return text if text not in ('', '-0') else '0'

    @staticmethod
    def complete_entries(metric_data: List[Dict]) -> List[Dict]:
        """
        one entry per metric name: the one with the most dimensions. BaseMetrics emits
        the per-dimension splits and the complete entry in one batch; sending all of them
        would count the same event once per dimension.
        """
        chosen: Dict[str, Dict] = {}
        order: List[str] = []
        for entry in metric_data:
            name = entry.get('MetricName')
            width = len(Utils.get_value_as_list('Dimensions', entry, []))
            if name not in chosen:
                order.append(name)
                chosen[name] = entry
            elif width > len(Utils.get_value_as_list('Dimensions', chosen[name], [])):
                chosen[name] = entry
        return [chosen[name] for name in order]

    def format_entry(self, entry: Dict) -> Optional[str]:
        name = entry.get('MetricName')
        if Utils.is_empty(name):
            return None
        value = entry.get('Value')
        if value is None:
            return None
        metric_type = _METRIC_TYPES.get(entry.get('MetricType'), 'g')
        tags = list(self.base_tags)
        for dimension in Utils.get_value_as_list('Dimensions', entry, []):
            key = _TAG_UNSAFE.sub('_', str(dimension.get('Name', '')).strip().lower())
            if Utils.is_empty(key):
                continue
            tags.append(f'{key}:{self._tag_value(dimension.get("Value"))}')
        line = f'{METRIC_PREFIX}.{name}:{self._value(value)}|{metric_type}'
        if len(tags) > 0:
            line = f'{line}|#{",".join(tags)}'
        return line

    def log(self, metric_data: List[Dict]):
        lines = []
        for entry in self.complete_entries(metric_data):
            line = self.format_entry(entry)
            if line is not None:
                lines.append(line)
        if len(lines) == 0:
            return

        datagram = b''
        for line in lines:
            encoded = line.encode('utf-8')
            if len(datagram) + len(encoded) + 1 > MAX_DATAGRAM_BYTES and len(datagram) > 0:
                self._send(datagram)
                datagram = b''
            datagram = encoded if len(datagram) == 0 else datagram + b'\n' + encoded
        if len(datagram) > 0:
            self._send(datagram)

    def _send(self, datagram: bytes):
        with self._lock:
            try:
                if self._socket is None:
                    self._socket = socket.socket(self._family, socket.SOCK_DGRAM)
                    self._socket.setblocking(False)
                self._socket.sendto(datagram, self._address)
                self._send_failures = 0
            except OSError as e:
                if self._socket is not None:
                    self._socket.close()
                    self._socket = None
                self._send_failures += 1
                # the first failure and then one in a hundred: the agent being down is
                # one fact, not one log line per metric.
                if self._send_failures == 1 or self._send_failures % 100 == 0:
                    self.logger.warning(
                        f'dogstatsd send to {self._address} failed '
                        f'({self._send_failures} in a row): {e}'
                    )

    def flush(self):
        # datagrams leave as they are built; nothing is held back.
        pass
