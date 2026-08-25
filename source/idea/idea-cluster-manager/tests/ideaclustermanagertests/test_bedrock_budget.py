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

"""
Test Cases for bedrock budget enforcement

The evaluator runs against a recording Cost Explorer stand-in, so what it asks for is
asserted as well as the verdict it reaches. Nothing touches DynamoDB or AWS.
"""

from ideaclustermanager.app.projects.bedrock_budget import BedrockBudget
from ideaclustermanager.app.api.projects_api import ProjectsAPI
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO

from ideadatamodel import (
    constants,
    locale,
    AwsProjectBudget,
    Project,
    ProjectBedrockConfig,
    ProjectBedrockBudget,
    SocaAmount,
    BEDROCK_BUDGET_ACTION_BLOCK,
    BEDROCK_BUDGET_ACTION_WARN,
    BEDROCK_BUDGET_STATUS_EXHAUSTED,
    BEDROCK_BUDGET_STATUS_OK,
    BEDROCK_BUDGET_STATUS_UNAVAILABLE,
    BEDROCK_BUDGET_STATUS_WARNING,
)

import pytest

MODULE_ID = 'cluster-manager'
PROJECT_ID = 'a1b2c3d4-0000-4000-8000-000000000001'
PROJECT_NAME = 'research'
BUDGET_NAME = 'research-budget'

MODEL_A = 'us.vendor-a.model-1'
MODEL_B = 'vendor-b.model-9'

BEDROCK_SERVICE = 'Amazon Bedrock'
BEDROCK_EDITION_SERVICE = 'Model A (Amazon Bedrock Edition)'
EC2_SERVICE = 'Amazon Elastic Compute Cloud - Compute'

CONFIG_VALUES = {
    f'{MODULE_ID}.bedrock.enabled': True,
    f'{MODULE_ID}.bedrock.budgets.enabled': True,
    f'{MODULE_ID}.bedrock.budgets.action': BEDROCK_BUDGET_ACTION_BLOCK,
    f'{MODULE_ID}.bedrock.budgets.warning_percent': 80,
}


@pytest.fixture(autouse=True)
def initialized_locale():
    # SocaAmount reads the currency code the app context normally initializes.
    try:
        locale.get_currency_code()
    except Exception:
        locale.init('C')


