import pytest

from ideadatamodel import exceptions
from ideascheduler.app.api.scheduler_api import SchedulerAPI
from ideatestutils.api_tokens import api_token_environment, api_token_invocation


def test_personal_token_lists_jobs_with_current_module_access():
    env = api_token_environment('scheduler')
    env.context.job_cache.list_jobs.return_value = []
    env.context.job_cache.get_count.return_value = 0
    api = SchedulerAPI(env.context)
    invocation = api_token_invocation(env, 'Scheduler.ListActiveJobs')
    api.invoke(invocation)
    assert invocation.response_payload['listing'] == []
    assert env.context.job_cache.list_jobs.call_args.kwargs['owner'] == 'user-a'
    assert any(
        call.args[0].endswith('.cluster-manager.api-tokens')
        for call in env.context.aws().dynamodb_table().Table.call_args_list
    )
    env.groups.return_value = {'Groups': []}
    with pytest.raises(exceptions.SocaException):
        api.invoke(api_token_invocation(env, 'Scheduler.ListActiveJobs'))
