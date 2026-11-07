"""Requests and results of the scratch cleanup admin page. No request carries a path."""

__all__ = (
    'ScratchCleanupFileSystem',
    'ScratchCleanupSettings',
    'ScratchCleanupProgress',
    'ScratchCleanupRun',
    'ScratchCleanupRow',
    'ScratchCleanupPreviewSummary',
    'GetScratchCleanupStatusRequest',
    'GetScratchCleanupStatusResult',
    'PreviewScratchCleanupRequest',
    'PreviewScratchCleanupResult',
    'ListScratchCleanupPreviewRequest',
    'ListScratchCleanupPreviewResult',
    'BackfillScratchCleanupRequest',
    'BackfillScratchCleanupResult',
    'ListScratchCleanupUpcomingRequest',
    'ListScratchCleanupUpcomingResult',
    'ListScratchCleanupAuditRequest',
    'ListScratchCleanupAuditResult',
)

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import Field

from ideadatamodel import SocaListingPayload, SocaPayload


class ScratchCleanupFileSystem(SocaPayload):
    name: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    mount_dir: Optional[str] = Field(default=None)
    selected: Optional[bool] = Field(default=None)
    # why nothing can be decided on this file system; empty when it is usable
    problem: Optional[str] = Field(default=None)
    capacity_bytes: Optional[int] = Field(default=None)
    used_bytes: Optional[int] = Field(default=None)
    used_percent: Optional[float] = Field(default=None)
    usage_read_at: Optional[datetime] = Field(default=None)


class ScratchCleanupSettings(SocaPayload):
    enabled: Optional[bool] = Field(default=None)
    dry_run: Optional[bool] = Field(default=None)
    kill_switch: Optional[bool] = Field(default=None)
    file_systems: Optional[List[str]] = Field(default=None)
    success_retention_days: Optional[int] = Field(default=None)
    failed_retention_days: Optional[int] = Field(default=None)
    max_folders_per_run: Optional[int] = Field(default=None)
    max_run_bytes_percent: Optional[float] = Field(default=None)
    max_run_share_percent: Optional[float] = Field(default=None)
    digest_weekday: Optional[int] = Field(default=None)
    problem: Optional[str] = Field(default=None)


class ScratchCleanupProgress(SocaPayload):
    # preview | run | backfill
    operation: Optional[str] = Field(default=None)
    file_system: Optional[str] = Field(default=None)
    phase: Optional[str] = Field(default=None)
    done: Optional[int] = Field(default=None)
    total: Optional[int] = Field(default=None)
    current: Optional[str] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)


class ScratchCleanupRun(SocaPayload):
    run_id: Optional[str] = Field(default=None)
    file_system: Optional[str] = Field(default=None)
    # schedule | preview | backfill
    trigger: Optional[str] = Field(default=None)
    requested_by: Optional[str] = Field(default=None)
    # running | finished | nothing_to_do | dry_run | stopped | failed | interrupted
    status: Optional[str] = Field(default=None)
    dry_run: Optional[bool] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    removed_folders: Optional[int] = Field(default=None)
    removed_bytes: Optional[int] = Field(default=None)
    would_remove_folders: Optional[int] = Field(default=None)
    noticed_folders: Optional[int] = Field(default=None)
    scheduled_folders: Optional[int] = Field(default=None)
    skipped_folders: Optional[int] = Field(default=None)
    partial_folders: Optional[int] = Field(default=None)
    kept_folders: Optional[int] = Field(default=None)
    used_percent_before: Optional[float] = Field(default=None)
    used_percent_after: Optional[float] = Field(default=None)
    message: Optional[str] = Field(default=None)


