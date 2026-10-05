"""
The image pipelines' validation identity: one service user and its hidden project,
shared by the desktop test launch (virtual desktop controller) and the compute canary
(scheduler). Either module creates them on first use, so a cluster with only one of the
two still validates, and a second caller (or a race between both) finds them in place.
"""

from typing import Callable, Optional

from ideadatamodel import (
    CreateProjectRequest,
    CreateUserRequest,
    EnableProjectRequest,
    GetUserRequest,
    ImagePipelineSettings,
    Project,
    SocaBaseModel,
    User,
)
from ideasdk.utils.group_name_helper import GroupNameHelper


def _not_found(error: Exception) -> bool:
    text = str(error)
    return 'not found' in text.lower() or 'NOT_FOUND' in text


def _user_exists(context, username: str) -> bool:
    try:
        context.accounts_client.get_user(GetUserRequest(username=username))
        return True
    except Exception as e:
        if not _not_found(e):
            raise
        return False


def _project(context, name: str) -> Optional[Project]:
    try:
        return context.projects_client.get_project_by_name(name)
    except Exception as e:
        if not _not_found(e):
            raise
        return None


def _reread_project(context, name: str) -> Optional[Project]:
    context.projects_client.cache.clear()
    return _project(context, name)


def ensure_validation_identity(
    context,
    settings: ImagePipelineSettings,
    invoke: Optional[Callable[[str, SocaBaseModel], None]] = None,
) -> Project:
    """
    the validation user and its hidden, enabled project, created through the cluster
    manager when missing. the user is a directory (AD) account: the cluster manager
    creates the IDEA side; in a read-only directory the AD account must already exist.
    a create that fails because the other module created it meanwhile is not an error
    """
    invoke = invoke or (
        lambda namespace, payload: invoke_cluster_manager(context, namespace, payload)
    )
    user = settings.validation_user
    project_name = settings.validation_project
    if not _user_exists(context, user):
        try:
            invoke(
                'Accounts.CreateUser',
                CreateUserRequest(
                    user=User(
                        username=user, email=f'{user}@validation.invalid', sudo=False
                    ),
                    email_verified=False,
                ),
            )
        except Exception:
            if not _user_exists(context, user):
                raise
    project = _project(context, project_name)
    if project is None:
        group = GroupNameHelper(context).get_user_group(user)
        try:
            invoke(
                'Projects.CreateProject',
                CreateProjectRequest(
                    project=Project(
                        name=project_name,
                        title='Image validation',
                        description='hidden: desktops and compute jobs the image pipelines launch to validate new images',
                        ldap_groups=[group],
                        enable_budgets=False,
                    )
                ),
            )
        except Exception:
            if _reread_project(context, project_name) is None:
                raise
        project = _reread_project(context, project_name)
    # CreateProject always creates a disabled project, and a disabled project is
    # left out of the user's projects, so CreateSession and job submission refuse it
    if not project.enabled:
        invoke(
            'Projects.EnableProject',
            EnableProjectRequest(project_id=project.project_id),
        )
        project = _reread_project(context, project_name)
    return project


def invoke_cluster_manager(context, namespace: str, payload: SocaBaseModel) -> None:
    """
    a write to the cluster manager. it needs cluster-manager/write on the calling
    module's client, which is requested here and nowhere else so a client without it
    keeps working. payload is the namespace's request model: the envelope is serialized
    by pydantic, which cannot serialize a SocaAnyPayload
    """
    from ideasdk.auth import TokenService, TokenServiceOptions
    from ideasdk.client.soca_client import SocaClient, SocaClientOptions
    from ideadatamodel import SocaAnyPayload, constants

    config = context.config()
    module_id = config.get_module_id(constants.MODULE_CLUSTER_MANAGER)
    base = context.token_service.options
    token_service = TokenService(
        context=context,
        options=TokenServiceOptions(
            cognito_user_pool_provider_url=base.cognito_user_pool_provider_url,
            cognito_user_pool_domain_url=base.cognito_user_pool_domain_url,
            client_id=base.client_id,
            client_secret=base.client_secret,
            client_credentials_scope=[f'{module_id}/write'],
            administrators_group_name=base.administrators_group_name,
            managers_group_name=base.managers_group_name,
        ),
    )
    try:
        token = token_service.get_access_token()
    except Exception as e:
        raise RuntimeError(
            f'{context.module_id()} cannot create the validation identity ({namespace}): '
            f'its client has no {module_id}/write scope ({e}). create the user and project '
            f'by hand or grant the scope'
        )
    client = SocaClient(
        context=context,
        options=SocaClientOptions(
            endpoint=f'{config.get_cluster_internal_endpoint()}/{module_id}/api/v1',
            enable_logging=False,
            verify_ssl=False,
        ),
    )
    try:
        client.invoke_alt(
            namespace=namespace,
            payload=payload,
            result_as=SocaAnyPayload,
            access_token=token,
        )
    finally:
        client.close()