class FakeConfig:
    def __init__(self, values):
        self.values = dict(values)

    def _get(self, key, default, required):
        # an entry set to None stands for a key the cluster config does not carry
        value = self.values.get(key, default)
        if value is None:
            value = default
        if required and value is None:
            raise KeyError(key)
        return value

    def get_string(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_bool(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_int(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_list(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)


class FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(('info', str(message)))

    def debug(self, message, *args, **kwargs):
        self.messages.append(('debug', str(message)))

    def warning(self, message, *args, **kwargs):
        self.messages.append(('warning', str(message)))

    def error(self, message, *args, **kwargs):
        self.messages.append(('error', str(message)))

    def exception(self, message, *args, **kwargs):
        self.messages.append(('exception', str(message)))


class StubAwsUtil:
    """
    stands in for the Cost Explorer read. records every call, so a billed request the
    evaluator did not need is visible.
    """

    def __init__(self, spend_by_service=None, raises=False):
        self.spend_by_service = spend_by_service
        self.raises = raises
        self.calls = []

    def cost_explorer_get_tagged_service_spend(self, tag_key, tag_value):
        self.calls.append((tag_key, tag_value))
        if self.raises:
            raise RuntimeError('cost explorer is not reachable')
        return self.spend_by_service


class FakeContext:
    def __init__(self, config, aws_util):
        self._config = config
        self._logger = FakeLogger()
        self._aws_util = aws_util

    def config(self):
        return self._config

    def logger(self, name=None):
        return self._logger

    def aws_util(self):
        return self._aws_util

    @staticmethod
    def module_id():
        return MODULE_ID


def build_budget(limit=100.0, actual_spend=10.0):
    return AwsProjectBudget(
        budget_name=BUDGET_NAME,
        budget_limit=None if limit is None else SocaAmount(amount=limit),
        actual_spend=None if actual_spend is None else SocaAmount(amount=actual_spend),
        forecasted_spend=SocaAmount(amount=0.0),
    )


def build_project(bedrock=True, budget=True, limit=100.0, actual_spend=10.0):
    return Project(
        project_id=PROJECT_ID,
        name=PROJECT_NAME,
        enabled=True,
        enable_budgets=budget,
        budget=build_budget(limit=limit, actual_spend=actual_spend) if budget else None,
        bedrock=ProjectBedrockConfig(enabled=True, model_ids=[MODEL_A, MODEL_B])
        if bedrock
        else None,
    )


def build_evaluator(config_values=None, spend_by_service=None, raises=False):
    config = FakeConfig({**CONFIG_VALUES, **(config_values or {})})
    aws_util = StubAwsUtil(spend_by_service=spend_by_service, raises=raises)
    context = FakeContext(config, aws_util)
    return BedrockBudget(context=context), context, aws_util


# ----------------------------------------------------------------- evaluation


def test_enforcement_is_off_unless_it_is_turned_on():
    evaluator, _, aws_util = build_evaluator(
        config_values={f'{MODULE_ID}.bedrock.budgets.enabled': None}
    )
    assert evaluator.evaluate(build_project()) is None
    assert aws_util.calls == []


def test_a_project_without_bedrock_is_not_evaluated():
    evaluator, _, aws_util = build_evaluator()
    assert evaluator.evaluate(build_project(bedrock=False)) is None
    assert aws_util.calls == []


def test_a_project_without_a_budget_has_nothing_to_exceed():
    evaluator, _, _ = build_evaluator()
    verdict = evaluator.evaluate(build_project(budget=False))
    assert verdict.status == BEDROCK_BUDGET_STATUS_OK
    assert verdict.is_blocking() is False


def test_the_budget_figure_from_aws_decides_on_its_own():
    # 96 of a 100 limit is inside the limit and past the warning threshold. nothing is
    # added to the figure AWS Budgets reports, so 96 is not an exhausted budget.
    evaluator, _, _ = build_evaluator()
    verdict = evaluator.evaluate(build_project(actual_spend=96.0))
    assert verdict.actual_spend.amount == pytest.approx(96.0)
    assert verdict.usage_percent == pytest.approx(96.0)
    assert verdict.status == BEDROCK_BUDGET_STATUS_WARNING
    assert verdict.is_blocking() is False

    verdict = evaluator.evaluate(build_project(actual_spend=100.01))
    assert verdict.status == BEDROCK_BUDGET_STATUS_EXHAUSTED
    assert verdict.is_blocking() is True


def test_an_exhausted_budget_is_logged_with_the_figures_it_was_decided_on():
    evaluator, context, _ = build_evaluator()
    evaluator.evaluate(build_project(actual_spend=110.0))
    assert any(
        'has exhausted budget' in message
        for level, message in context.logger().messages
        if level == 'warning'
    )


def test_warn_reports_without_blocking_anything():
    evaluator, _, _ = build_evaluator(
        config_values={
            f'{MODULE_ID}.bedrock.budgets.action': BEDROCK_BUDGET_ACTION_WARN
        },
    )
    verdict = evaluator.evaluate(build_project(actual_spend=110.0))
    assert verdict.status == BEDROCK_BUDGET_STATUS_EXHAUSTED
    assert verdict.action == BEDROCK_BUDGET_ACTION_WARN
    assert verdict.is_blocking() is False


def test_an_action_that_is_neither_warn_nor_block_enforces():
    evaluator, context, _ = build_evaluator(
        config_values={f'{MODULE_ID}.bedrock.budgets.action': 'observe'},
    )
    verdict = evaluator.evaluate(build_project(actual_spend=110.0))
    assert verdict.action == BEDROCK_BUDGET_ACTION_BLOCK
    assert verdict.is_blocking() is True
    assert any(
        'observe' in message
        for level, message in context.logger().messages
        if level == 'warning'
    )


def test_a_budget_that_could_not_be_read_blocks_bedrock_access():
    evaluator, _, _ = build_evaluator()
    verdict = evaluator.evaluate(build_project(actual_spend=None))
    assert verdict.status == BEDROCK_BUDGET_STATUS_UNAVAILABLE
    assert verdict.is_blocking() is True


def test_a_budget_without_a_spend_limit_blocks_bedrock_access():
    evaluator, _, _ = build_evaluator()
    verdict = evaluator.evaluate(build_project(limit=0.0))
    assert verdict.status == BEDROCK_BUDGET_STATUS_UNAVAILABLE
    assert verdict.is_blocking() is True


# ------------------------------------------------------ bedrock share of spend


def test_the_share_of_the_spend_that_was_bedrock_is_reported():
    evaluator, _, aws_util = build_evaluator(
        spend_by_service={BEDROCK_SERVICE: 30.0, EC2_SERVICE: 70.0}
    )
    verdict = evaluator.evaluate(build_project(actual_spend=110.0))
    assert verdict.bedrock_spend_percent == pytest.approx(30.0)
    assert '30.0%' in verdict.message
    # filtered to this project's cost allocation tag, not the whole cluster
    assert aws_util.calls == [(constants.IDEA_TAG_PROJECT, PROJECT_NAME)]


def test_a_model_billed_under_its_own_service_name_counts_as_bedrock():
    # a third party model bills as its own cost explorer service, not 'Amazon Bedrock'
    evaluator, _, _ = build_evaluator(
        spend_by_service={BEDROCK_EDITION_SERVICE: 25.0, EC2_SERVICE: 75.0}
    )
    verdict = evaluator.evaluate(build_project(actual_spend=110.0))
    assert verdict.bedrock_spend_percent == pytest.approx(25.0)


def test_the_share_is_not_read_while_the_project_is_inside_its_budget():
    # each cost explorer request is billed, and nobody is being told anything yet
    evaluator, _, aws_util = build_evaluator(
        spend_by_service={BEDROCK_SERVICE: 30.0, EC2_SERVICE: 70.0}
    )
    verdict = evaluator.evaluate(build_project(actual_spend=10.0))
    assert verdict.status == BEDROCK_BUDGET_STATUS_OK
    assert verdict.bedrock_spend_percent is None
    assert aws_util.calls == []


def test_a_share_that_cannot_be_established_is_absent_and_never_zero():
    evaluator, _, _ = build_evaluator(spend_by_service=None)
    verdict = evaluator.evaluate(build_project(actual_spend=110.0))
    assert verdict.bedrock_spend_percent is None
    # the verdict is unchanged by not knowing the share
    assert verdict.status == BEDROCK_BUDGET_STATUS_EXHAUSTED
    assert verdict.is_blocking() is True


def test_nothing_priced_against_the_project_tag_is_absent_not_zero():
    evaluator, _, _ = build_evaluator(spend_by_service={})
    verdict = evaluator.evaluate(build_project(actual_spend=110.0))
    assert verdict.bedrock_spend_percent is None


def test_a_share_read_that_fails_does_not_hold_up_the_verdict():
    evaluator, context, _ = build_evaluator(raises=True)
    verdict = evaluator.evaluate(build_project(actual_spend=110.0))
    assert verdict.bedrock_spend_percent is None
    assert verdict.status == BEDROCK_BUDGET_STATUS_EXHAUSTED
    assert any(
        'failed to read the bedrock share of spend' in message
        for level, message in context.logger().messages
        if level == 'warning'
    )


# -------------------------------------------------------------- api hydration


class StubProjectsService:
    def __init__(self, evaluator, raises=False):
        self.evaluator = evaluator
        self.raises = raises

    def get_project_bedrock_budget(self, project):
        if self.raises:
            raise RuntimeError('budget could not be read')
        return self.evaluator.evaluate(project)


class StubApiContext:
    def __init__(self, projects_service, logger):
        self.projects = projects_service
        self._logger = logger

    def logger(self, name=None):
        return self._logger


class StubApi:
    def __init__(self, context):
        self.context = context

    apply_bedrock_budget = ProjectsAPI.apply_bedrock_budget


def test_api_does_not_evaluate_a_project_without_bedrock():
    evaluator, context, aws_util = build_evaluator()
    api = StubApi(StubApiContext(StubProjectsService(evaluator), context.logger()))
    project = build_project(bedrock=False)
    api.apply_bedrock_budget(project)
    assert project.bedrock_budget is None
    assert aws_util.calls == []


def test_api_hydration_treats_a_failed_evaluation_as_no_room_to_spend():
    evaluator, context, _ = build_evaluator()
    api = StubApi(
        StubApiContext(StubProjectsService(evaluator, raises=True), context.logger())
    )
    project = build_project()
    api.apply_bedrock_budget(project)
    assert project.bedrock_budget.status == BEDROCK_BUDGET_STATUS_UNAVAILABLE
    assert project.bedrock_budget.is_blocking() is True
    assert any(
        'failed to evaluate the bedrock budget' in message
        for level, message in context.logger().messages
        if level == 'warning'
    )


def test_the_verdict_is_never_written_back_to_the_projects_table():
    project = build_project()
    project.bedrock_budget = ProjectBedrockBudget(
        status=BEDROCK_BUDGET_STATUS_EXHAUSTED
    )
    db_project = ProjectsDAO.convert_to_db(project)
    assert 'bedrock_budget' not in db_project
    assert 'bedrock' in db_project
