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

__all__ = ('BedrockProvisioner',)

from ideadatamodel import exceptions, constants, Project
from ideasdk.context import SocaContext, ArnBuilder
from ideasdk.utils import Utils

from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO

import botocore.exceptions
import fnmatch
import hashlib
import json
import re
from typing import Dict, List, Optional, Set

BEDROCK_INVOKE_ACTIONS = [
    'bedrock:InvokeModel',
    'bedrock:InvokeModelWithResponseStream',
]

# a client has to find the profile before it can invoke it, and every sdk and cli does that by
# listing. the project role boundary already allows both of these.
BEDROCK_READ_ACTIONS = [
    'bedrock:GetInferenceProfile',
]
BEDROCK_LIST_ACTIONS = [
    'bedrock:ListInferenceProfiles',
]

# geographic prefixes identify a system inference profile id, mapped to the aws
# region prefixes that geography routes within. an id without one of these
# prefixes is a base foundation model id.
SYSTEM_PROFILE_REGION_PREFIXES = {
    'apac.': ('ap-',),
    'au.': ('ap-southeast-',),
    'ca.': ('ca-',),
    'eu.': ('eu-',),
    'jp.': ('ap-northeast-',),
    'us.': ('us-',),
    'us-gov.': ('us-gov-',),
}

GOV_PARTITION = 'aws-us-gov'
GOV_PROFILE_PREFIX = 'us-gov.'
GLOBAL_PROFILE_PREFIX = 'global.'
GOV_REGION_PREFIX = 'us-gov-'
US_PROFILE_PREFIX = 'us.'
ON_DEMAND_INFERENCE_TYPE = 'ON_DEMAND'

SYSTEM_PROFILE_PREFIXES = tuple(SYSTEM_PROFILE_REGION_PREFIXES) + (
    GLOBAL_PROFILE_PREFIX,
)

IAM_ROLE_NAME_MAX_LENGTH = 64
IAM_INSTANCE_PROFILE_NAME_MAX_LENGTH = 128
IAM_POLICY_NAME_MAX_LENGTH = 128
INFERENCE_PROFILE_NAME_MAX_LENGTH = 64

MAX_POLICY_VERSIONS = 5


def build_resource_name(parts: List[str], max_length: int) -> str:
    """
    join and sanitize name parts. over-length names are truncated and suffixed
    with a digest of the full name so the result stays unique and deterministic.
    """
    name = '-'.join([part for part in parts if Utils.is_not_empty(part)])
    name = re.sub(r'[^0-9a-zA-Z:._-]', '-', name)
    name = re.sub(r'[ _.-]{2,}', '-', name).strip(' _.-')
    if len(name) <= max_length:
        return name
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]
    truncated = name[: max_length - len(digest) - 1].rstrip(' _.-')
    return f'{truncated}-{digest}'


def build_policy_document(
    inference_profile_arns: List[str], foundation_model_arns: List[str]
) -> Dict:
    """
    invoke is allowed on the project's application inference profiles, and on the
    foundation models behind them only when the request names one of those
    profiles - so an invocation always carries the profile's cost allocation tags.
    """
    profiles = sorted(set(inference_profile_arns))
    statements = [
        {
            'Sid': 'InvokeProjectInferenceProfiles',
            'Effect': 'Allow',
            'Action': list(BEDROCK_INVOKE_ACTIONS),
            'Resource': profiles,
        }
    ]

    models = sorted(set(foundation_model_arns))
    if len(models) > 0:
        statements.append(
            {
                'Sid': 'InvokeFoundationModelsThroughProjectProfiles',
                'Effect': 'Allow',
                'Action': list(BEDROCK_INVOKE_ACTIONS),
                'Resource': models,
                'Condition': {
                    'StringEquals': {'bedrock:InferenceProfileArn': profiles}
                },
            }
        )

    if len(profiles) > 0:
        statements.append(
            {
                'Sid': 'ReadProjectInferenceProfiles',
                'Effect': 'Allow',
                'Action': list(BEDROCK_READ_ACTIONS),
                'Resource': profiles,
            }
        )
        # ListInferenceProfiles is a collection action and cannot be narrowed to this project's
        # own profiles. It also authorises against inference-profile/* when the caller does not
        # filter by type, and application-inference-profile/* when it does, so both are needed.
        collections = set()
        for arn in profiles:
            account_scope = ':'.join(arn.split(':')[:5])
            collections.add(f'{account_scope}:inference-profile/*')
            collections.add(f'{account_scope}:application-inference-profile/*')
        statements.append(
            {
                'Sid': 'ListInferenceProfiles',
                'Effect': 'Allow',
                'Action': list(BEDROCK_LIST_ACTIONS),
                'Resource': sorted(collections),
            }
        )

    return {'Version': '2012-10-17', 'Statement': statements}


def get_allowed_actions(document: Optional[Dict]) -> List[str]:
    """
    the actions a managed policy document allows. a statement that allows by exclusion
    names no actions, so it stands for all of them and only a ceiling that allows every
    action can cover it.
    """
    actions = set()
    statements = Utils.get_any_value('Statement', document, [])
    if isinstance(statements, dict):
        statements = [statements]
    for statement in Utils.get_as_list(statements, []):
        if Utils.get_value_as_string('Effect', statement, 'Allow') != 'Allow':
            continue
        if 'NotAction' in statement:
            actions.add('*')
            continue
        action = statement.get('Action')
        if isinstance(action, str):
            action = [action]
        actions.update(Utils.get_as_list(action, []))
    return sorted(actions)


