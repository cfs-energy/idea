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

from ideadatamodel import (
    AuthResult,
    User,
    CreateUserRequest,
    CreateUserResult,
    GetUserRequest,
    GetUserResult,
    GetGroupRequest,
    GetGroupResult,
    InitiateAuthRequest,
    InitiateAuthResult,
    ChangePasswordRequest,
    ChangePasswordResult,
    ListUsersRequest,
    ListUsersResult,
    CreateProjectRequest,
    CreateProjectResult,
    Project,
    SocaKeyValue,
    EnableProjectRequest,
    GetProjectRequest,
    GetProjectResult,
    UpdateProjectRequest,
    ListProjectsRequest,
    ListProjectsResult,
    DeleteProjectRequest,
    DeleteProjectResult,
    DeleteUserRequest,
    DeleteUserResult
)
from ideasdk.utils import Utils, GroupNameHelper
from ideadatamodel import exceptions

from ideaadministrator.integration_tests.test_context import TestContext
from ideaadministrator.integration_tests import test_constants

from typing import Optional

user_auth: Optional[AuthResult] = None
test_username = None
test_password = None
user: Optional[User] = None


def test_admin_create_user(context: TestContext):
    global test_username
    global test_password
    test_username = Utils.generate_password(8, 0, 8, 0, 0)
    test_password = Utils.generate_password(8, 1, 1, 1, 1)
    email = f'{test_username}@sampleideauser.local'
    result = context.get_cluster_manager_client().invoke_alt(
        namespace='Accounts.CreateUser',
        payload=CreateUserRequest(
            user=User(
                username=test_username,
                password=test_password,
                email=email,
                additional_groups=["managers-cluster-group"],
            ),
            email_verified=True
        ),
        result_as=CreateUserResult,
        access_token=context.get_admin_access_token()
    )
    global user
    user = result.user
    assert Utils.is_not_empty(user)
    assert Utils.is_not_empty(user.username)
    assert Utils.are_equal(test_username, user.username)
    assert Utils.are_equal(email, user.email)
    # assert Utils.are_equal(user.status, 'CONFIRMED')
    assert Utils.is_false(user.sudo)
    assert Utils.is_true(user.enabled)
    assert Utils.is_not_empty(user.uid)
    assert Utils.is_not_empty(user.gid)
    assert Utils.is_not_empty(user.group_name)
    assert Utils.is_not_empty(user.login_shell)
    assert Utils.are_equal(user.login_shell, '/bin/bash')
    assert Utils.are_equal(user.home_dir, f'/data/home/{test_username}')


def test_admin_get_user(context: TestContext):
    result = context.get_cluster_manager_client().invoke_alt(
        namespace='Accounts.GetUser',
        payload=GetUserRequest(
            username=test_username
        ),
        result_as=GetUserResult,
        access_token=context.get_admin_access_token()
    )

    assert result.user is not None
    assert result.user.username == test_username


def test_get_user_group(context: TestContext):
    assert test_username is not None
    group_name = GroupNameHelper(context.idea_context).get_user_group(str(test_username))
    result = context.get_cluster_manager_client().invoke_alt(
        namespace='Accounts.GetGroup',
        payload=GetGroupRequest(
            group_name=group_name
        ),
        result_as=GetGroupResult,
        access_token=context.get_admin_access_token()
    )
    group = result.group
    assert group is not None
    assert group.name == group_name


def test_user_initiate_auth(context: TestContext):
    result = context.get_cluster_manager_client().invoke_alt(
        namespace='Auth.InitiateAuth',
        payload=InitiateAuthRequest(
            auth_flow='USER_PASSWORD_AUTH',
            username=test_username,
            password=test_password
        ),
        result_as=InitiateAuthResult
    )
    assert result.auth is not None
    assert Utils.is_not_empty(result.auth.access_token)
    global user_auth
    user_auth = result.auth


def test_user_change_password(context: TestContext):
    global test_password
    old_password = test_password
    test_password = Utils.generate_password(8, 1, 1, 1, 1)

    context.get_cluster_manager_client().invoke_alt(
        namespace='Auth.ChangePassword',
        payload=ChangePasswordRequest(
            username=test_username,
            old_password=old_password,
            new_password=test_password
        ),
        result_as=ChangePasswordResult,
        access_token=user_auth.access_token
    )


def test_admin_list_users(context: TestContext):
    result = context.get_cluster_manager_client().invoke_alt(
        namespace='Accounts.ListUsers',
        payload=ListUsersRequest(),
        result_as=ListUsersResult,
        access_token=context.get_admin_access_token()
    )
    assert len(result.listing) > 0


def test_admin_create_project(context: TestContext):
    project_name = Utils.generate_password(8, 0, 8, 0, 0)

    create_project_result = context.get_cluster_manager_client().invoke_alt(
        namespace='Projects.CreateProject',
        payload=CreateProjectRequest(
            project=Project(
                name=project_name,
                title=f'{project_name} title',
                description=f'{project_name} description',
                ldap_groups=[GroupNameHelper(context.idea_context).get_default_project_group()],
                tags=[
                    SocaKeyValue(key='key', value='value')
                ]
            )
        ),
        result_as=CreateProjectResult,
        access_token=context.get_admin_access_token()
    )

    project = create_project_result.project
    assert project is not None
    assert project.project_id is not None and len(project.project_id) > 0
    assert project.enabled is None or project.enabled is False

    global TEST_PROJECT_ID
    TEST_PROJECT_ID = project.project_id

    context.get_cluster_manager_client().invoke_alt(
        namespace='Projects.EnableProject',
        payload=EnableProjectRequest(
            project_id=project.project_id
        ),
        access_token=context.get_admin_access_token()
    )


