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

import ideascheduler
from ideadatamodel import (
    exceptions, errorcodes,
    SocaJob, SocaJobEstimatedBOMCost,
    SocaJobEstimatedBudgetUsage,
    AwsProjectBudget, Project, GetProjectRequest
)
from ideasdk.utils import Utils

from typing import Optional


class AwsBudgetsHelper:
    def __init__(self, context: ideascheduler.AppContext, job: SocaJob = None, project: Project = None):
        self._context = context
        self._logger = context.logger()

        self._job = job
        self._project = project

        if self._project is None and self._job is not None:
            if Utils.is_empty(job.project):
                return
            get_project_result = self._context.projects_client.get_project(GetProjectRequest(project_name=job.project))
            self._project = get_project_result.project

    @property
    def config(self):
        return self._context.config()

    @property
    def job_owner(self) -> str:
        return self._job.owner

    @property
    def project_name(self) -> Optional[str]:
        if self._project is not None:
            return self._project.name
        if self._job is not None:
            return self._job.project
        return None

    @property
    def budget_name(self) -> Optional[str]:
        if self._project is None:
            return None
        if not self._project.is_budgets_enabled():
            return None
        return self._project.budget.budget_name

    def get_budget(self, raise_exc=True) -> Optional[AwsProjectBudget]:
        try:
            budget_name = self.budget_name
            if Utils.is_empty(budget_name):
                return None
            return self._context.aws_util().budgets_get_budget(budget_name=self.budget_name)
        except exceptions.SocaException as e:
            if raise_exc:
                raise e
            else:
                self._logger.exception(f'failed to get budget: {e}')

    def check_budget_availability(self):
        """
        Checks if budget is available for the job to run.
        """

        budget = self.get_budget()
        if budget is None:
            return

        if budget.actual_spend > budget.budget_limit:
            raise exceptions.SocaException(
                error_code=errorcodes.BUDGETS_LIMIT_EXCEEDED,
                message=f'Project: ({self._job.project}) has exceeded the allocated budget limit. '
                        f'Please update the limit on AWS Budget Console or try again later.',
                ref=budget
            )

    def compute_budget_usage(self, bom_cost: Optional[SocaJobEstimatedBOMCost] = None) -> Optional[SocaJobEstimatedBudgetUsage]:

        budget_name = self.budget_name
        if Utils.is_empty(budget_name):
            return None

        if bom_cost is None:
            bom_cost = self._job.estimated_bom_cost

        if bom_cost is None:
            return None

        budget = self.get_budget()

        job_usage_percent = round((bom_cost.line_items_total.amount / budget.budget_limit.amount) * 100, 2)
        job_usage_percent_with_savings = round((bom_cost.total.amount / budget.budget_limit.amount) * 100, 2)

        return SocaJobEstimatedBudgetUsage(
            budget_name=budget_name,
            budget_limit=budget.budget_limit,
            actual_spend=budget.actual_spend,
            forecasted_spend=budget.forecasted_spend,
            job_usage_percent=job_usage_percent,
            job_usage_percent_with_savings=job_usage_percent_with_savings
        )
