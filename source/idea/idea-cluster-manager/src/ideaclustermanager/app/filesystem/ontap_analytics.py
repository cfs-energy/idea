"""
Folder sizes from ONTAP File System Analytics, read through the SVM management endpoint.

ONTAP keeps the rollups per directory, so a request costs one REST call rather than a walk, whoever
asks and however many files sit below. It totals every owner together: which folder holds one
person's bytes is not something it can say. The login is the storage metrics one, so it needs read
access to /api/storage/volumes/*/files as well as quota reports and volumes, and it can see the
whole SVM: callers check the person's own access before showing anything it returns.
"""

import re
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timezone
from urllib.parse import quote

from ideaclustermanager.app.metrics.storage_metrics_service import (
    FileContentsRefused,
    OntapClient,
)

VOLUMES = (
    '/api/storage/volumes?return_records=true&max_records=1000'
    '&fields=name,uuid,nas.path,files.used,analytics'
)
ANALYTICS_FIELDS = (
    'name,type,is_junction,analytics.bytes_used,analytics.file_count,'
    'analytics.subdir_count,analytics.incomplete_data,'
    'analytics.by_modified_time.bytes_used.values,'
    'analytics.by_modified_time.bytes_used.newest_label,'
    'analytics.by_modified_time.bytes_used.oldest_label'
)
LABELS_FIELD = 'analytics.by_modified_time.bytes_used.labels'
LABELS_SECONDS = 3600
MONTH = re.compile(r'^\d{4}-\d{2}$')
SCAN_COMPLETE_FRACTION = 0.9
MAX_ROWS = 1000
# a folder's listing is read to its end: what is left out would count as files directly in it.
# these bound one read, and a folder past them says so.
MAX_PAGES = 5
MAX_READ_SECONDS = 30
FOLDER_SECONDS = 900
REFRESHED_SECONDS = 60
VOLUME_SECONDS = 300
RETRY_SECONDS = 60
READ_TIMEOUT_SECONDS = 10
MAX_CONCURRENT_READS = 4
MAX_CACHED_FOLDERS = 5000
# what ONTAP answers about the filer itself: a bad login, a timeout, too many requests
FILER_ERRORS = {401, 408, 429}


def refused_this_path(error):
    """ONTAP refused this one path (a file, not a folder, or a volume the role cannot read), or
    the guard did before sending it. Anyone can ask for any path, so that says nothing about the
    filer, and must not turn analytics off for everyone else."""
    if isinstance(error, (FileNotFoundError, FileContentsRefused)):
        return True
    status = getattr(error, 'status_code', None)
    return isinstance(status, int) and 400 <= status < 500 and status not in FILER_ERRORS


