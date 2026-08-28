"""
Test Cases for ArnBuilder

verifies arns are built for the configured aws partition. IDEA supports the aws-us-gov
partition, where the arn prefix changes but the iam service linked role path does not.
"""

from ideasdk.context.arn_builder import ArnBuilder

import pytest

AWS_PARTITION_CONFIG = {
    'cluster.aws.partition': 'aws',
    'cluster.aws.account_id': '123456789012',
    'cluster.aws.region': 'us-east-1',
    'cluster.aws.dns_suffix': 'amazonaws.com',
    'cluster.cluster_name': 'idea-mock',
    'cluster.cluster_s3_bucket': 'idea-mock-cluster-us-east-1-123456789012',
}

GOVCLOUD_PARTITION_CONFIG = {
    'cluster.aws.partition': 'aws-us-gov',
    'cluster.aws.account_id': '123456789012',
    'cluster.aws.region': 'us-gov-west-1',
    'cluster.aws.dns_suffix': 'amazonaws.com',
    'cluster.cluster_name': 'idea-mock',
    'cluster.cluster_s3_bucket': 'idea-mock-cluster-us-gov-west-1-123456789012',
}


class ConfigStub:
    """
    minimal ClusterConfig stand-in. ArnBuilder only reads string values.
    """

    def __init__(self, values):
        self.values = values

    def get_string(self, key, default=None, required=False, module_id=None):
        return self.values.get(key, default)


@pytest.fixture()
def aws_arns() -> ArnBuilder:
    return ArnBuilder(config=ConfigStub(AWS_PARTITION_CONFIG))


@pytest.fixture()
def govcloud_arns() -> ArnBuilder:
    return ArnBuilder(config=ConfigStub(GOVCLOUD_PARTITION_CONFIG))


def test_arn_builder_partition_prefix(aws_arns, govcloud_arns):
    assert aws_arns.vpc_arn.startswith('arn:aws:ec2:')
    assert govcloud_arns.vpc_arn.startswith('arn:aws-us-gov:ec2:')

    assert aws_arns.get_route53_hostedzone_arn().startswith('arn:aws:route53:')
    assert govcloud_arns.get_route53_hostedzone_arn().startswith(
        'arn:aws-us-gov:route53:'
    )

    assert aws_arns.user_pool_arn.startswith('arn:aws:cognito-idp:')
    assert govcloud_arns.user_pool_arn.startswith('arn:aws-us-gov:cognito-idp:')


def test_service_role_arns_use_aws_service_role_path(aws_arns, govcloud_arns):
    # the service linked role path is 'aws-service-role' in every partition.
    # building it from the partition name yields arns that never match in govcloud.
    for arn in aws_arns.service_role_arns:
        assert arn.startswith('arn:aws:iam::123456789012:role/aws-service-role/')

    for arn in govcloud_arns.service_role_arns:
        assert arn.startswith('arn:aws-us-gov:iam::123456789012:role/aws-service-role/')
        assert 'aws-us-gov-service-role' not in arn


def test_service_role_arns_cover_expected_services(govcloud_arns):
    services = set()
    for arn in govcloud_arns.service_role_arns:
        services.add(arn.split('role/aws-service-role/')[1])

    assert services == {
        's3.data-source.lustre.fsx.amazonaws.com/*',
        'autoscaling.amazonaws.com/*',
        'spotfleet.amazonaws.com/*',
        'fsx.amazonaws.com/*',
    }


def test_ddb_application_autoscaling_service_role_arn(aws_arns, govcloud_arns):
    role_path = (
        ':role/aws-service-role/dynamodb.application-autoscaling.amazonaws.com/'
        'AWSServiceRoleForApplicationAutoScaling_DynamoDBTable'
    )
    assert (
        aws_arns.get_ddb_application_autoscaling_service_role_arn()
        == f'arn:aws:iam::123456789012{role_path}'
    )
    assert (
        govcloud_arns.get_ddb_application_autoscaling_service_role_arn()
        == f'arn:aws-us-gov:iam::123456789012{role_path}'
    )


def test_s3_arns_omit_region_and_account(aws_arns, govcloud_arns):
    for arn in aws_arns.s3_bucket_arns:
        assert arn.startswith('arn:aws:s3:::')
    for arn in govcloud_arns.s3_bucket_arns:
        assert arn.startswith('arn:aws-us-gov:s3:::')


def test_get_ssm_arn(aws_arns, govcloud_arns):
    assert aws_arns.get_ssm_arn('parameter/idea-mock').startswith('arn:aws:ssm:')
    assert govcloud_arns.get_ssm_arn('parameter/idea-mock').startswith(
        'arn:aws-us-gov:ssm:'
    )