class ScratchCleanupRow(SocaPayload):
    file_system: Optional[str] = Field(default=None)
    path: Optional[str] = Field(default=None)
    owner: Optional[str] = Field(default=None)
    job_id: Optional[str] = Field(default=None)
    job_uid: Optional[str] = Field(default=None)
    job_end_time: Optional[datetime] = Field(default=None)
    size_bytes: Optional[int] = Field(default=None)
    entries: Optional[int] = Field(default=None)
    # preview rows: schedule | scheduled | keep; upcoming rows: scheduled; audit rows: started | removed | failed | interrupted | failed_gone
    action: Optional[str] = Field(default=None)
    reason: Optional[str] = Field(default=None)
    reason_text: Optional[str] = Field(default=None)
    remove_after: Optional[datetime] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    run_id: Optional[str] = Field(default=None)
    requested_by: Optional[str] = Field(default=None)
    removed_entries: Optional[int] = Field(default=None)
    # succeeded | failed
    outcome: Optional[str] = Field(default=None)
    first_notice_at: Optional[datetime] = Field(default=None)
    final_notice_at: Optional[datetime] = Field(default=None)


class ScratchCleanupPreviewSummary(SocaPayload):
    preview_id: Optional[str] = Field(default=None)
    file_system: Optional[str] = Field(default=None)
    created_at: Optional[datetime] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)
    action_counts: Optional[Dict[str, int]] = Field(default=None)
    action_bytes: Optional[Dict[str, int]] = Field(default=None)
    reason_counts: Optional[Dict[str, int]] = Field(default=None)
    reason_text: Optional[Dict[str, str]] = Field(default=None)
    # the exact text an admin types to run this preview; empty when there is nothing to run
    confirm_phrase: Optional[str] = Field(default=None)
    # a problem the run would stop on, shown before anyone tries
    problem: Optional[str] = Field(default=None)
    truncated_rows: Optional[int] = Field(default=None)
    # kept items that are not job folders: counted, and their names not shown
    other_kept: Optional[int] = Field(default=None)


class GetScratchCleanupStatusRequest(SocaPayload):
    pass


class GetScratchCleanupStatusResult(SocaPayload):
    available: Optional[bool] = Field(default=None)
    message: Optional[str] = Field(default=None)
    file_systems: Optional[List[ScratchCleanupFileSystem]] = Field(default=None)
    settings: Optional[ScratchCleanupSettings] = Field(default=None)
    progress: Optional[ScratchCleanupProgress] = Field(default=None)
    previews: Optional[List[ScratchCleanupPreviewSummary]] = Field(default=None)
    runs: Optional[List[ScratchCleanupRun]] = Field(default=None)
    failed_folders: Optional[int] = Field(default=None)
    scheduled_folders: Optional[int] = Field(default=None)


class PreviewScratchCleanupRequest(SocaPayload):
    file_system: Optional[str] = Field(default=None)


class PreviewScratchCleanupResult(SocaPayload):
    started: Optional[bool] = Field(default=None)


class ListScratchCleanupPreviewRequest(SocaListingPayload):
    preview_id: Optional[str] = Field(default=None)
    action: Optional[str] = Field(default=None)


class ListScratchCleanupPreviewResult(SocaListingPayload):
    listing: Optional[List[ScratchCleanupRow]] = Field(default=None)


class ListScratchCleanupUpcomingRequest(SocaListingPayload):
    pass


class ListScratchCleanupUpcomingResult(SocaListingPayload):
    listing: Optional[List[ScratchCleanupRow]] = Field(default=None)
    # opaque; pass back as cursor for the next page
    next_token: Optional[str] = Field(default=None)


class BackfillScratchCleanupRequest(SocaPayload):
    preview_id: Optional[str] = Field(default=None)
    confirm_phrase: Optional[str] = Field(default=None)


class BackfillScratchCleanupResult(SocaPayload):
    run_id: Optional[str] = Field(default=None)


class ListScratchCleanupAuditRequest(SocaListingPayload):
    pass


class ListScratchCleanupAuditResult(SocaListingPayload):
    listing: Optional[List[ScratchCleanupRow]] = Field(default=None)
    # opaque; pass back as cursor for the next page
    next_token: Optional[str] = Field(default=None)
