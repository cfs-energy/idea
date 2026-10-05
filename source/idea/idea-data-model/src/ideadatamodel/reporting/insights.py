"""Reporting insights wire contract. Decimal amounts serialize as strings."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, Field
from ideadatamodel import IdeaOpenAPISpecEntry
from .reporting_api import ReportingPeriodRequest, ReportingUsername, ReportingWireModel


class InsightModel(ReportingWireModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class InsightsPeriod(InsightModel):
    start: date
    end: date
    label: str


class Ranked(InsightModel):
    name: str
    cost: Decimal
    count: int | None = None
    share_pct: float


class JobRow(InsightModel):
    job_id: str
    name: str | None = None
    owner: str
    project: str | None = None
    queue: str | None = None
    instance_type: str | None = None
    nodes: int | None = None
    requested_cores: int | None = None
    used_cores: float | None = None
    requested_memory_gib: float | None = None
    peak_memory_gib: float | None = None
    instance_memory_gib: float | None = None
    finished_at: datetime
    elapsed_hours: float | None = None
    cost: Decimal | None = None
    cpu_efficiency_pct: float | None = None
    memory_efficiency_pct: float | None = None
    walltime_efficiency_pct: float | None = None
    wasted_core_hours: float | None = None


class DailyProjectCost(InsightModel):
    date: date
    project: str
    cost: Decimal


class DailyUserCost(InsightModel):
    date: date
    user: str
    cost: Decimal


class JobsInsights(InsightModel):
    count: int = 0
    cost: Decimal | None = None
    # no longer set: it was a hypothetical reserved-instance discount. kept so stored
    # snapshots, which carry it, still validate.
    savings: Decimal | None = None
    cpu_efficiency_pct: float | None = None
    cpu_efficiency_weighted_pct: float | None = None
    memory_efficiency_pct: float | None = None
    walltime_efficiency_pct: float | None = None
    wasted_core_hours: float | None = None
    wasted_cost: Decimal | None = None
    jobs_with_efficiency: int = 0
    by_user: list[Ranked] = Field(default_factory=list)
    by_project: list[Ranked] = Field(default_factory=list)
    by_queue: list[Ranked] = Field(default_factory=list)
    by_instance_family: list[Ranked] = Field(default_factory=list)
    daily_by_project: list[DailyProjectCost] = Field(default_factory=list)
    costliest: list[JobRow] = Field(default_factory=list)
    least_efficient: list[JobRow] = Field(default_factory=list)


class DesktopRow(InsightModel):
    idea_session_id: str
    name: str | None = None
    owner: str
    project: str | None = None
    instance_type: str | None = None
    checked_hours: float
    idle_hours: float
    idle_pct: float
    idle_cost: Decimal | None = None


class DesktopsInsights(InsightModel):
    cost: Decimal | None = None
    hours: float | None = None
    count: int = 0
    by_user: list[Ranked] = Field(default_factory=list)
    by_project: list[Ranked] = Field(default_factory=list)
    daily_top_users: list[DailyUserCost] = Field(default_factory=list)
    # from the idle stop's checks; desktops without checks are left out, never counted as in use
    desktops_with_activity: int = 0
    checked_hours: float | None = None
    idle_hours: float | None = None
    idle_cost: Decimal | None = None
    idle_by_user: list[Ranked] = Field(default_factory=list)
    idle_by_project: list[Ranked] = Field(default_factory=list)
    least_efficient: list[DesktopRow] = Field(default_factory=list)


class StorageUser(InsightModel):
    name: str
    bytes: int
    cost: Decimal | None = None


class StorageTierDaily(InsightModel):
    date: date
    tier: Literal['ssd', 'capacity_pool']
    bytes: int


class StorageInsights(InsightModel):
    cost: Decimal | None = None
    used_bytes: int | None = None
    by_user: list[StorageUser] = Field(default_factory=list)
    tier_daily: list[StorageTierDaily] = Field(default_factory=list)


class BudgetInsight(InsightModel):
    project: str
    budget_name: str
    limit: Decimal
    spent: Decimal
    forecast: Decimal | None = None
    pct_at_forecast: float | None = None
    headroom: Decimal | None = None
    status: Literal['ok', 'watch', 'over']


class ReportingInsights(InsightModel):
    period: InsightsPeriod
    currency: str
    updated_at: datetime
    jobs: JobsInsights = Field(default_factory=JobsInsights)
    desktops: DesktopsInsights = Field(default_factory=DesktopsInsights)
    storage: StorageInsights = Field(default_factory=StorageInsights)
    budgets: list[BudgetInsight] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, max_length=3)


class GetReportingInsightsRequest(ReportingPeriodRequest):
    username: ReportingUsername | None = None


GetReportingInsightsResult = ReportingInsights
GetMyCostsInsightsRequest = ReportingPeriodRequest
GetMyCostsInsightsResult = ReportingInsights

OPEN_API_SPEC_ENTRIES_REPORTING_INSIGHTS = [
    IdeaOpenAPISpecEntry(
        namespace=namespace,
        request=request,
        result=ReportingInsights,
        is_listing=False,
        is_public=False,
    )
    for namespace, request in (
        ('Reporting.GetInsights', GetReportingInsightsRequest),
        ('MyCosts.GetInsights', GetMyCostsInsightsRequest),
    )
]
