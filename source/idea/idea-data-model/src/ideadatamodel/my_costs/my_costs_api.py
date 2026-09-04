__all__ = (
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

from typing import Optional, List
from pydantic import Field


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
    """one user's totals across the three sections, for the admin listing."""

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
    total_cost: Optional[float] = Field(default=None)


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


# Costs.GetUserSummary
class GetUserCostsSummaryRequest(SocaPayload):
    # admin only, and the one place a username is accepted. the self scoped
    # MyCosts.GetSummary still takes none.
    username: Optional[str] = Field(default=None)