def get_actions_outside_boundary(
    actions: List[str], boundary_actions: List[str]
) -> List[str]:
    """
    the actions a permissions boundary does not allow. a role's effective access is the
    intersection of the two, so these are granted by an attached policy and still
    denied on the instance. matching is case insensitive, as iam's own is.

    only the boundary's allow statements are compared, so an action can be absent here
    and still be refused by a conditional deny in the boundary.
    """
    patterns = [pattern.lower() for pattern in boundary_actions]
    return sorted(
        action
        for action in actions
        if not any(fnmatch.fnmatchcase(action.lower(), pattern) for pattern in patterns)
    )


def get_system_profile_prefix(model_id: str) -> Optional[str]:
    for prefix in SYSTEM_PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            return prefix
    return None


def is_model_supported_in_partition(partition: str, model_id: str) -> bool:
    """
    gov partitions accept a base model id (single region) or a us-gov system
    profile id. commercial partitions accept anything except the gov prefix. a
    global profile routes outside its geography, so no partition accepts one.
    """
    prefix = get_system_profile_prefix(model_id)
    if prefix == GLOBAL_PROFILE_PREFIX:
        return False
    if partition == GOV_PARTITION:
        return prefix is None or prefix == GOV_PROFILE_PREFIX
    return prefix != GOV_PROFILE_PREFIX


def is_model_supported_in_region(partition: str, region: str, model_id: str) -> bool:
    """
    a system profile id can only be used in a region its own geography covers, so
    a mismatch is reported here rather than failing opaquely at profile create.
    """
    if not is_model_supported_in_partition(partition, model_id):
        return False
    prefix = get_system_profile_prefix(model_id)
    if prefix is None:
        return True
    if prefix == US_PROFILE_PREFIX and region.startswith(GOV_REGION_PREFIX):
        return False
    region_prefixes = SYSTEM_PROFILE_REGION_PREFIXES.get(prefix, ())
    if len(region_prefixes) == 0:
        return True
    return any(region.startswith(value) for value in region_prefixes)


def get_geographic_profile_ids(
    model_id: str, partition: str = '', region: str = ''
) -> List[str]:
    """
    the geographic profile ids that carry a global id's model in this region.
    empty when the region is unknown, so the caller words the message generically.
    """
    suffix = model_id[len(GLOBAL_PROFILE_PREFIX) :]
    if Utils.is_empty(region) or Utils.is_empty(suffix):
        return []
    return [
        f'{prefix}{suffix}'
        for prefix in SYSTEM_PROFILE_REGION_PREFIXES
        if is_model_supported_in_region(partition, region, f'{prefix}{suffix}')
    ]


def validate_no_global_profiles(
    model_ids: Optional[List[str]], partition: str = '', region: str = ''
):
    """
    an application inference profile is built over a geographic system profile. a
    global one can route a request outside the geography, so the id is rejected
    where it is entered rather than skipped later during provisioning.
    """
    for model_id in Utils.get_as_list(model_ids, []):
        if get_system_profile_prefix(model_id) != GLOBAL_PROFILE_PREFIX:
            continue
        alternatives = get_geographic_profile_ids(model_id, partition, region)
        replacement = (
            ' or '.join(alternatives)
            if len(alternatives) > 0
            else 'the geographic profile id for this cluster region'
        )
        raise exceptions.invalid_params(
            f'global inference profiles are not supported: {model_id}. '
            f'use {replacement} instead.'
        )


def get_arn_region(arn: str) -> str:
    parts = arn.split(':')
    if len(parts) < 4:
        return ''
    return parts[3]


