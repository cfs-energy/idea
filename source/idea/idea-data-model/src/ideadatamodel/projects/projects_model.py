#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

from ideadatamodel import SocaBaseModel, SocaKeyValue, AwsProjectBudget, SocaAmount
from ideadatamodel.model_utils import ModelUtils

from typing import Optional, List, Dict
from datetime import datetime
from pydantic import Field

# what the last bedrock budget evaluation found. unavailable means the evaluation
# could not be made, and is deliberately not a synonym for ok.
BEDROCK_BUDGET_STATUS_OK = 'ok'
BEDROCK_BUDGET_STATUS_WARNING = 'warning'
BEDROCK_BUDGET_STATUS_EXHAUSTED = 'exhausted'
BEDROCK_BUDGET_STATUS_UNAVAILABLE = 'unavailable'

# what the cluster does about it. warn only reports.
BEDROCK_BUDGET_ACTION_WARN = 'warn'
BEDROCK_BUDGET_ACTION_BLOCK = 'block'


class ProjectBedrockConfig(SocaBaseModel):
    enabled: Optional[bool] = Field(default=None)
    model_ids: Optional[List[str]] = Field(default=None)
    # written by the provisioner, not api callers: role_arn/instance_profile_arn are the
    # project's runtime identity, inference_profile_arns maps each model id to its profile.
    role_arn: Optional[str] = Field(default=None)
    instance_profile_arn: Optional[str] = Field(default=None)
    inference_profile_arns: Optional[Dict[str, str]] = Field(default=None)
    # model id -> why that model got no access on the last reconcile. absent when
    # every requested model was provisioned.
    model_errors: Optional[Dict[str, str]] = Field(default=None)
    # policy arn -> why an admin-supplied policy isn't on the project role, or couldn't be
    # checked against the permissions boundary; absent when every one of them is attached.
    policy_errors: Optional[Dict[str, str]] = Field(default=None)
    # the last reconcile failure: the error code and the call that raised it. cleared by
    # the next reconcile that completes, so broken provisioning does not read as disabled.
    reconcile_error: Optional[str] = Field(default=None)
    reconcile_error_on: Optional[datetime] = Field(default=None)

    def is_enabled(self) -> bool:
        return ModelUtils.get_as_bool(self.enabled, False)

    def get_model_ids(self) -> List[str]:
        if self.model_ids is None:
            return []
        return self.model_ids

    def get_model_errors(self) -> Dict[str, str]:
        if self.model_errors is None:
            return {}
        return self.model_errors


class BedrockUserUsage(SocaBaseModel):
    username: Optional[str] = Field(default=None)
    invocations: Optional[int] = Field(default=None)
    input_tokens: Optional[int] = Field(default=None)
    output_tokens: Optional[int] = Field(default=None)
    total_tokens: Optional[int] = Field(default=None)
    # the model this user spent the most tokens on in the window.
    top_model_id: Optional[str] = Field(default=None)
    # this user's share of the project's window spend, apportioned by token share: cost
    # explorer prices a project tag, never a caller, so spend_is_estimated is always set.
    spend: Optional[SocaAmount] = Field(default=None)
    spend_is_estimated: Optional[bool] = Field(default=None)


class BedrockModelUsage(SocaBaseModel):
    model_id: Optional[str] = Field(default=None)
    invocations: Optional[int] = Field(default=None)
    input_tokens: Optional[int] = Field(default=None)
    output_tokens: Optional[int] = Field(default=None)
    total_tokens: Optional[int] = Field(default=None)
    # apportioned by token share the same way, and estimated for the same reason.
    spend: Optional[SocaAmount] = Field(default=None)
    spend_is_estimated: Optional[bool] = Field(default=None)


class ProjectBedrockUsage(SocaBaseModel):
    # aggregated from bedrock invocation logs, not billing data, so it's recorded as it
    # happens; the priced equivalent reaches cost allocation about a day later.
    # exactly one of window and period is set: window names a trailing window, e.g.
    # last_30_days, period a calendar month. the reader is never left to guess which.
    window: Optional[str] = Field(default=None)
    period: Optional[str] = Field(default=None)
    username: Optional[str] = Field(default=None)
    invocations: Optional[int] = Field(default=None)
    input_tokens: Optional[int] = Field(default=None)
    output_tokens: Optional[int] = Field(default=None)
    total_tokens: Optional[int] = Field(default=None)
    by_user: Optional[List[BedrockUserUsage]] = Field(default=None)
    by_model: Optional[List[BedrockModelUsage]] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)
    # set when the usage read failed, so a read error is not rendered as zero usage.
    is_unavailable: Optional[bool] = Field(default=None)
    # bedrock cost from cost explorer over the same window as the token counts above,
    # which it trails by about a day. spend_is_unavailable means no answer, not no spend.
    spend: Optional[SocaAmount] = Field(default=None)
    spend_is_unavailable: Optional[bool] = Field(default=None)


class ProjectBedrockBudget(SocaBaseModel):
    # verdict on the project's AWS budget. actual_spend is the budget's total spend, not
    # bedrock-only; bedrock_spend_percent is the model share, absent when that can't be established.
    action: Optional[str] = Field(default=None)
    status: Optional[str] = Field(default=None)
    budget_name: Optional[str] = Field(default=None)
    budget_limit: Optional[SocaAmount] = Field(default=None)
    actual_spend: Optional[SocaAmount] = Field(default=None)
    usage_percent: Optional[float] = Field(default=None)
    bedrock_spend_percent: Optional[float] = Field(default=None)
    message: Optional[str] = Field(default=None)

    def is_blocking(self) -> bool:
        """
        whether bedrock access should be withheld. an evaluation that could not be
        made blocks like an exhausted one: it must not read as room to spend.
        """
        if self.action == BEDROCK_BUDGET_ACTION_WARN:
            return False
        return self.status in (
            BEDROCK_BUDGET_STATUS_EXHAUSTED,
            BEDROCK_BUDGET_STATUS_UNAVAILABLE,
        )


class Project(SocaBaseModel):
    project_id: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    ldap_groups: Optional[List[str]] = Field(default=None)
    enable_budgets: Optional[bool] = Field(default=None)
    budget: Optional[AwsProjectBudget] = Field(default=None)
    bedrock: Optional[ProjectBedrockConfig] = Field(default=None)
    # hydrated on read by the projects api, never persisted.
    bedrock_usage: Optional[ProjectBedrockUsage] = Field(default=None)
    bedrock_budget: Optional[ProjectBedrockBudget] = Field(default=None)
    tags: Optional[List[SocaKeyValue]] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)

    def is_enabled(self) -> bool:
        return ModelUtils.get_as_bool(self.enabled, False)

    def is_budgets_enabled(self) -> bool:
        if ModelUtils.get_as_bool(self.enable_budgets, False):
            if self.budget is None:
                return False
            return ModelUtils.is_not_empty(self.budget.budget_name)
        return False

    def is_bedrock_enabled(self) -> bool:
        if self.bedrock is None:
            return False
        return self.bedrock.is_enabled()
