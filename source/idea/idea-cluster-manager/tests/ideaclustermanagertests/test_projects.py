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
Test Cases for ProjectsService
"""

from ideaclustermanager import AppContext
from ideaclustermanager.app.accounts.account_tasks import GroupMembershipUpdatedTask
from ideaclustermanager.app.projects.project_tasks import (
    ProjectEnabledTask,
    ProjectDisabledTask,
)

from ideadatamodel import (
    exceptions,
    errorcodes,
    constants,
    GetProjectRequest,
    CreateProjectRequest,
    Project,
    SocaAnyPayload,
    SocaKeyValue,
    EnableProjectRequest,
    DisableProjectRequest,
    ListProjectsRequest,
    Group,
    User,
    GetUserProjectsRequest,
)

import pytest
from typing import List, Optional


class ProjectsTestContext:
    crud_project: Optional[Project]


def enable_project(context: AppContext, project: Project):
    context.projects.enable_project(EnableProjectRequest(project_id=project.project_id))
    task = ProjectEnabledTask(context=context)
    task.invoke({'project_id': project.project_id})


def disable_project(context: AppContext, project: Project):
    context.projects.enable_project(EnableProjectRequest(project_id=project.project_id))
    task = ProjectDisabledTask(context=context)
    task.invoke({'project_id': project.project_id})


def is_memberof(context: AppContext, user: User, project: Project) -> bool:
    result = context.projects.get_user_projects(
        GetUserProjectsRequest(username=user.username)
    )

    if result.projects is None:
        return False

    for user_project in result.projects:
        if user_project.project_id == project.project_id:
            return True

    return False


def add_member(context: AppContext, user: User, project: Project):
    username = user.username
    group_name = project.ldap_groups[0]
    context.accounts.add_users_to_group(usernames=[username], group_name=group_name)
    task = GroupMembershipUpdatedTask(context=context)
    task.invoke(
        payload={'group_name': group_name, 'username': username, 'operation': 'add'}
    )


def remove_member(context: AppContext, user: User, project: Project):
    username = user.username
    group_name = project.ldap_groups[0]
    context.accounts.remove_users_from_group(
        usernames=[username], group_name=group_name
    )
    task = GroupMembershipUpdatedTask(context=context)
    task.invoke(
        payload={'group_name': group_name, 'username': username, 'operation': 'remove'}
    )


@pytest.fixture(scope='module')
def membership(context: AppContext):
    def create_project(project_name: str, project_title: str) -> Project:
        # create project group
        group = context.accounts.create_group(
            Group(
                title=f'{project_title} Project Group',
                name=f'{project_name}-project-group',
                group_type=constants.GROUP_TYPE_PROJECT,
            )
        )
        assert group is not None

        # create project
        result = context.projects.create_project(
            CreateProjectRequest(
                project=Project(
                    name=project_name, title=project_title, ldap_groups=[group.name]
                )
            )
        )
        assert result is not None
        assert result.project is not None

        # enable project
        enable_project(context=context, project=result.project)

        # get enabled project
        result = context.projects.get_project(
            GetProjectRequest(project_id=result.project.project_id)
        )

        assert result.project is not None
        assert result.project.enabled is True

        return result.project

    def create_user(username: str) -> User:
        return context.accounts.create_user(
            user=User(
                username=username,
                password='MockPassword_123',
                email=f'{username}@example.com',
            ),
            email_verified=True,
        )

    project_a = create_project('project-a', 'Project A')
    project_b = create_project('project-b', 'Project B')
    user_1 = create_user(username='project_user_1')
    user_2 = create_user(username='project_user_2')
    user_3 = create_user(username='project_user_3')

    return SocaAnyPayload(
        project_a=project_a,
        project_b=project_b,
        user_1=user_1,
        user_2=user_2,
        user_3=user_3,
    )


def test_projects_create_defaults(context):
    """
    defaults creation for projects
    """

    context.projects.create_defaults()

    result = context.projects.get_project(GetProjectRequest(project_name='default'))
    assert result is not None
    assert result.project is not None
    assert result.project.name == 'default'


def test_projects_crud_create_project(context):
    """
    create project
    """
    result = context.projects.create_project(
        CreateProjectRequest(
            project=Project(
                name='sampleproject',
                title='Sample Project',
                description='Sample Project Description',
                ldap_groups=['default-project-group'],
                tags=[
                    SocaKeyValue(key='k1', value='v1'),
                    SocaKeyValue(key='k2', value='v2'),
                ],
            )
        )
    )
    assert result is not None
    assert result.project is not None
    assert result.project.name == 'sampleproject'
    ProjectsTestContext.crud_project = result.project

    result = context.projects.get_project(
        GetProjectRequest(project_name=ProjectsTestContext.crud_project.name)
    )
    assert result is not None
    assert result.project is not None
    assert result.project.enabled is False
    assert result.project.ldap_groups is not None
    assert (
        result.project.ldap_groups[0] == ProjectsTestContext.crud_project.ldap_groups[0]
    )
    assert result.project.description is not None
    assert result.project.description == 'Sample Project Description'
    assert result.project.tags is not None
    assert len(result.project.tags) == 2
    assert result.project.tags[0].key == 'k1'
    assert result.project.tags[0].value == 'v1'
    assert result.project.tags[1].key == 'k2'
    assert result.project.tags[1].value == 'v2'
    assert result.project.created_on is not None
    assert result.project.updated_on is not None


def test_projects_crud_get_project_by_name(context):
    """
    get project by name
    """
    assert ProjectsTestContext.crud_project is not None

    result = context.projects.get_project(
        GetProjectRequest(project_name=ProjectsTestContext.crud_project.name)
    )
    assert result is not None
    assert result.project is not None
    assert result.project.name == ProjectsTestContext.crud_project.name
    assert result.project.project_id == ProjectsTestContext.crud_project.project_id


def test_projects_crud_get_project_by_id(context):
    """
    get project by id
    """
    assert ProjectsTestContext.crud_project is not None

    result = context.projects.get_project(
        GetProjectRequest(project_id=ProjectsTestContext.crud_project.project_id)
    )
    assert result is not None
    assert result.project is not None
    assert result.project.name == ProjectsTestContext.crud_project.name
    assert result.project.project_id == ProjectsTestContext.crud_project.project_id


def test_projects_crud_get_project_invalid_should_fail(context):
    """
    get project - invalid project id or name
    """

    # by project id
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.projects.get_project(GetProjectRequest(project_id='unknown-project-id'))
    assert exc_info.value.error_code == errorcodes.PROJECT_NOT_FOUND

    # by project name
    with pytest.raises(exceptions.SocaException) as exc_info:
        context.projects.get_project(
            GetProjectRequest(project_name='unknown-project-name')
        )
    assert exc_info.value.error_code == errorcodes.PROJECT_NOT_FOUND


def test_projects_create_without_ldap_groups(context):
    """
    Create project without providing ldap_groups
    """
    result = context.projects.create_project(
        CreateProjectRequest(
            project=Project(
                name='no-groups-project',
                title='Project Without Groups',
                description='Project without LDAP groups',
            )
        )
    )
    assert result is not None
    assert result.project is not None
    assert result.project.name == 'no-groups-project'
    assert result.project.ldap_groups is not None
    assert len(result.project.ldap_groups) == 0

    # Clean up
    context.projects.disable_project(
        DisableProjectRequest(project_name='no-groups-project')
    )


def test_projects_crud_enable_project(context):
    """
    enable project
    """
    assert ProjectsTestContext.crud_project is not None

    context.projects.enable_project(
        EnableProjectRequest(project_id=ProjectsTestContext.crud_project.project_id)
    )

    result = context.projects.get_project(
        GetProjectRequest(project_id=ProjectsTestContext.crud_project.project_id)
    )
    assert result is not None
    assert result.project is not None
    assert result.project.enabled is True


def test_projects_crud_disable_project(context):
    """
    disable project
    """
    assert ProjectsTestContext.crud_project is not None

    context.projects.disable_project(
        DisableProjectRequest(project_id=ProjectsTestContext.crud_project.project_id)
    )

    result = context.projects.get_project(
        GetProjectRequest(project_id=ProjectsTestContext.crud_project.project_id)
    )
    assert result is not None
    assert result.project is not None
    assert result.project.enabled is False


def test_projects_crud_list_projects(context):
    """
    list projects
    """
    assert ProjectsTestContext.crud_project is not None

    result = context.projects.list_projects(ListProjectsRequest())

    assert result is not None
    assert result.listing is not None
    assert len(result.listing) > 0

    found = None
    for project in result.listing:
        if project.project_id == ProjectsTestContext.crud_project.project_id:
            found = project
            break

    assert found is not None


def test_projects_membership_setup(context, membership):
    """
    check if membership setup data is valid and tests are starting with a clean slate.
    """

    assert membership.project_a is not None
    assert membership.project_b is not None
    assert membership.user_1 is not None
    assert membership.user_2 is not None
    assert membership.user_3 is not None

    assert is_memberof(context, membership.user_1, membership.project_a) is False
    assert is_memberof(context, membership.user_2, membership.project_a) is False
    assert is_memberof(context, membership.user_3, membership.project_a) is False

    assert is_memberof(context, membership.user_1, membership.project_b) is False
    assert is_memberof(context, membership.user_2, membership.project_b) is False
    assert is_memberof(context, membership.user_3, membership.project_b) is False


def test_projects_membership_member_added(context, membership):
    """
    add user to a group and check if user is member of projects
    """

    add_member(context, membership.user_1, membership.project_a)
    assert is_memberof(context, membership.user_1, membership.project_a) is True

    add_member(context, membership.user_2, membership.project_b)
    assert is_memberof(context, membership.user_2, membership.project_b) is True


def test_projects_membership_member_removed(context, membership):
    """
    add user to a group and check if user is member of projects
    """

    remove_member(context, membership.user_1, membership.project_a)
    assert is_memberof(context, membership.user_1, membership.project_a) is False

    remove_member(context, membership.user_2, membership.project_b)
    assert is_memberof(context, membership.user_2, membership.project_b) is False


def test_projects_membership_project_disabled(context, membership):
    """
    disable project and add member. user should not be member of project.
    """

    disable_project(context, membership.project_a)

    add_member(context, membership.user_1, membership.project_a)
    assert is_memberof(context, membership.user_1, membership.project_a) is False


def test_projects_membership_project_enabled(context, membership):
    """
    add member, enable project and check if existing group members are part of project
    """

    add_member(context, membership.user_1, membership.project_a)
    add_member(context, membership.user_2, membership.project_a)
    add_member(context, membership.user_3, membership.project_a)

    enable_project(context, membership.project_a)

    assert is_memberof(context, membership.user_1, membership.project_a) is True
    assert is_memberof(context, membership.user_2, membership.project_a) is True
    assert is_memberof(context, membership.user_3, membership.project_a) is True


def test_projects_membership_multiple_projects(context, membership):
    """
    add user to multiple projects. check for membership for all
    """

    # pre-requisites
    remove_member(context, membership.user_1, membership.project_a)
    remove_member(context, membership.user_1, membership.project_b)
    enable_project(context, membership.project_a)

    add_member(context, membership.user_1, membership.project_a)
    add_member(context, membership.user_1, membership.project_b)

    assert is_memberof(context, membership.user_1, membership.project_a) is True
    assert is_memberof(context, membership.user_1, membership.project_b) is True


def test_projects_get_user_projects(context, membership):
    """
    get user projects
    """

    # setup
    def clear_memberships(user: User):
        remove_member(context, user, membership.project_a)
        remove_member(context, user, membership.project_b)

    clear_memberships(membership.user_1)
    clear_memberships(membership.user_2)
    clear_memberships(membership.user_3)

    enable_project(context, membership.project_a)
    enable_project(context, membership.project_b)

    add_member(context, membership.user_1, membership.project_a)

    add_member(context, membership.user_2, membership.project_b)

    add_member(context, membership.user_3, membership.project_a)
    add_member(context, membership.user_3, membership.project_b)

    # verify

    def check_user_projects(username: str, project_ids: List[str]):
        result = context.projects.get_user_projects(
            GetUserProjectsRequest(username=username)
        )
        assert result.projects is not None
        count = 0
        for project in result.projects:
            if project.project_id in project_ids:
                count += 1

        assert len(project_ids) == count

    check_user_projects(
        username=membership.user_1.username,
        project_ids=[membership.project_a.project_id],
    )
    assert is_memberof(context, membership.user_1, membership.project_b) is False

    check_user_projects(
        username=membership.user_2.username,
        project_ids=[membership.project_b.project_id],
    )
    assert is_memberof(context, membership.user_2, membership.project_a) is False

    check_user_projects(
        username=membership.user_3.username,
        project_ids=[membership.project_a.project_id, membership.project_b.project_id],
    )