def bucket_end(label):
    """the last day a histogram bucket covers: 2026-W44, 2026-10, 2026-Q3, 2026, --2015."""
    try:
        text = label.removeprefix('--')
        if '-W' in text:
            year, week = text.split('-W')
            return datetime.strptime(f'{year}-{week}-0', '%G-%V-%w').date()
        if '-Q' in text:
            year, quarter = text.split('-Q')
            month = int(quarter) * 3
            return date(int(year) + month // 12, month % 12 + 1, 1)
        if '-' in text:
            year, month = (int(part) for part in text.split('-'))
            return date(year + month // 12, month % 12 + 1, 1)
        return date(int(text), 12, 31)
    except (ValueError, AttributeError):
        return None


def ages(analytics, labels, now):
    """(bytes unchanged since before the last three calendar months, newest, oldest). The histogram
    repeats the same bytes as weeks, months, quarters and years, and its only recent buckets are
    months, so that is the closest it comes to 90 days: 61 to 92 days, depending on the date."""
    histogram = (analytics.get('by_modified_time') or {}).get('bytes_used') or {}
    buckets = dict(zip(labels or [], histogram.get('values') or []))
    recent = sum(used for label, used in buckets.items() if MONTH.match(label))
    unchanged = max(
        0, analytics.get('bytes_used', 0) - recent - buckets.get('unknown', 0)
    )

    def stamp(label):
        end = bucket_end(label)
        if end is None:
            return None
        moment = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
        return min(now, moment.timestamp())

    return (
        unchanged,
        stamp(histogram.get('newest_label')),
        stamp(histogram.get('oldest_label')),
    )


def to_row(path, name, record, labels, now):
    analytics = record.get('analytics')
    if not analytics:
        # state=on is reported at once; a folder the first scan has not reached has no analytics.
        return dict(
            path=path,
            name=name,
            bytes=0,
            files=0,
            newest_mtime=None,
            oldest_mtime=None,
            unchanged_90_days_bytes=0,
            partial=True,
            measured=False,
        )
    unchanged, newest, oldest = ages(analytics, labels, now)
    return dict(
        path=path,
        name=name,
        bytes=analytics.get('bytes_used', 0),
        files=analytics.get('file_count', 0),
        subfolders=analytics.get('subdir_count', 0),
        newest_mtime=newest,
        oldest_mtime=oldest,
        unchanged_90_days_bytes=unchanged,
        partial=bool(analytics.get('incomplete_data')),
    )


class FolderAnalytics:
    def __init__(self, context, collector):
        self.context = context
        self.collector = collector
        self.logger = context.logger('storage-usage')
        self._lock = threading.Lock()
        self._reads = threading.BoundedSemaphore(MAX_CONCURRENT_READS)
        self._folders = OrderedDict()
        self._folder_locks = {}
        self._volumes = {}
        self._labels = {}
        self._failed_at = 0

    def _client(self, target):
        config = self.context.config()
        return OntapClient(
            target.endpoint,
            target.username,
            config.get_secret(target.password_key),
            verify_tls=config.get_bool(self.collector._config_key('verify_tls'), False),
            timeout=READ_TIMEOUT_SECONDS,
        )

    def _attachment(self, path):
        """the attachment whose mount holds this path, and the path from the SVM's namespace root."""
        config = self.context.config()
        for target in self.collector.targets():
            prefix = f'shared-storage.{target.name}'
            mount = (config.get_string(f'{prefix}.mount_dir') or '').rstrip('/')
            export = config.get_string(f'{prefix}.fsx_netapp_ontap.volume.volume_path')
            if mount and export and (path == mount or path.startswith(f'{mount}/')):
                return target, f'{export.rstrip("/")}{path[len(mount) :]}'
        return None, None

    def _volume_list(self, target):
        now = time.monotonic()
        with self._lock:
            cached = self._volumes.get(target.name)
            if cached and now - cached[0] < VOLUME_SECONDS:
                return cached[1]
        volumes = self._client(target).get(VOLUMES)
        with self._lock:
            self._volumes[target.name] = (now, volumes)
        return volumes

    def status(self, path):
        """on, off, initializing or unavailable, with the volume and its relative path when on."""
        if time.monotonic() - self._failed_at < RETRY_SECONDS:
            return dict(state='unavailable')
        target, namespace = self._attachment(path)
        if target is None:
            return dict(state='unavailable')
        try:
            volumes = self._volume_list(target)
        except Exception as e:
            return self._failed(e)
        best = None
        for volume in volumes:
            junction = (volume.get('nas') or {}).get('path')
            if not junction:
                continue
            root = junction.rstrip('/')
            if namespace == root or namespace.startswith(f'{root}/'):
                if best is None or len(root) > len(best[0]):
                    best = (root, volume)
        if best is None:
            return dict(state='unavailable')
        root, volume = best
        analytics = volume.get('analytics') or {}
        state = analytics.get('state')
        if state not in ('on', 'initializing'):
            return dict(state='off')
        # ONTAP says on within seconds of enabling and reports no scan progress, so the first
        # scan shows only as the volume's rollup catching up with its file count.
        used = (volume.get('files') or {}).get('used') or 0
        counted = analytics.get('file_count', 0)
        if state == 'initializing' or (
            used and counted < used * SCAN_COMPLETE_FRACTION
        ):
            return dict(
                state='initializing',
                progress=min(99, counted * 100 // used) if used else 0,
            )
        return dict(
            state='on',
            target=target,
            volume=volume.get('name'),
            uuid=volume['uuid'],
            relative=namespace[len(root) :].lstrip('/'),
        )

    def _failed(self, error):
        # Names and paths stay out of the log: only the kind of failure is recorded.
        self.logger.warning(f'storage analytics read failed: {type(error).__name__}')
        self._failed_at = time.monotonic()
        return dict(state='unavailable')

    def folder(self, path, fresh=False):
        """{'self': row, 'children': [rows, largest first], 'measured_at': time} or None. One ONTAP
        call per folder per cache period, however many people ask at once."""
        status = self.status(path)
        if status['state'] != 'on':
            return None
        key = (status['uuid'], status['relative'])
        max_age = REFRESHED_SECONDS if fresh else FOLDER_SECONDS
        with self._lock:
            flight = self._folder_locks.setdefault(key, threading.Lock())
        with flight:
            with self._lock:
                cached = self._folders.get(key)
                if cached and time.monotonic() - cached[0] < max_age:
                    self._folders.move_to_end(key)
                    return cached[1]
            result = self._read(status, path)
            with self._lock:
                self._folder_locks.pop(key, None)
                if result is not None:
                    self._folders[key] = (time.monotonic(), result)
                    self._folders.move_to_end(key)
                    while len(self._folders) > MAX_CACHED_FOLDERS:
                        self._folders.popitem(last=False)
            return result

    def _bucket_labels(self, client, uuid):
        # A listing leaves the labels out of its rows. They only change with the calendar.
        cached = self._labels.get(uuid)
        if cached and time.monotonic() - cached[0] < LABELS_SECONDS:
            return cached[1]
        page = client.first_page(
            f'/api/storage/volumes/{uuid}/files/%2F?return_metadata=true&fields={LABELS_FIELD}'
        )
        records = page.get('records') or [{}]
        labels = (
            (records[0].get('analytics') or {})
            .get('by_modified_time', {})
            .get('bytes_used', {})
            .get('labels')
        )
        self._labels[uuid] = (time.monotonic(), labels)
        return labels

    def _listing(self, client, request):
        """(every record, whether any are left out). A later page that fails leaves the folder
        partial rather than unavailable: its largest rows are already in hand."""
        deadline = time.monotonic() + MAX_READ_SECONDS
        page = client.first_page(request)
        records = list(page.get('records', []))
        for _ in range(MAX_PAGES - 1):
            following = ((page.get('_links') or {}).get('next') or {}).get('href')
            if not following:
                return records, False
            if time.monotonic() >= deadline:
                return records, True
            try:
                page = client.first_page(following)
            except Exception as e:
                self.logger.warning(
                    f'storage analytics page read failed: {type(e).__name__}'
                )
                return records, True
            records.extend(page.get('records', []))
        return records, bool((page.get('_links') or {}).get('next'))

    def _read(self, status, path):
        now = time.time()
        relative = quote(status['relative'] or '/', safe='')
        request = (
            f'/api/storage/volumes/{status["uuid"]}/files/{relative}?type=directory'
            f'&fields={ANALYTICS_FIELDS}&order_by=analytics.bytes_used%20desc'
            f'&max_records={MAX_ROWS}&return_timeout=10'
        )
        try:
            with self._reads:
                client = self._client(status['target'])
                labels = self._bucket_labels(client, status['uuid'])
                records, truncated = self._listing(client, request)
        except Exception as e:
            if refused_this_path(e):
                # Names and paths stay out of the log here too.
                status = getattr(e, 'status_code', type(e).__name__)
                self.logger.info(f'storage analytics refused one folder: {status}')
            else:
                self._failed(e)
            return None
        own = None
        children = []
        for record in records:
            name = record.get('name')
            if name == '.':
                own = to_row(path, path.rsplit('/', 1)[-1], record, labels, now)
            elif name != '..' and not record.get('is_junction'):
                children.append(to_row(f'{path}/{name}', name, record, labels, now))
        if own is None:
            return None
        return dict(
            self=own,
            children=children,
            truncated=truncated,
            measured_at=now,
        )
