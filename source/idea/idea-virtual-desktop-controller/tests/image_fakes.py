"""
An in-memory stand-in for ImageBuildRecordsDB(kind=desktop): rows round-trip through the
real to_item / from_item (range key variants, timestamps, migration), and put_if / claim
apply the same conditions the DynamoDB expressions encode.
"""

from ideadatamodel import IMAGE_ROW_IN_FLIGHT, ImageKind, ImageRowKey
from ideasdk.aws.image_builds import ImageBuildRecordsDB


class FakeRecords:
    kind = ImageKind.DESKTOP

    def __init__(self):
        self.items = {}

    @staticmethod
    def _key(base_os, architecture, variant=None):
        return (
            base_os,
            ImageRowKey(
                base_os=base_os, architecture=architecture or 'x86_64', variant=variant
            ).range_key(),
        )

    def _record_key(self, record):
        item = ImageBuildRecordsDB.to_item(record)
        return (item['base_os'], item['architecture'])

    def get(self, base_os, architecture, variant=None):
        item = self.items.get(self._key(base_os, architecture, variant))
        return ImageBuildRecordsDB.from_item(dict(item), self.kind) if item else None

    def put(self, record):
        self.items[self._record_key(record)] = ImageBuildRecordsDB.to_item(record)
        return record

    def put_if(self, record, expected):
        stored = self.items.get(self._record_key(record)) or {}
        for name, value in expected.items():
            actual = stored.get(name)
            if value is None:
                ok = actual is None
            elif isinstance(value, (list, tuple, set, frozenset)):
                ok = actual not in value
            else:
                ok = actual == value
            if not ok:
                return False
        self.put(record)
        return True

    def claim(self, record):
        stored = self.items.get(self._record_key(record))
        if stored and (
            stored.get('status') in IMAGE_ROW_IN_FLIGHT or stored.get('pinned')
        ):
            return False
        self.put(record)
        return True

    def delete(self, base_os, architecture, variant=None):
        self.items.pop(self._key(base_os, architecture, variant), None)

    def list_all(self):
        return [
            ImageBuildRecordsDB.from_item(dict(item), self.kind)
            for item in self.items.values()
        ]

    def update_fields(self, record, fields, unless_status=None):
        key = self._record_key(record)
        stored = self.items.get(key)
        if stored is None:
            return False
        if unless_status and stored.get('status') in unless_status:
            return False
        for name, value in fields.items():
            if value is None:
                stored.pop(name, None)
            else:
                stored[name] = value
        return True