class BedrockProvisioner:
    """
    reconciles the aws resources a bedrock enabled project needs: one iam role
    and instance profile under the project role path, and one application
    inference profile per allowed model.
    """

    def __init__(self, context: SocaContext, projects_dao: ProjectsDAO):
        self.context = context
        self.projects_dao = projects_dao
        self.logger = context.logger('bedrock-provisioner')
        self.arns = ArnBuilder(context.config())

    @property
    def iam(self):
        return self.context.aws().iam()

    @property
    def bedrock(self):
        return self.context.aws().bedrock()

    @property
    def ec2(self):
        return self.context.aws().ec2()

    def is_enabled(self) -> bool:
        return self.context.config().get_bool(
            f'{self.context.module_id()}.bedrock.enabled', False
        )

    def get_model_catalog(self) -> List[str]:
        return self.context.config().get_list(
            f'{self.context.module_id()}.bedrock.model_ids', []
        )

    def get_base_policy_arn(self) -> str:
        module_id = self.context.config().get_module_id(
            constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER
        )
        policy_arn = self.context.config().get_string(
            f'{module_id}.dcv_host_policy_arn'
        )
        if Utils.is_empty(policy_arn):
            raise exceptions.general_exception(
                f'{module_id}.dcv_host_policy_arn is not set. deploy the '
                f'virtual-desktop-controller module before enabling bedrock.'
            )
        return policy_arn

    def get_managed_policy_arns(self) -> List[str]:
        """
        the same managed policies the shared dcv host role carries, so a project
        role is a drop in replacement for it.
        """
        config = self.context.config()
        policy_arns = [self.get_base_policy_arn()]
        keys = [
            'cluster.iam.policies.amazon_ssm_managed_instance_core_arn',
            'cluster.iam.policies.cloud_watch_agent_server_arn',
        ]
        if (
            config.get_string('metrics.provider')
            == constants.METRICS_PROVIDER_AMAZON_MANAGED_PROMETHEUS
        ):
            keys.append('cluster.iam.policies.amazon_prometheus_remote_write_arn')
        for key in keys:
            value = config.get_string(key)
            if Utils.is_not_empty(value):
                policy_arns.append(value)
        policy_arns += self.get_admin_policy_arns()
        return policy_arns

    def get_admin_policy_arns(self) -> List[str]:
        """
        the policies an administrator asked for on every idea instance role. their
        contents are outside idea, so they are checked against the project role
        boundary before they are attached.
        """
        return self.context.config().get_list('cluster.iam.ec2_managed_policy_arns', [])

    # naming

    def role_name(self, project: Project) -> str:
        return build_resource_name(
            [self.context.cluster_name(), project.project_id, 'project'],
            IAM_ROLE_NAME_MAX_LENGTH,
        )

    def instance_profile_name(self, project: Project) -> str:
        return build_resource_name(
            [self.context.cluster_name(), project.project_id, 'project'],
            IAM_INSTANCE_PROFILE_NAME_MAX_LENGTH,
        )

    def policy_name(self, project: Project) -> str:
        return build_resource_name(
            [self.context.cluster_name(), project.project_id, 'bedrock'],
            IAM_POLICY_NAME_MAX_LENGTH,
        )

    def inference_profile_name(self, project: Project, model_id: str) -> str:
        return build_resource_name(
            [self.context.cluster_name(), project.project_id, model_id],
            INFERENCE_PROFILE_NAME_MAX_LENGTH,
        )

    def build_tags(self, project: Project) -> Dict[str, str]:
        tags = {
            constants.IDEA_TAG_CLUSTER_NAME: self.context.cluster_name(),
            constants.IDEA_TAG_MODULE_ID: self.context.module_id(),
            constants.IDEA_TAG_PROJECT: project.name,
        }
        if Utils.is_not_empty(project.tags):
            for tag in project.tags:
                tags[tag.key] = tag.value
        custom_tags = self.context.config().get_list(
            'global-settings.custom_tags', default=[]
        )
        custom_tags_dict = Utils.convert_custom_tags_to_key_value_pairs(custom_tags)
        return {**custom_tags_dict, **tags}

    # entry point

    def reconcile_project(
        self, project_id: str, cluster_bedrock: Optional[Dict] = None
    ):
        """
        cluster_bedrock carries the enabled flag and model catalog the caller intends,
        for the case where they were just written. the in-memory config tree is built
        once and only refreshes on the dynamodb stream poller, so reading it here would
        return the pre-change values and turn a disable into a no-op.
        """
        db_project = self.projects_dao.get_project_by_id(project_id)
        if db_project is None:
            self.logger.warning(
                f'bedrock reconcile skipped, project not found: {project_id}'
            )
            return

        enabled = self.is_enabled()
        model_catalog = None
        if cluster_bedrock is not None:
            enabled = Utils.get_value_as_bool('enabled', cluster_bedrock, False)
            model_catalog = Utils.get_value_as_list('model_ids', cluster_bedrock, [])

        project = self.projects_dao.convert_from_db(db_project)
        try:
            if not enabled:
                # the feature was turned off. what was already provisioned is
                # removed, so no project keeps access that is no longer reconciled.
                if self._has_provisioner_fields(project):
                    self._teardown(project)
                return
            if project.is_enabled() and project.is_bedrock_enabled():
                self._provision(project, model_catalog)
            else:
                self._teardown(project)
        except botocore.exceptions.ClientError as e:
            if not self._is_access_denied(e):
                raise e
            reason = (
                f'denied: {e}. redeploy the cluster-manager module so its role '
                f'carries the bedrock provisioner permissions.'
            )
            self.logger.error(
                f'bedrock reconcile for project {project.name} was {reason}'
            )
            self._record_access_denied(project, e.operation_name, reason)

    def _record_access_denied(self, project: Project, operation: str, reason: str):
        """
        recorded against the project like a refused policy, so the administrator sees
        it in the project view. the provisioned fields are carried over unchanged.
        """
        bedrock = project.bedrock
        self._save_provisioner_fields(
            project,
            role_arn=bedrock.role_arn if bedrock is not None else None,
            instance_profile_arn=bedrock.instance_profile_arn
            if bedrock is not None
            else None,
            inference_profile_arns=Utils.get_as_dict(
                bedrock.inference_profile_arns if bedrock is not None else None, {}
            ),
            model_errors=bedrock.model_errors if bedrock is not None else None,
            policy_errors={operation: reason},
        )

    @staticmethod
    def _has_provisioner_fields(project: Project) -> bool:
        if project.bedrock is None:
            return False
        if Utils.is_not_empty(project.bedrock.role_arn):
            return True
        if Utils.is_not_empty(project.bedrock.instance_profile_arn):
            return True
        return len(Utils.get_as_dict(project.bedrock.inference_profile_arns, {})) > 0

    # provision

    def _provision(self, project: Project, model_catalog: Optional[List[str]] = None):
        config = self.context.config()
        partition = config.get_string('cluster.aws.partition', required=True)
        region = config.get_string('cluster.aws.region', required=True)
        catalog = set(
            self.get_model_catalog() if model_catalog is None else model_catalog
        )

        # a model that cannot be provisioned is recorded against the project rather than
        # only logged, so an administrator can see which one and why.
        model_errors: Dict[str, str] = {}
        model_ids = []
        for model_id in project.bedrock.get_model_ids():
            if model_id not in catalog:
                model_errors[model_id] = 'not in the cluster model catalog'
                self.logger.warning(
                    f'project {project.name}: model {model_id} is not in the cluster '
                    f'catalog, no access will be provisioned for it'
                )
                continue
            if not is_model_supported_in_region(partition, region, model_id):
                model_errors[model_id] = (
                    f'cannot be routed from region {region} in partition {partition}'
                )
                self.logger.warning(
                    f'project {project.name}: model {model_id} cannot be routed '
                    f'from region {region} in partition {partition}, skipped'
                )
                continue
            model_ids.append(model_id)

        self._check_permissions_boundary()

        tags = self.build_tags(project)
        inference_profile_arns = self._reconcile_inference_profiles(
            project, model_ids, tags, model_errors
        )
        (
            inference_profile_arns,
            foundation_model_arns,
            rejected_profile_arns,
        ) = self._resolve_foundation_model_arns(partition, inference_profile_arns)

        role_name = self.role_name(project)
        role_arn = self._ensure_role(project, role_name, tags)
        policy_arn = self._ensure_policy(
            project,
            list(inference_profile_arns.values()),
            foundation_model_arns,
            tags,
        )
        # an administrator supplied policy that is not attached, or could not be
        # checked, is recorded against the project for the same reason a model is.
        policy_errors: Dict[str, str] = {}
        self._ensure_attached_policies(role_name, policy_arn, policy_errors)
        if policy_arn is None:
            self._delete_policy(
                self.arns.get_project_policy_arn(self.policy_name(project))
            )
        instance_profile_arn = self._ensure_instance_profile(project, role_name, tags)

        # the policy already names only the profiles that are still allowed, so a
        # revoked model loses access here even if its profile deletion fails. one
        # that fails to delete is kept in the stored map so the next run retries it.
        retained = self._delete_stale_inference_profiles(
            project, inference_profile_arns, rejected_profile_arns
        )

        self._save_provisioner_fields(
            project,
            role_arn=role_arn,
            instance_profile_arn=instance_profile_arn,
            inference_profile_arns={**retained, **inference_profile_arns},
            model_errors=model_errors,
            policy_errors=policy_errors,
        )

    def _check_permissions_boundary(self):
        boundary_arn = self.arns.get_project_permissions_boundary_arn()
        try:
            self.iam.get_policy(PolicyArn=boundary_arn)
        except botocore.exceptions.ClientError as e:
            if self._is_no_such_entity(e):
                raise exceptions.general_exception(
                    f'project permissions boundary not found: {boundary_arn}. '
                    f'redeploy the cluster-manager module with bedrock enabled.'
                )
            raise e

    def _ensure_role(self, project: Project, role_name: str, tags: Dict) -> str:
        boundary_arn = self.arns.get_project_permissions_boundary_arn()
        try:
            role = Utils.get_value_as_dict(
                'Role', self.iam.get_role(RoleName=role_name), {}
            )
            boundary = Utils.get_value_as_dict('PermissionsBoundary', role, {})
            if (
                Utils.get_value_as_string('PermissionsBoundaryArn', boundary)
                != boundary_arn
            ):
                self.iam.put_role_permissions_boundary(
                    RoleName=role_name, PermissionsBoundary=boundary_arn
                )
            # tags are reconciled on the existing role: a renamed project has to
            # reach cost allocation under its current name.
            self.iam.tag_role(RoleName=role_name, Tags=self._iam_tags(tags))
            return Utils.get_value_as_string('Arn', role)
        except botocore.exceptions.ClientError as e:
            if not self._is_absent(e):
                raise e

        result = self.iam.create_role(
            Path=self.arns.project_role_path,
            RoleName=role_name,
            Description=f'Project instance role for project: {project.name}',
            AssumeRolePolicyDocument=Utils.to_json(self._assume_role_policy()),
            PermissionsBoundary=boundary_arn,
            Tags=self._iam_tags(tags),
        )
        return Utils.get_value_as_string(
            'Arn', Utils.get_value_as_dict('Role', result, {})
        )

    def _assume_role_policy(self) -> Dict:
        dns_suffix = self.context.config().get_string(
            'cluster.aws.dns_suffix', required=True
        )
        return {
            'Version': '2012-10-17',
            'Statement': [
                {
                    'Effect': 'Allow',
                    'Principal': {'Service': f'ec2.{dns_suffix}'},
                    'Action': 'sts:AssumeRole',
                }
            ],
        }

    def _ensure_policy(
        self,
        project: Project,
        inference_profile_arns: List[str],
        foundation_model_arns: List[str],
        tags: Dict,
    ) -> Optional[str]:
        policy_name = self.policy_name(project)
        policy_arn = self.arns.get_project_policy_arn(policy_name)

        if len(inference_profile_arns) == 0:
            # nothing is allowed, so the project carries no bedrock policy. the
            # caller detaches it before deleting it.
            return None

        document = build_policy_document(inference_profile_arns, foundation_model_arns)

        try:
            self.iam.get_policy(PolicyArn=policy_arn)
        except botocore.exceptions.ClientError as e:
            if not self._is_no_such_entity(e):
                raise e
            self.iam.create_policy(
                PolicyName=policy_name,
                Path=self.arns.project_role_path,
                PolicyDocument=Utils.to_json(document),
                Tags=self._iam_tags(tags),
            )
            return policy_arn

        if self._get_default_policy_document(policy_arn) != document:
            self._prune_policy_versions(policy_arn)
            self.iam.create_policy_version(
                PolicyArn=policy_arn,
                PolicyDocument=Utils.to_json(document),
                SetAsDefault=True,
            )
        return policy_arn

    def _get_default_policy_document(self, policy_arn: str) -> Optional[Dict]:
        versions = Utils.get_value_as_list(
            'Versions', self.iam.list_policy_versions(PolicyArn=policy_arn), []
        )
        for version in versions:
            if not Utils.get_value_as_bool('IsDefaultVersion', version, False):
                continue
            result = self.iam.get_policy_version(
                PolicyArn=policy_arn,
                VersionId=Utils.get_value_as_string('VersionId', version),
            )
            document = Utils.get_value_as_dict('PolicyVersion', result, {}).get(
                'Document'
            )
            if isinstance(document, str):
                document = json.loads(document)
            return document
        return None

    def _prune_policy_versions(self, policy_arn: str):
        # a managed policy holds at most 5 versions, so the oldest non default
        # ones are removed before a new version can be created.
        versions = Utils.get_value_as_list(
            'Versions', self.iam.list_policy_versions(PolicyArn=policy_arn), []
        )
        removable = [
            version
            for version in versions
            if not Utils.get_value_as_bool('IsDefaultVersion', version, False)
        ]
        removable.sort(
            key=lambda version: Utils.get_value_as_string('VersionId', version, 'v0')
            .lstrip('v')
            .rjust(12, '0')
        )
        remaining = len(versions)
        while remaining >= MAX_POLICY_VERSIONS and len(removable) > 0:
            version = removable.pop(0)
            self.iam.delete_policy_version(
                PolicyArn=policy_arn,
                VersionId=Utils.get_value_as_string('VersionId', version),
            )
            remaining -= 1

    def _ensure_attached_policies(
        self,
        role_name: str,
        policy_arn: Optional[str],
        policy_errors: Optional[Dict[str, str]] = None,
    ):
        desired = set(self.get_managed_policy_arns())
        if policy_arn is not None:
            desired.add(policy_arn)

        refused = self._policies_the_boundary_voids(role_name, policy_errors)
        desired -= refused

        attached = {
            Utils.get_value_as_string('PolicyArn', policy)
            for policy in Utils.get_value_as_list(
                'AttachedPolicies',
                self.iam.list_attached_role_policies(RoleName=role_name),
                [],
            )
        }

        for arn in sorted(desired - attached):
            self.iam.attach_role_policy(RoleName=role_name, PolicyArn=arn)

        # only policies this provisioner owns, or ones it has refused, are detached, so
        # anything an administrator attached by hand is reported and left alone.
        project_policy_prefix = self.arns.get_project_policy_arn('')
        for arn in sorted(attached - desired):
            if arn.startswith(project_policy_prefix) or arn in refused:
                self.iam.detach_role_policy(RoleName=role_name, PolicyArn=arn)
            else:
                self.logger.warning(
                    f'role {role_name} carries an unmanaged policy: {arn}'
                )

    def _policies_the_boundary_voids(
        self, role_name: str, policy_errors: Optional[Dict[str, str]] = None
    ) -> Set[str]:
        """
        the administrator supplied policies this role will not carry, because the
        project role boundary does not allow everything they grant. attaching one
        reads as granted in the console and denies on the instance, which is the
        failure this refusal replaces with a named reason.

        idea's own policies are never refused here. the boundary is generated to
        cover them and the administrator tests compare the two, so dropping one at
        runtime would only cost a desktop its ssm, logging and dcv access.
        """
        admin_policy_arns = self.get_admin_policy_arns()
        if len(admin_policy_arns) == 0:
            return set()

        boundary_actions = self._get_policy_actions(
            self.arns.get_project_permissions_boundary_arn()
        )
        if boundary_actions is None or len(boundary_actions) == 0:
            # nothing is refused on a guess: a ceiling that cannot be read, or that
            # read back allowing nothing, is not evidence that a policy is void, and
            # dropping one costs the role access it has today.
            self.logger.warning(
                f'role {role_name}: the project role permissions boundary could not '
                f'be read, administrator supplied policies are attached unchecked'
            )
            return set()

        refused = set()
        for arn in admin_policy_arns:
            actions = self._get_policy_actions(arn)
            if actions is None:
                reason = (
                    'could not be read, so whether the project role permissions '
                    'boundary allows what it grants is unknown'
                )
                self.logger.warning(f'role {role_name}: policy {arn} {reason}')
            else:
                outside = get_actions_outside_boundary(actions, boundary_actions)
                if len(outside) == 0:
                    continue
                reason = (
                    f'not attached: the project role permissions boundary does not '
                    f'allow {", ".join(outside)}'
                )
                self.logger.error(f'role {role_name}: policy {arn} {reason}')
                refused.add(arn)
            if policy_errors is not None:
                policy_errors[arn] = reason
        return refused

    def _get_policy_actions(self, policy_arn: str) -> Optional[List[str]]:
        """
        the actions a managed policy allows, or None when its document cannot be read.
        """
        try:
            document = self._get_default_policy_document(policy_arn)
        except botocore.exceptions.ClientError as e:
            if not (self._is_access_denied(e) or self._is_no_such_entity(e)):
                raise e
            return None
        if document is None:
            return None
        return get_allowed_actions(document)

    def _ensure_instance_profile(
        self, project: Project, role_name: str, tags: Dict
    ) -> str:
        instance_profile_name = self.instance_profile_name(project)
        try:
            instance_profile = Utils.get_value_as_dict(
                'InstanceProfile',
                self.iam.get_instance_profile(
                    InstanceProfileName=instance_profile_name
                ),
                {},
            )
        except botocore.exceptions.ClientError as e:
            if not self._is_absent(e):
                raise e
            instance_profile = Utils.get_value_as_dict(
                'InstanceProfile',
                self.iam.create_instance_profile(
                    InstanceProfileName=instance_profile_name,
                    Path=self.arns.project_role_path,
                    Tags=self._iam_tags(tags),
                ),
                {},
            )

        attached_roles = {
            Utils.get_value_as_string('RoleName', role)
            for role in Utils.get_value_as_list('Roles', instance_profile, [])
        }
        for stale_role in sorted(attached_roles - {role_name}):
            self.iam.remove_role_from_instance_profile(
                InstanceProfileName=instance_profile_name, RoleName=stale_role
            )
        if role_name not in attached_roles:
            self.iam.add_role_to_instance_profile(
                InstanceProfileName=instance_profile_name, RoleName=role_name
            )

        return Utils.get_value_as_string('Arn', instance_profile)

    # application inference profiles

    def _list_application_inference_profiles(self) -> Dict[str, Dict]:
        profiles = {}
        next_token = None
        while True:
            kwargs = {'typeEquals': 'APPLICATION'}
            if next_token is not None:
                kwargs['nextToken'] = next_token
            result = self.bedrock.list_inference_profiles(**kwargs)
            for summary in Utils.get_value_as_list(
                'inferenceProfileSummaries', result, []
            ):
                name = Utils.get_value_as_string('inferenceProfileName', summary)
                if Utils.is_not_empty(name):
                    profiles[name] = summary
            next_token = Utils.get_value_as_string('nextToken', result)
            if Utils.is_empty(next_token):
                return profiles

    def _list_system_inference_profiles(self) -> List[Dict]:
        summaries = []
        next_token = None
        while True:
            kwargs = {'typeEquals': 'SYSTEM_DEFINED'}
            if next_token is not None:
                kwargs['nextToken'] = next_token
            result = self.bedrock.list_inference_profiles(**kwargs)
            summaries += Utils.get_value_as_list(
                'inferenceProfileSummaries', result, []
            )
            next_token = Utils.get_value_as_string('nextToken', result)
            if Utils.is_empty(next_token):
                return summaries

    def _supports_on_demand(self, model_id: str) -> Optional[bool]:
        try:
            result = self.bedrock.get_foundation_model(modelIdentifier=model_id)
        except botocore.exceptions.ClientError as e:
            self.logger.warning(f'foundation model {model_id} is not available: {e}')
            return None
        details = Utils.get_value_as_dict('modelDetails', result, {})
        types = Utils.get_value_as_list('inferenceTypesSupported', details, [])
        return ON_DEMAND_INFERENCE_TYPE in types

    def _find_system_inference_profile(
        self, partition: str, region: str, model_arn: str
    ) -> Optional[str]:
        for summary in self._list_system_inference_profiles():
            profile_id = Utils.get_value_as_string('inferenceProfileId', summary, '')
            if not is_model_supported_in_region(partition, region, profile_id):
                continue
            if get_system_profile_prefix(profile_id) is None:
                continue
            for model in Utils.get_value_as_list('models', summary, []):
                if Utils.get_value_as_string('modelArn', model) == model_arn:
                    return Utils.get_value_as_string('inferenceProfileArn', summary)
        return None

    def _resolve_model_source_arn(
        self, partition: str, region: str, account_id: str, model_id: str
    ) -> Optional[str]:
        """
        copyFrom for a new application inference profile. a geographic id names a
        system profile; a base id names the foundation model, unless that model
        has no on-demand throughput, in which case the system profile that routes
        to it is used instead.
        """
        if get_system_profile_prefix(model_id) is not None:
            return f'arn:{partition}:bedrock:{region}:{account_id}:inference-profile/{model_id}'

        model_arn = f'arn:{partition}:bedrock:{region}::foundation-model/{model_id}'
        supports_on_demand = self._supports_on_demand(model_id)
        if supports_on_demand is None:
            return None
        if supports_on_demand:
            return model_arn
        return self._find_system_inference_profile(partition, region, model_arn)

    def _reconcile_inference_profiles(
        self,
        project: Project,
        model_ids: List[str],
        tags: Dict,
        model_errors: Dict[str, str],
    ) -> Dict[str, str]:
        if len(model_ids) == 0:
            return {}

        existing = self._list_application_inference_profiles()
        config = self.context.config()
        region = config.get_string('cluster.aws.region', required=True)
        partition = config.get_string('cluster.aws.partition', required=True)
        account_id = config.get_string('cluster.aws.account_id', required=True)
        profile_tags = [{'key': key, 'value': value} for key, value in tags.items()]

        inference_profile_arns = {}
        for model_id in model_ids:
            name = self.inference_profile_name(project, model_id)
            if name in existing:
                profile_arn = Utils.get_value_as_string(
                    'inferenceProfileArn', existing[name]
                )
                inference_profile_arns[model_id] = profile_arn
                # tags are reconciled on the adopt path: a renamed project has to
                # reach cost allocation under its current name.
                self._tag_inference_profile(profile_arn, profile_tags)
                continue

            source_arn = self._resolve_model_source_arn(
                partition, region, account_id, model_id
            )
            if Utils.is_empty(source_arn):
                model_errors[model_id] = (
                    f'no invocable model or system inference profile in region {region}'
                )
                self.logger.warning(
                    f'project {project.name}: model {model_id} could not be resolved '
                    f'to an invocable model or system inference profile in region '
                    f'{region}, skipped'
                )
                continue

            try:
                result = self.bedrock.create_inference_profile(
                    inferenceProfileName=name,
                    modelSource={'copyFrom': source_arn},
                    tags=profile_tags,
                )
            except botocore.exceptions.ClientError as e:
                # per model: one unavailable model must not cost the project its role,
                # its policy and access to every other model it was granted.
                model_errors[model_id] = str(
                    Utils.get_value_as_string(
                        'Message',
                        Utils.get_value_as_dict('Error', e.response, {}),
                        'inference profile could not be created',
                    )
                )
                self.logger.warning(
                    f'project {project.name}: could not create inference profile '
                    f'{name} for model {model_id}: {e}'
                )
                continue

            inference_profile_arns[model_id] = Utils.get_value_as_string(
                'inferenceProfileArn', result
            )
            self.logger.info(
                f'project {project.name}: created inference profile {name} '
                f'for model {model_id} from {source_arn}'
            )

        return inference_profile_arns

    def _tag_inference_profile(self, profile_arn: str, profile_tags: List[Dict]):
        try:
            self.bedrock.tag_resource(resourceARN=profile_arn, tags=profile_tags)
        except botocore.exceptions.ClientError as e:
            self.logger.warning(f'failed to tag inference profile {profile_arn}: {e}')

    def _resolve_foundation_model_arns(
        self, partition: str, inference_profile_arns: Dict[str, str]
    ):
        """
        the profile reports the model arns it routes to, which is what the invoke
        condition has to name. a profile reporting none would compile to a policy
        that denies every call, and a regionless arn is global routing; both are
        dropped rather than written into the policy.
        """
        kept = {}
        rejected = []
        model_arns = set()

        for model_id, profile_arn in inference_profile_arns.items():
            result = self.bedrock.get_inference_profile(
                inferenceProfileIdentifier=profile_arn
            )
            routed = [
                Utils.get_value_as_string('modelArn', model)
                for model in Utils.get_value_as_list('models', result, [])
                if Utils.is_not_empty(Utils.get_value_as_string('modelArn', model))
            ]

            if len(routed) == 0:
                self.logger.warning(
                    f'inference profile {profile_arn} reports no routed foundation '
                    f'models, model {model_id} skipped'
                )
                rejected.append(profile_arn)
                continue

            regionless = [arn for arn in routed if Utils.is_empty(get_arn_region(arn))]
            if len(regionless) > 0:
                self.logger.warning(
                    f'inference profile {profile_arn} routes globally, which is not '
                    f'supported in partition {partition}, model {model_id} skipped'
                )
                rejected.append(profile_arn)
                continue

            kept[model_id] = profile_arn
            model_arns.update(routed)

        return kept, sorted(model_arns), rejected

    def _delete_stale_inference_profiles(
        self,
        project: Project,
        inference_profile_arns: Dict[str, str],
        rejected_profile_arns: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        recorded = {}
        if project.bedrock is not None:
            recorded = Utils.get_as_dict(project.bedrock.inference_profile_arns, {})
        current = set(inference_profile_arns.values())

        retained = {}
        for model_id, profile_arn in recorded.items():
            if profile_arn in current or Utils.is_empty(profile_arn):
                continue
            if not self._delete_inference_profile(profile_arn):
                retained[model_id] = profile_arn

        for profile_arn in Utils.get_as_list(rejected_profile_arns, []):
            self._delete_inference_profile(profile_arn)

        return retained

    def _delete_inference_profile(self, profile_arn: str) -> bool:
        profile_id = profile_arn.split('/')[-1]
        try:
            self.bedrock.delete_inference_profile(inferenceProfileIdentifier=profile_id)
            return True
        except botocore.exceptions.ClientError as e:
            # deletion is best effort, the next reconcile retries it. access has
            # already been removed from the project policy at this point.
            self.logger.warning(
                f'failed to delete inference profile {profile_arn}: {e}'
            )
            return False

    # teardown

    def _instances_using_instance_profile(
        self, instance_profile_name: str
    ) -> List[str]:
        """
        ec2 instance ids still associated with the project instance profile. stopped
        instances count: a desktop that is resumed needs the role again.

        a lookup that fails answers 'in use'. deleting the role costs a running desktop
        its dcv, ssm and cloudwatch access, so an unknown must not be read as 'nobody'.
        """
        try:
            profile = Utils.get_value_as_dict(
                'InstanceProfile',
                self.iam.get_instance_profile(
                    InstanceProfileName=instance_profile_name
                ),
                {},
            )
        except botocore.exceptions.ClientError as e:
            if self._is_no_such_entity(e):
                return []
            raise e

        profile_arn = Utils.get_value_as_string('Arn', profile)
        if Utils.is_empty(profile_arn):
            return []

        instance_ids = []
        try:
            paginator = self.ec2.get_paginator('describe_instances')
            for page in paginator.paginate(
                Filters=[
                    {'Name': 'iam-instance-profile.arn', 'Values': [profile_arn]},
                    {
                        'Name': 'instance-state-name',
                        'Values': ['pending', 'running', 'stopping', 'stopped'],
                    },
                ]
            ):
                for reservation in Utils.get_value_as_list('Reservations', page, []):
                    for instance in Utils.get_value_as_list(
                        'Instances', reservation, []
                    ):
                        instance_id = Utils.get_value_as_string('InstanceId', instance)
                        if Utils.is_not_empty(instance_id):
                            instance_ids.append(instance_id)
        except botocore.exceptions.ClientError as e:
            self.logger.error(
                f'could not determine whether instance profile {instance_profile_name} '
                f'is still in use: {e}. keeping the role and instance profile.'
            )
            return ['unknown']

        return instance_ids

    def _detach_role_policy(self, role_name: str, policy_arn: str):
        try:
            self.iam.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
        except botocore.exceptions.ClientError as e:
            if not self._is_no_such_entity(e):
                raise e

    def _delete_recorded_inference_profiles(self, recorded: Dict[str, str]) -> Dict:
        retained = {}
        for model_id, profile_arn in recorded.items():
            if Utils.is_empty(profile_arn):
                continue
            if not self._delete_inference_profile(profile_arn):
                retained[model_id] = profile_arn
        return retained

    def _teardown(self, project: Project):
        role_name = self.role_name(project)
        recorded = {}
        if project.bedrock is not None:
            recorded = Utils.get_as_dict(project.bedrock.inference_profile_arns, {})

        if len(recorded) == 0 and not self._role_exists(role_name):
            return

        instance_profile_name = self.instance_profile_name(project)
        project_policy_arn = self.arns.get_project_policy_arn(self.policy_name(project))

        # revoke bedrock on its own first. the same role carries the dcv host, ssm and
        # cloudwatch policies a running desktop needs, so detaching this one policy is
        # what actually removes model access.
        if self._role_exists(role_name):
            self._detach_role_policy(role_name, project_policy_arn)

        in_use = self._instances_using_instance_profile(instance_profile_name)
        if len(in_use) > 0:
            self.logger.info(
                f'project {project.name}: bedrock access revoked. '
                f'{len(in_use)} instance(s) still use its instance profile '
                f'({", ".join(in_use)}), so the role and instance profile are kept and '
                f'removed by a later reconcile once none remain.'
            )
            self._delete_policy(project_policy_arn)
            retained = self._delete_recorded_inference_profiles(recorded)
            self._save_provisioner_fields(
                project,
                role_arn=project.bedrock.role_arn
                if project.bedrock is not None
                else None,
                instance_profile_arn=project.bedrock.instance_profile_arn
                if project.bedrock is not None
                else None,
                inference_profile_arns=retained,
            )
            return

        try:
            instance_profile = Utils.get_value_as_dict(
                'InstanceProfile',
                self.iam.get_instance_profile(
                    InstanceProfileName=instance_profile_name
                ),
                {},
            )
            for role in Utils.get_value_as_list('Roles', instance_profile, []):
                self.iam.remove_role_from_instance_profile(
                    InstanceProfileName=instance_profile_name,
                    RoleName=Utils.get_value_as_string('RoleName', role),
                )
            self.iam.delete_instance_profile(InstanceProfileName=instance_profile_name)
        except botocore.exceptions.ClientError as e:
            if not self._is_no_such_entity(e):
                raise e

        # only policies idea attached are detached. an unmanaged one is outside the
        # provisioner's iam grant, so the role is kept and reported instead.
        role_kept = False
        try:
            attached = [
                Utils.get_value_as_string('PolicyArn', policy)
                for policy in Utils.get_value_as_list(
                    'AttachedPolicies',
                    self.iam.list_attached_role_policies(RoleName=role_name),
                    [],
                )
            ]
            managed = set(self.get_managed_policy_arns()) | {project_policy_arn}
            project_policy_prefix = self.arns.get_project_policy_arn('')
            unmanaged = []
            for arn in attached:
                if arn in managed or arn.startswith(project_policy_prefix):
                    self.iam.detach_role_policy(RoleName=role_name, PolicyArn=arn)
                else:
                    unmanaged.append(arn)
            if len(unmanaged) > 0:
                role_kept = True
                self.logger.warning(
                    f'project {project.name}: role {role_name} carries unmanaged '
                    f'policies ({", ".join(unmanaged)}). it is kept until an '
                    f'administrator detaches them.'
                )
            else:
                self.iam.delete_role(RoleName=role_name)
        except botocore.exceptions.ClientError as e:
            if not self._is_no_such_entity(e):
                raise e

        self._delete_policy(project_policy_arn)

        retained = self._delete_recorded_inference_profiles(recorded)

        self._save_provisioner_fields(
            project,
            role_arn=project.bedrock.role_arn
            if role_kept and project.bedrock is not None
            else None,
            instance_profile_arn=None,
            inference_profile_arns=retained,
        )

    def _role_exists(self, role_name: str) -> bool:
        try:
            self.iam.get_role(RoleName=role_name)
            return True
        except botocore.exceptions.ClientError as e:
            if self._is_absent(e):
                return False
            raise e

    def _delete_policy(self, policy_arn: str):
        try:
            for version in Utils.get_value_as_list(
                'Versions', self.iam.list_policy_versions(PolicyArn=policy_arn), []
            ):
                if Utils.get_value_as_bool('IsDefaultVersion', version, False):
                    continue
                self.iam.delete_policy_version(
                    PolicyArn=policy_arn,
                    VersionId=Utils.get_value_as_string('VersionId', version),
                )
            self.iam.delete_policy(PolicyArn=policy_arn)
        except botocore.exceptions.ClientError as e:
            if not self._is_no_such_entity(e):
                raise e

    # persistence

    def _save_provisioner_fields(
        self,
        project: Project,
        role_arn: Optional[str],
        instance_profile_arn: Optional[str],
        inference_profile_arns: Dict[str, str],
        model_errors: Optional[Dict[str, str]] = None,
        policy_errors: Optional[Dict[str, str]] = None,
    ):
        # enabled and model_ids are re-read from storage: an administrator edit
        # that landed during this reconcile must not be reverted by the write back.
        stored = {}
        db_project = self.projects_dao.get_project_by_id(project.project_id)
        if db_project is not None:
            stored = Utils.get_value_as_dict('bedrock', db_project, {})

        model_ids = (
            project.bedrock.get_model_ids() if project.bedrock is not None else []
        )
        bedrock = {
            'enabled': Utils.get_value_as_bool(
                'enabled', stored, project.is_bedrock_enabled()
            ),
            'model_ids': Utils.get_value_as_list('model_ids', stored, model_ids),
        }
        if Utils.is_not_empty(role_arn):
            bedrock['role_arn'] = role_arn
        if Utils.is_not_empty(instance_profile_arn):
            bedrock['instance_profile_arn'] = instance_profile_arn
        if len(inference_profile_arns) > 0:
            bedrock['inference_profile_arns'] = inference_profile_arns
        # bedrock is written whole, so leaving the key out is how a cleared error goes away
        if model_errors:
            bedrock['model_errors'] = model_errors
        if policy_errors:
            bedrock['policy_errors'] = policy_errors

        self.projects_dao.update_project(
            {'project_id': project.project_id, 'bedrock': bedrock}
        )

    # helpers

    @staticmethod
    def _iam_tags(tags: Dict[str, str]) -> List[Dict[str, str]]:
        return [{'Key': key, 'Value': value} for key, value in tags.items()]

    @staticmethod
    def _is_no_such_entity(error: botocore.exceptions.ClientError) -> bool:
        code = error.response.get('Error', {}).get('Code')
        return code in ('NoSuchEntity', 'NoSuchEntityException', 'ResourceNotFound')

    @staticmethod
    def _is_access_denied(error: botocore.exceptions.ClientError) -> bool:
        code = error.response.get('Error', {}).get('Code')
        return code in (
            'AccessDenied',
            'AccessDeniedException',
            'UnauthorizedOperation',
        )

    @staticmethod
    def _is_absent(error: botocore.exceptions.ClientError) -> bool:
        # iam authorizes a name addressed read of an entity that does not exist
        # against the root path arn, which the project path scoped grant denies.
        # an entity that does exist under that path is readable, so denied is absent.
        return BedrockProvisioner._is_no_such_entity(
            error
        ) or BedrockProvisioner._is_access_denied(error)