def test_admin_get_project(context: TestContext):
    assert context.is_test_case_passed(test_constants.PROJECTS_CREATE_PROJECT)

    get_project_result = context.get_cluster_manager_client().invoke_alt(
        namespace='Projects.GetProject',
        payload=GetProjectRequest(
            project_id=TEST_PROJECT_ID
        ),
        result_as=GetProjectResult,
        access_token=context.get_admin_access_token()
    )

    assert get_project_result.project is not None
    assert get_project_result.project.enabled is True


def test_admin_update_project(context: TestContext):
    assert context.is_test_case_passed(test_constants.PROJECTS_CREATE_PROJECT)

    get_project_result = context.get_cluster_manager_client().invoke_alt(
        namespace='Projects.GetProject',
        payload=GetProjectRequest(
            project_id=TEST_PROJECT_ID
        ),
        result_as=GetProjectResult,
        access_token=context.get_admin_access_token()
    )

    project = get_project_result.project
    updated_title = f'{project.title} - updated'
    project.title = updated_title

    context.get_cluster_manager_client().invoke_alt(
        namespace='Projects.UpdateProject',
        payload=UpdateProjectRequest(
            project=project
        ),
        access_token=context.get_admin_access_token()
    )

    get_updated_project_result = context.get_cluster_manager_client().invoke_alt(
        namespace='Projects.GetProject',
        payload=GetProjectRequest(
            project_id=TEST_PROJECT_ID
        ),
        result_as=GetProjectResult,
        access_token=context.get_admin_access_token()
    )

    assert get_updated_project_result.project.title == updated_title


def test_admin_list_projects(context: TestContext):
    assert context.is_test_case_passed(test_constants.PROJECTS_CREATE_PROJECT)

    list_projects_result = context.get_cluster_manager_client().invoke_alt(
        namespace='Projects.ListProjects',
        payload=ListProjectsRequest(),
        result_as=ListProjectsResult,
        access_token=context.get_admin_access_token()
    )

    assert list_projects_result.listing is not None
    assert len(list_projects_result.listing) > 0

    found = False
    for project in list_projects_result.listing:
        if project.project_id == TEST_PROJECT_ID:
            found = True
    assert found is True


def test_admin_disable_project(context: TestContext):
    assert context.is_test_case_passed(test_constants.PROJECTS_CREATE_PROJECT)

    context.get_cluster_manager_client().invoke_alt(
        namespace='Projects.DisableProject',
        payload=DeleteProjectRequest(
            project_id=TEST_PROJECT_ID
        ),
        result_as=DeleteProjectResult,
        access_token=context.get_admin_access_token()
    )

    try:
        context.get_cluster_manager_client().invoke_alt(
            namespace='Projects.GetProject',
            payload=GetProjectRequest(
                project_id=TEST_PROJECT_ID
            ),
            result_as=GetProjectResult,
            access_token=context.get_admin_access_token()
        )
    except exceptions.SocaException as e:
        assert e.error_code == 'SCHEDULER_HPC_PROJECT_NOT_FOUND'


def test_admin_disable_user(context: TestContext):
    context.get_cluster_manager_client().invoke_alt(
        namespace='Accounts.DisableUser',
        payload=DeleteUserRequest(
            username=test_username
        ),
        result_as=DeleteUserResult,
        access_token=context.get_admin_access_token()
    )


TEST_CASES = [
    {
        'test_case_id': test_constants.ACCOUNTS_CREATE_USER,
        'test_case': test_admin_create_user
    },
    {
        'test_case_id': test_constants.ACCOUNTS_GET_USER,
        'test_case': test_admin_get_user
    },
    {
        'test_case_id': test_constants.ACCOUNTS_GET_USER_GROUP,
        'test_case': test_get_user_group
    },
    {
        'test_case_id': test_constants.AUTH_INITIATE_AUTH,
        'test_case': test_user_initiate_auth
    },
    {
        'test_case_id': test_constants.AUTH_CHANGE_PASSWORD,
        'test_case': test_user_change_password
    },
    {
        'test_case_id': test_constants.ACCOUNTS_LIST_USERS,
        'test_case': test_admin_list_users
    },
    {
        'test_case_id': test_constants.ACCOUNTS_DISABLE_USER,
        'test_case': test_admin_disable_user
    },
    {
        'test_case_id': test_constants.PROJECTS_CREATE_PROJECT,
        'test_case': test_admin_create_project
    },
    {
        'test_case_id': test_constants.PROJECTS_GET_PROJECT,
        'test_case': test_admin_get_project
    },
    {
        'test_case_id': test_constants.PROJECTS_GET_PROJECT,
        'test_case': test_admin_get_project
    },
    {
        'test_case_id': test_constants.PROJECTS_UPDATE_PROJECT,
        'test_case': test_admin_update_project
    },
    {
        'test_case_id': test_constants.PROJECTS_LIST_PROJECTS,
        'test_case': test_admin_list_projects
    },
    {
        'test_case_id': test_constants.PROJECTS_DISABLE_PROJECT,
        'test_case': test_admin_disable_project
    }
]
