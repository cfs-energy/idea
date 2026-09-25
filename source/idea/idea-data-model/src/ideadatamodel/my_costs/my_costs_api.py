__all__ = (
    'MyCostsAmount',
    'MyCostsDaily',
    'MyCostsCoverage',
    'MyCostsStorageShare',
    'MyCostsDisk',
    'MyCostsMonth',
    'GetMyCostsResult',
    'GetCostTickerRequest',
    'GetCostTickerResult',
    'MyCostsAiModel',
    'MyCostsAiProject',
    'MyCostsAi',
    'MyCostsJob',
    'MyCostsJobGroup',
    'MyCostsJobs',
    'MyCostsDesktopSession',
    'MyCostsDesktops',
    'GetMyCostsSummaryRequest',
    'GetMyCostsSummaryResult',
    'UserCosts',
    'ListUserCostsRequest',
    'ListUserCostsResult',
    'GetUserCostsSummaryRequest',
)

from ideadatamodel import SocaPayload, SocaBaseModel

from typing import Optional, List, Literal
from pydantic import Field, model_serializer


class MyCostsAiModel(SocaBaseModel):
    model_id: Optional[str] = Field(default=None)
    invocations: Optional[int] = Field(default=None)
    input_tokens: Optional[int] = Field(default=None)
    output_tokens: Optional[int] = Field(default=None)
    total_tokens: Optional[int] = Field(default=None)
    # the caller's share of the project's bedrock spend over the window, by token
    # share. always an apportionment, never a measured per model charge.
    cost: Optional[float] = Field(default=None)
    estimated: Optional[bool] = Field(default=None)


class MyCostsAiProject(SocaBaseModel):
    project_id: Optional[str] = Field(default=None)
    project_name: Optional[str] = Field(default=None)
    project_title: Optional[str] = Field(default=None)
    invocations: Optional[int] = Field(default=None)
    input_tokens: Optional[int] = Field(default=None)
    output_tokens: Optional[int] = Field(default=None)
    total_tokens: Optional[int] = Field(default=None)
    cost: Optional[float] = Field(default=None)
    estimated: Optional[bool] = Field(default=None)
    # the project spend the apportionment needs could not be read. tokens still count.
    cost_unavailable: Optional[bool] = Field(default=None)
    by_model: Optional[List[MyCostsAiModel]] = Field(default=None)


class MyCostsAi(SocaBaseModel):
    invocations: Optional[int] = Field(default=None)
    total_tokens: Optional[int] = Field(default=None)
    cost: Optional[float] = Field(default=None)
    estimated: Optional[bool] = Field(default=None)
    projects: Optional[List[MyCostsAiProject]] = Field(default=None)
    is_unavailable: Optional[bool] = Field(default=None)


class MyCostsJob(SocaBaseModel):
    job_id: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    queue: Optional[str] = Field(default=None)
    project: Optional[str] = Field(default=None)
    end_time: Optional[str] = Field(default=None)
    cost: Optional[float] = Field(default=None)
    # the scheduler could not price the instance hours, so the estimate omits the compute.
    # read this flag, never a zero amount: a job cancelled before it ran did cost nothing.
    cost_unavailable: Optional[bool] = Field(default=None)


