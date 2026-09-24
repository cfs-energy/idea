from typing import Dict, List
import re

from ideasdk.utils import Utils

METRIC_PREFIX = 'idea'
_TAG_UNSAFE = re.compile(r'[,|#\n\r\t ]+')


class DatadogFormat:
    def __init__(self, namespace: str):
        self.base_tags: List[str] = []
        for key, value in zip(
            ('idea_cluster', 'idea_module', 'component'), namespace.split('/')
        ):
            if Utils.is_not_empty(value):
                self.base_tags.append(f'{key}:{self._tag_value(value)}')

    @staticmethod
    def _tag_value(value) -> str:
        text = _TAG_UNSAFE.sub('_', str(value).strip())
        return text[:200] if Utils.is_not_empty(text) else 'unknown'

    @staticmethod
    def complete_entries(metric_data: List[Dict]) -> List[Dict]:
        # BaseMetrics includes dimension splits for exporters without tagged series.
        # Sending those splits to Datadog would count the same event repeatedly.
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

    def name_and_tags(self, entry):
        name = entry['MetricName']
        collector = name.startswith(('cost.', 'storage.'))
        tags = [
            tag
            for tag in self.base_tags
            if not collector or tag.startswith('idea_cluster:')
        ]
        for dimension in Utils.get_value_as_list('Dimensions', entry, []):
            key = _TAG_UNSAFE.sub('_', str(dimension.get('Name', '')).strip().lower())
            if Utils.is_empty(key) or (collector and key == 'host'):
                continue
            tags.append(f'{key}:{self._tag_value(dimension.get("Value"))}')
        return f'{METRIC_PREFIX}.{name}', tags
