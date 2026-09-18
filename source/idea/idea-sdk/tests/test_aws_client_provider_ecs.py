"""The cluster-manager verifies a bastion task's identity with the ECS API before issuing its
directory join; the client provider must know the service, or the first live join times out."""

from ideasdk.aws.aws_client_provider import AWS_CLIENT_ECS, SUPPORTED_CLIENTS


def test_ecs_is_a_supported_client():
    assert AWS_CLIENT_ECS == 'ecs'
    assert AWS_CLIENT_ECS in SUPPORTED_CLIENTS
