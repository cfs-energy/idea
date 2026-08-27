"""
Test Cases for Bedrock spend in the project budget gate

Bedrock spend is already in the AWS Budgets figure the gate compares against, so the
Bedrock verdict decides nothing here and only names the share of spend that was models.
These tests drive the real AwsBudgetsHelper against a stubbed AWS Budgets read.
"""

from ideadatamodel import (
    exceptions,
    errorcodes,
    AwsProjectBudget,
    Project,
    ProjectBedrockBudget,
    ProjectBedrockConfig,
    SocaAmount,
    SocaJob,
    SocaJobParams,
    BEDROCK_BUDGET_ACTION_BLOCK,
    BEDROCK_BUDGET_STATUS_EXHAUSTED,
    BEDROCK_BUDGET_STATUS_UNAVAILABLE,
)
from ideasdk.aws import AWSUtil
from ideascheduler.app.aws import AwsBudgetsHelper

import pytest

BUDGET_NAME = 'research-budget'


def build_job() -> SocaJob:
    return SocaJob(
        name='mock-job',
        job_id='1',
        job_uid='mock-job-uid',
        owner='mockuser',
        project='research',
        params=SocaJobParams(),
    )


def build_project(bedrock_budget=None) -> Project:
    return Project(
        project_id='a1b2c3d4-0000-4000-8000-000000000001',
        name='research',
        enabled=True,
        enable_budgets=True,
        budget=AwsProjectBudget(budget_name=BUDGET_NAME),
        bedrock=ProjectBedrockConfig(enabled=True, model_ids=['vendor.model']),
        bedrock_budget=bedrock_budget,
    )


def bedrock_budget(
    bedrock_spend_percent: float = None,
    action: str = BEDROCK_BUDGET_ACTION_BLOCK,
    status: str = BEDROCK_BUDGET_STATUS_EXHAUSTED,
) -> ProjectBedrockBudget:
    return ProjectBedrockBudget(
        action=action,
        status=status,
        budget_name=BUDGET_NAME,
        bedrock_spend_percent=bedrock_spend_percent,
    )


def build_helper(context, monkeypatch, actual_spend: float, bedrock_budget=None):
    budget = AwsProjectBudget(
        budget_name=BUDGET_NAME,
        budget_limit=SocaAmount(amount=100.0),
        actual_spend=SocaAmount(amount=actual_spend),
        forecasted_spend=SocaAmount(amount=actual_spend),
    )
    monkeypatch.setattr(
        AWSUtil, 'budgets_get_budget', lambda _self, budget_name: budget
    )
    return AwsBudgetsHelper(
        context=context,
        job=build_job(),
        project=build_project(bedrock_budget=bedrock_budget),
    )


def test_a_project_without_bedrock_enforcement_passes_the_gate_unchanged(
    context, monkeypatch
):
    helper = build_helper(context, monkeypatch, actual_spend=90.0)
    assert helper.get_bedrock_spend_percent() is None
    helper.check_budget_availability()


def test_spend_over_the_limit_is_rejected(context, monkeypatch):
    helper = build_helper(context, monkeypatch, actual_spend=101.0)
    with pytest.raises(exceptions.SocaException) as exc_info:
        helper.check_budget_availability()
    assert exc_info.value.error_code == errorcodes.BUDGETS_LIMIT_EXCEEDED


def test_a_held_job_is_told_what_share_of_the_spend_was_bedrock(context, monkeypatch):
    helper = build_helper(
        context,
        monkeypatch,
        actual_spend=101.0,
        bedrock_budget=bedrock_budget(bedrock_spend_percent=42.5),
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        helper.check_budget_availability()
    assert '42.5% of the spend recorded for this project' in exc_info.value.message


def test_a_share_that_could_not_be_established_is_left_out_of_the_reason(
    context, monkeypatch
):
    # absent, never a zero: a share nobody could read must not be reported as none spent
    helper = build_helper(
        context,
        monkeypatch,
        actual_spend=101.0,
        bedrock_budget=bedrock_budget(bedrock_spend_percent=None),
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        helper.check_budget_availability()
    assert '%' not in exc_info.value.message


def test_an_exhausted_bedrock_verdict_never_holds_a_job_on_its_own(
    context, monkeypatch
):
    # the budget the gate reads already counts bedrock spend. the verdict withholds
    # model access; it is not a second limit that can hold a job inside its budget.
    helper = build_helper(
        context,
        monkeypatch,
        actual_spend=95.0,
        bedrock_budget=bedrock_budget(bedrock_spend_percent=60.0),
    )
    helper.check_budget_availability()


def test_a_bedrock_budget_that_could_not_be_evaluated_never_holds_a_job(
    context, monkeypatch
):
    # one failed read must not stop every user's work: an unavailable verdict
    # withholds model access, it does not invent spend for the job gate.
    helper = build_helper(
        context,
        monkeypatch,
        actual_spend=95.0,
        bedrock_budget=ProjectBedrockBudget(
            action=BEDROCK_BUDGET_ACTION_BLOCK,
            status=BEDROCK_BUDGET_STATUS_UNAVAILABLE,
        ),
    )
    assert helper.get_bedrock_spend_percent() is None
    helper.check_budget_availability()