class MyCostsJobGroup(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    job_count: Optional[int] = Field(default=None)
    cost: Optional[float] = Field(default=None)


class MyCostsJobs(SocaBaseModel):
    job_count: Optional[int] = Field(default=None)
    cost: Optional[float] = Field(default=None)
    # jobs in the window the scheduler recorded no cost estimate for. cost is the
    # subtotal of the rest, so a caller can tell a real zero from an incomplete one.
    unpriced_jobs: Optional[int] = Field(default=None)
    # at least one job in the window has an estimate that omits its instance hours, so
    # the total below is short by an unknown amount and must not be shown as spend.
    cost_unavailable: Optional[bool] = Field(default=None)
    estimated: Optional[bool] = Field(default=None)
    by_project: Optional[List[MyCostsJobGroup]] = Field(default=None)
    by_queue: Optional[List[MyCostsJobGroup]] = Field(default=None)
    recent_jobs: Optional[List[MyCostsJob]] = Field(default=None)
    is_unavailable: Optional[bool] = Field(default=None)


class MyCostsDesktopSession(SocaBaseModel):
    idea_session_id: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    instance_type: Optional[str] = Field(default=None)
    base_os: Optional[str] = Field(default=None)
    state: Optional[str] = Field(default=None)
    started_on: Optional[str] = Field(default=None)
    ended_on: Optional[str] = Field(default=None)
    hours: Optional[float] = Field(default=None)
    cost: Optional[float] = Field(default=None)
    estimated: Optional[bool] = Field(default=None)
    # the session predates the recorded stop time, so its hours are inferred from the
    # last write to the record and are an upper bound, not a measurement.
    stop_time_estimated: Optional[bool] = Field(default=None)
    # no on-demand price for the instance type, so hours stand alone.
    price_unavailable: Optional[bool] = Field(default=None)


class MyCostsDesktops(SocaBaseModel):
    session_count: Optional[int] = Field(default=None)
    hours: Optional[float] = Field(default=None)
    cost: Optional[float] = Field(default=None)
    # sessions whose instance type had no on-demand price. cost is the subtotal of the
    # priced ones; when this equals session_count there is no cost to report at all.
    unpriced_sessions: Optional[int] = Field(default=None)
    estimated: Optional[bool] = Field(default=None)
    sessions: Optional[List[MyCostsDesktopSession]] = Field(default=None)
    is_unavailable: Optional[bool] = Field(default=None)


# MyCosts.GetSummary
class GetMyCostsSummaryRequest(SocaPayload):
    # deliberately empty: the summary is always the caller's own, and a username
    # parameter would be a way to ask for someone else's.
    pass


class GetMyCostsSummaryResult(SocaPayload):
    username: Optional[str] = Field(default=None)
    window: Optional[str] = Field(default=None)
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    currency: Optional[str] = Field(default=None)
    ai: Optional[MyCostsAi] = Field(default=None)
    jobs: Optional[MyCostsJobs] = Field(default=None)
    desktops: Optional[MyCostsDesktops] = Field(default=None)


class UserCosts(SocaBaseModel):
    """one user's totals for the admin listing."""

    username: Optional[str] = Field(default=None)
    ai_requests: Optional[int] = Field(default=None)
    ai_tokens: Optional[int] = Field(default=None)
    ai_cost: Optional[float] = Field(default=None)
    ai_cost_unavailable: Optional[bool] = Field(default=None)
    desktop_session_count: Optional[int] = Field(default=None)
    desktop_hours: Optional[float] = Field(default=None)
    desktop_cost: Optional[float] = Field(default=None)
    desktop_unpriced_sessions: Optional[int] = Field(default=None)
    job_count: Optional[int] = Field(default=None)
    job_cost: Optional[float] = Field(default=None)
    job_unpriced_jobs: Optional[int] = Field(default=None)
    job_cost_unavailable: Optional[bool] = Field(default=None)
    storage_cost: Optional[float] = Field(default=None)
    storage_gb: Optional[float] = Field(default=None)
    storage_cost_period: Optional[str] = Field(default=None)
    total_cost: Optional[float] = Field(default=None)
    total_cost_excludes_storage: bool = Field(default=False)


# Costs.ListUserCosts
class ListUserCostsRequest(SocaPayload):
    # no filters: the window is fixed, the listing is every user with a measured cost in
    # it, and the result is not paged.
    pass


class ListUserCostsResult(SocaPayload):
    window: Optional[str] = Field(default=None)
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    currency: Optional[str] = Field(default=None)
    listing: Optional[List[UserCosts]] = Field(default=None)
    ai_unavailable: Optional[bool] = Field(default=None)
    jobs_unavailable: Optional[bool] = Field(default=None)
    desktops_unavailable: Optional[bool] = Field(default=None)
    storage_unavailable: Optional[bool] = Field(default=None)
    storage_disabled: bool = Field(default=False)
    storage_configuration_status: Optional[
        Literal['disabled', 'not_configured', 'unsupported', 'enabled']
    ] = Field(default=None)
    storage_configuration_reason: Optional[str] = Field(default=None)
    storage_metrics_provider: Optional[str] = Field(default=None)
    storage_has_efs: Optional[bool] = Field(default=None)
    storage_data_available: Optional[bool] = Field(default=None)


# Costs.GetUserSummary
class GetUserCostsSummaryRequest(SocaPayload):
    # admin only, and the one place a username is accepted. the self scoped
    # MyCosts.GetSummary still takes none.
    username: Optional[str] = Field(default=None)


class MyCostsDaily(SocaBaseModel):
    date: str
    day: int
    amount: Optional[float] = Field(default=None)
    status: str

    @model_serializer(mode='wrap')
    def serialize_daily(self, handler):
        result = handler(self)
        # API payloads otherwise omit None fields. A missing day is explicitly null.
        result['amount'] = self.amount
        return result


class MyCostsCoverage(SocaBaseModel):
    known_days: int = Field(default=0)
    missing_days: int = Field(default=0)
    missing_prices: int = Field(default=0)
    inferred_intervals: int = Field(default=0)


class MyCostsAmount(SocaBaseModel):
    cost: Optional[float] = Field(default=None)
    status: str = Field(default='unavailable')
    note: str = Field(default='')
    amount: Optional[float] = Field(default=None)
    reason: str = Field(default='')
    coverage: Optional[MyCostsCoverage] = Field(default=None)
    source_as_of: Optional[str] = Field(default=None)
    daily: List[MyCostsDaily] = Field(default_factory=list)


class MyCostsStorageShare(MyCostsAmount):
    filesystem: str
    used_bytes: Optional[int] = Field(default=None)
    share: Optional[float] = Field(default=None)
    measured_at: Optional[float] = Field(default=None)


class MyCostsDisk(MyCostsAmount):
    volume_id: str
    desktop: str
    state: str
    size_gb: int
    volume_type: str
    gb_month_rate: Optional[float] = Field(default=None)


class MyCostsMonth(SocaBaseModel):
    start_date: str
    end_date: str
    total: Optional[float] = Field(default=None)
    incomplete: bool = Field(default=True)
    jobs: MyCostsAmount
    desktops: MyCostsAmount
    desktop_disks: MyCostsAmount
    shared_storage: MyCostsAmount
    ai: MyCostsAmount
    disks: List[MyCostsDisk] = Field(default_factory=list)
    storage: List[MyCostsStorageShare] = Field(default_factory=list)
    details: Optional[GetMyCostsSummaryResult] = Field(default=None)


# MyCosts.GetCosts: identity comes only from the authenticated invocation.
class GetMyCostsResult(SocaPayload):
    currency: str
    state: str
    refreshed_at: Optional[str] = Field(default=None)
    generation: Optional[str] = Field(default=None)
    timezone: Optional[str] = Field(default=None)
    expected_ready_at: Optional[str] = Field(default=None)
    collecting_delayed: bool = Field(default=False)
    collecting_reason: Optional[str] = Field(default=None)
    refresh_pending: bool = Field(default=False)
    refresh_acknowledged: bool = Field(default=False)
    current: Optional[MyCostsMonth] = Field(default=None)
    previous: Optional[MyCostsMonth] = Field(default=None)


class GetCostTickerRequest(SocaPayload):
    pass


class GetCostTickerResult(SocaPayload):
    enabled: bool = Field(default=False)
    period: Optional[str] = Field(default=None)
    total: Optional[float] = Field(default=None)
    currency: Optional[str] = Field(default=None)
    as_of: Optional[str] = Field(default=None)
    incomplete: Optional[bool] = Field(default=None)
