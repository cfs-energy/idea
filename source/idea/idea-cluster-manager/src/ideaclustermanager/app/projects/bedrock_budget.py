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

__all__ = (
    'BedrockBudget',
    'is_bedrock_service',
)

from ideadatamodel import (
    constants,
    Project,
    ProjectBedrockBudget,
    BEDROCK_BUDGET_ACTION_BLOCK,
    BEDROCK_BUDGET_ACTION_WARN,
    BEDROCK_BUDGET_STATUS_EXHAUSTED,
    BEDROCK_BUDGET_STATUS_OK,
    BEDROCK_BUDGET_STATUS_UNAVAILABLE,
    BEDROCK_BUDGET_STATUS_WARNING,
)
from ideasdk.context import SocaContext
from ideasdk.utils import Utils

from typing import Optional


def is_bedrock_service(service_name: str) -> bool:
    """
    a third party model bills under its own cost explorer service name, which carries
    a bedrock edition suffix rather than being 'Amazon Bedrock'.
    """
    return 'bedrock' in Utils.get_as_string(service_name, '').lower()


class BedrockBudget:
    """
    compares a project's AWS budget against its limit and publishes the verdict the
    scheduler and the virtual desktop controller read.

    AWS Budgets already counts bedrock spend: application inference profiles carry the
    project cost allocation tag, so model charges reach the budget's own actual spend
    figure, about a day after the invocation. nothing here values usage.
    """

    def __init__(self, context: SocaContext, logger=None):
        self.context = context
        self.logger = logger if logger is not None else context.logger('bedrock-budget')

    # configuration

    def _config_key(self, suffix: str) -> str:
        return f'{self.context.module_id()}.bedrock.{suffix}'

    def is_enabled(self) -> bool:
        if not self.context.config().get_bool(self._config_key('enabled'), False):
            return False
        return self.context.config().get_bool(
            self._config_key('budgets.enabled'), False
        )

    def get_action(self) -> str:
        action = Utils.get_as_string(
            self.context.config().get_string(
                self._config_key('budgets.action'), BEDROCK_BUDGET_ACTION_BLOCK
            ),
            BEDROCK_BUDGET_ACTION_BLOCK,
        )
        normalized = action.strip().lower()
        if normalized in (BEDROCK_BUDGET_ACTION_WARN, BEDROCK_BUDGET_ACTION_BLOCK):
            return normalized
        # an unreadable action is enforced, not ignored: this setting only exists
        # because an administrator turned enforcement on.
        self.logger.warning(
            f'{self._config_key("budgets.action")}: {action} is not '
            f'{BEDROCK_BUDGET_ACTION_WARN} or {BEDROCK_BUDGET_ACTION_BLOCK}. '
            f'enforcing as {BEDROCK_BUDGET_ACTION_BLOCK}.'
        )
        return BEDROCK_BUDGET_ACTION_BLOCK

    def get_warning_percent(self) -> float:
        percent = Utils.get_as_float(
            self.context.config().get_int(
                self._config_key('budgets.warning_percent'), 80
            ),
            80.0,
        )
        if percent <= 0 or percent > 100:
            return 100.0
        return percent

    # bedrock share of the project's spend

    def get_bedrock_spend_percent(self, project: Project) -> Optional[float]:
        """
        how much of the project's priced spend is bedrock, read from cost explorer
        grouped by service. None when it cannot be established, which is reported as
        absent: a share is never inferred and never stands in as zero.
        """
        try:
            aws_util = self.context.aws_util()
            spend_by_service = aws_util.cost_explorer_get_tagged_service_spend(
                tag_key=constants.IDEA_TAG_PROJECT, tag_value=project.name
            )
        except Exception as e:
            # informational only, so nothing about a failure here reaches the verdict.
            self.logger.warning(
                f'failed to read the bedrock share of spend for project '
                f'{project.project_id}: {e}'
            )
            return None

        if spend_by_service is None:
            return None
        total = sum(spend_by_service.values())
        if total <= 0:
            return None
        bedrock = sum(
            amount
            for service, amount in spend_by_service.items()
            if is_bedrock_service(service)
        )
        return round((bedrock / total) * 100, 1)

    # evaluation

    def unavailable(self, action: str, message: str) -> ProjectBedrockBudget:
        return ProjectBedrockBudget(
            action=action,
            status=BEDROCK_BUDGET_STATUS_UNAVAILABLE,
            message=message,
        )

    def evaluate(self, project: Project) -> Optional[ProjectBedrockBudget]:
        """
        None when nothing is being enforced, so a cluster that never turned this on
        behaves exactly as it did before. the caller passes a project whose budget
        already carries live actuals.
        """
        if not self.is_enabled():
            return None
        if project is None or not project.is_bedrock_enabled():
            return None

        action = self.get_action()
        if not project.is_budgets_enabled():
            # no budget is not an unknown: there is no limit to exceed, the same
            # conclusion the job and session budget checks reach.
            return ProjectBedrockBudget(
                action=action,
                status=BEDROCK_BUDGET_STATUS_OK,
                message='no project budget is configured',
            )

        budget = project.budget
        if budget is None or budget.budget_limit is None or budget.actual_spend is None:
            budget_name = None if budget is None else budget.budget_name
            return self.unavailable(action, f'budget {budget_name} could not be read')

        limit = Utils.get_as_float(budget.budget_limit.amount, 0.0)
        if limit <= 0:
            return self.unavailable(
                action,
                f'budget {budget.budget_name} carries no spend limit to compare '
                f'against',
            )

        spend = Utils.get_as_float(budget.actual_spend.amount, 0.0)
        usage_percent = round((spend / limit) * 100, 2)

        if spend > limit:
            status = BEDROCK_BUDGET_STATUS_EXHAUSTED
        elif usage_percent >= self.get_warning_percent():
            status = BEDROCK_BUDGET_STATUS_WARNING
        else:
            status = BEDROCK_BUDGET_STATUS_OK

        result = ProjectBedrockBudget(
            action=action,
            status=status,
            budget_name=budget.budget_name,
            budget_limit=budget.budget_limit,
            actual_spend=budget.actual_spend,
            usage_percent=usage_percent,
        )

        if status != BEDROCK_BUDGET_STATUS_OK:
            # only read when someone is being warned or held, which is when the share
            # is worth saying and worth a billed cost explorer request.
            bedrock_spend_percent = self.get_bedrock_spend_percent(project)
            if bedrock_spend_percent is not None:
                result.bedrock_spend_percent = bedrock_spend_percent
                result.message = (
                    f'{bedrock_spend_percent}% of the spend recorded for this project '
                    f'is Amazon Bedrock'
                )

        if status == BEDROCK_BUDGET_STATUS_EXHAUSTED:
            self.logger.warning(
                f'project {project.name} has exhausted budget {budget.budget_name}: '
                f'spend {spend}, limit {limit}. action: {action}'
            )
        return result
