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
    context.projects.disable_project(
        DisableProjectRequest(project_id=project.project_id)
    )
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


@pytest.fixture
def two_group_project(context: AppContext):

    groups = [
        context.accounts.create_group(
            Group(
                title=f'two groups {n}',
                name=f'twogroups-{n}-project-group',
                group_type=constants.GROUP_TYPE_PROJECT,
            )
        )
        for n in (1, 2)
    ]
    project = context.projects.create_project(
        CreateProjectRequest(
            project=Project(
                name='two-groups',
                title='Two Groups',
                ldap_groups=[g.name for g in groups],
            )
        )
    ).project
    enable_project(context=context, project=project)
    user = context.accounts.create_user(
        user=User(username='twogroups_user', email='twogroups_user@example.com')
    )
    for group in groups:
        context.accounts.add_users_to_group([user.username], group.name)
        GroupMembershipUpdatedTask(context=context).invoke(
            payload={
                'group_name': group.name,
                'username': user.username,
                'operation': 'add',
            }
        )
    yield SocaAnyPayload(groups=groups, project=project, user=user)
    context.accounts.delete_user(user.username)
    context.projects.projects_dao.delete_project(project.project_id)
    for group in groups:
        context.accounts.delete_group(group.name, force=True)


def test_a_project_stays_while_another_of_its_groups_grants_it(
    context, two_group_project
):
    t = two_group_project
    assert is_memberof(context, t.user, t.project)
    context.accounts.remove_users_from_group([t.user.username], t.groups[0].name)
    GroupMembershipUpdatedTask(context=context).invoke(
        payload={
            'group_name': t.groups[0].name,
            'username': t.user.username,
            'operation': 'remove',
        }
    )
    assert is_memberof(context, t.user, t.project)
    context.accounts.remove_users_from_group([t.user.username], t.groups[1].name)
    GroupMembershipUpdatedTask(context=context).invoke(
        payload={
            'group_name': t.groups[1].name,
            'username': t.user.username,
            'operation': 'remove',
        }
    )
    assert not is_memberof(context, t.user, t.project)


def test_removing_a_group_from_a_project_removes_what_only_it_granted(
    context, two_group_project
):
    from ideaclustermanager.app.projects.project_tasks import ProjectGroupsUpdatedTask

    t = two_group_project
    only = context.accounts.create_user(
        user=User(username='onegroup_user', email='onegroup_user@example.com')
    )
    try:
        context.accounts.add_users_to_group([only.username], t.groups[0].name)
        GroupMembershipUpdatedTask(context=context).invoke(
            payload={
                'group_name': t.groups[0].name,
                'username': only.username,
                'operation': 'add',
            }
        )
        assert is_memberof(context, only, t.project)
        context.projects.projects_dao.update_project(
            {'project_id': t.project.project_id, 'ldap_groups': [t.groups[1].name]}
        )
        ProjectGroupsUpdatedTask(context).invoke(
            {
                'project_id': t.project.project_id,
                'groups_added': [],
                'groups_removed': [t.groups[0].name],
            }
        )
        assert not is_memberof(context, only, t.project)
        # still granted through the group that stays
        assert is_memberof(context, t.user, t.project)
    finally:
        context.accounts.delete_user(only.username)


def queued_group_update(context, monkeypatch, project_id, ldap_groups):
    """update a project's groups and run the task it queues, sent as the queue sends it"""
    from ideadatamodel import UpdateProjectRequest
    from ideaclustermanager.app.projects.project_tasks import ProjectGroupsUpdatedTask
    from ideasdk.utils import Utils

    sent = []
    monkeypatch.setattr(
        context.projects.task_manager,
        'send',
        lambda task_name, payload, **_: sent.append((task_name, payload)),
    )
    context.projects.update_project(
        UpdateProjectRequest(
            project=Project(project_id=project_id, ldap_groups=ldap_groups)
        )
    )
    for task_name, payload in sent:
        if task_name == 'projects.project-groups-updated':
            # the queue body is JSON: what the worker reads is what survives it
            ProjectGroupsUpdatedTask(context).invoke(
                Utils.from_json(Utils.to_json(payload))
            )
    return [name for name, _ in sent]


def test_a_project_group_edit_reaches_the_members_through_the_queue(
    context, two_group_project, monkeypatch
):
    t = two_group_project
    only = context.accounts.create_user(
        user=User(username='queued_user', email='queued_user@example.com')
    )
    try:
        context.accounts.add_users_to_group([only.username], t.groups[0].name)
        GroupMembershipUpdatedTask(context=context).invoke(
            payload={
                'group_name': t.groups[0].name,
                'username': only.username,
                'operation': 'add',
            }
        )
        assert is_memberof(context, only, t.project)
        # the first group leaves: its only member loses the project, the overlapping
        # member keeps it through the second group
        queued_group_update(
            context, monkeypatch, t.project.project_id, [t.groups[1].name]
        )
        assert not is_memberof(context, only, t.project)
        assert is_memberof(context, t.user, t.project)
        # the last group leaves: no one keeps it
        names = queued_group_update(context, monkeypatch, t.project.project_id, [])
        assert 'projects.project-groups-updated' in names
        assert not is_memberof(context, t.user, t.project)
        # the first group comes back: its member gets the project again
        queued_group_update(
            context, monkeypatch, t.project.project_id, [t.groups[0].name]
        )
        assert is_memberof(context, only, t.project)
    finally:
        context.accounts.delete_user(only.username)


def test_a_disabled_group_grants_none_of_its_projects_until_enabled(
    context, two_group_project
):
    t = two_group_project
    only = context.accounts.create_user(
        user=User(username='disabled_group_user', email='dgu@example.com')
    )
    try:
        context.accounts.add_users_to_group([only.username], t.groups[0].name)
        GroupMembershipUpdatedTask(context=context).invoke(
            payload={
                'group_name': t.groups[0].name,
                'username': only.username,
                'operation': 'add',
            }
        )
        context.accounts.disable_group(t.groups[0].name)
        assert not is_memberof(context, only, t.project)
        # still granted through the other, enabled group
        assert is_memberof(context, t.user, t.project)
        context.accounts.enable_group(t.groups[0].name)
        assert is_memberof(context, only, t.project)
    finally:
        context.accounts.delete_user(only.username)


def join_group(context, username, group_name):
    context.accounts.add_users_to_group([username], group_name)
    GroupMembershipUpdatedTask(context=context).invoke(
        payload={'group_name': group_name, 'username': username, 'operation': 'add'}
    )


def test_a_group_left_on_a_project_by_an_earlier_release_grants_nothing_after_an_edit(
    context, two_group_project, monkeypatch
):
    t = two_group_project
    dao = context.projects.user_projects_dao
    stray = context.accounts.create_group(
        Group(name='stray-project-group', title='stray', group_type='project')
    )
    newcomer = context.accounts.create_user(
        user=User(username='stray_member', email='stray_member@example.com')
    )
    try:
        # an earlier release left this group's index row on the project
        dao.project_groups_table.put_item(
            Item={'group_name': stray.name, 'project_id': t.project.project_id}
        )
        # any edit of the project through the queue repairs the index
        queued_group_update(
            context, monkeypatch, t.project.project_id, [t.groups[1].name]
        )
        join_group(context, newcomer.username, stray.name)
        assert not is_memberof(context, newcomer, t.project)
    finally:
        context.accounts.delete_user(newcomer.username)
        context.accounts.delete_group(stray.name, force=True)


def test_a_group_disabled_after_an_edit_revokes_its_project_without_a_restart(
    context, two_group_project, monkeypatch
):
    t = two_group_project
    dao = context.projects.user_projects_dao
    only = context.accounts.create_user(
        user=User(username='index_member', email='index_member@example.com')
    )
    try:
        # an earlier release added the first group by an edit that never wrote its
        # index row
        dao.project_groups_table.delete_item(
            Key={'group_name': t.groups[0].name, 'project_id': t.project.project_id}
        )
        join_group(context, only.username, t.groups[0].name)
        # an edit adding a third group, through the queue, repairs the index
        third = context.accounts.create_group(
            Group(name='third-project-group', title='third', group_type='project')
        )
        queued_group_update(
            context,
            monkeypatch,
            t.project.project_id,
            [t.groups[0].name, t.groups[1].name, third.name],
        )
        assert is_memberof(context, only, t.project)
        context.accounts.disable_group(t.groups[0].name)
        assert not is_memberof(context, only, t.project)
        context.accounts.delete_group(third.name, force=True)
    finally:
        context.accounts.delete_user(only.username)


def test_the_start_time_refresh_repairs_every_projects_rows_and_index(
    context, two_group_project
):
    t = two_group_project
    dao = context.projects.user_projects_dao
    # what earlier releases left: a row no group grants, a granted row missing, an index
    # row for a group not on the project and none for one that is
    dao.create_user_project(project_id=t.project.project_id, username='stale_marker')
    dao.delete_user_project(project_id=t.project.project_id, username=t.user.username)
    dao.project_groups_table.put_item(
        Item={'group_name': 'gone-marker-group', 'project_id': t.project.project_id}
    )
    dao.project_groups_table.delete_item(
        Key={'group_name': t.groups[1].name, 'project_id': t.project.project_id}
    )
    # the start path calls this once, inside the defaults lock
    dao.refresh_all_projects()
    assert not dao.is_user_in_project('stale_marker', t.project.project_id)
    assert dao.is_user_in_project(t.user.username, t.project.project_id)
    assert t.project.project_id not in dao.get_projects_by_group_name(
        'gone-marker-group'
    )
    assert t.project.project_id in dao.get_projects_by_group_name(t.groups[1].name)


def test_remove_add_remove_of_a_group_each_reach_the_worker(
    context, two_group_project, monkeypatch
):
    from ideadatamodel import UpdateProjectRequest
    from ideaclustermanager.app.projects.project_tasks import ProjectGroupsUpdatedTask
    from ideasdk.utils import Utils

    t = two_group_project
    seen = set()
    delivered = []

    def fifo_send(task_name, payload, message_dedupe_id=None, **_):
        # the queue drops a repeated de-duplication id for five minutes
        dedupe = message_dedupe_id or Utils.sha256(Utils.to_json(payload))
        if dedupe in seen:
            return
        seen.add(dedupe)
        delivered.append((task_name, Utils.from_json(Utils.to_json(payload))))

    only = context.accounts.create_user(
        user=User(username='rar_member', email='rar_member@example.com')
    )
    join_group(context, only.username, t.groups[0].name)
    assert is_memberof(context, only, t.project)
    monkeypatch.setattr(context.projects.task_manager, 'send', fifo_send)
    one, both = [t.groups[1].name], [t.groups[0].name, t.groups[1].name]
    for groups in (one, both, one):
        context.projects.update_project(
            UpdateProjectRequest(
                project=Project(project_id=t.project.project_id, ldap_groups=groups)
            )
        )
        while delivered:
            name, payload = delivered.pop(0)
            if name == 'projects.project-groups-updated':
                ProjectGroupsUpdatedTask(context).invoke(payload)
    try:
        # the group left last: its only member keeps no grant
        assert not is_memberof(context, only, t.project)
    finally:
        context.accounts.delete_user(only.username)


@pytest.fixture
def sole_member(context, two_group_project):
    # a member through the first group alone, and that group's record as it was
    t = two_group_project
    only = context.accounts.create_user(
        user=User(username='sole_member', email='sole_member@example.com')
    )
    join_group(context, only.username, t.groups[0].name)
    table = context.accounts.group_dao.table
    record = table.get_item(Key={'group_name': t.groups[0].name})['Item']
    assert is_memberof(context, only, t.project)
    yield only, table, record
    table.put_item(Item=record)
    context.accounts.delete_user(only.username)


@pytest.mark.parametrize(
    'edit',
    [
        {'UpdateExpression': 'REMOVE enabled'},
        {
            'UpdateExpression': 'SET enabled = :none',
            'ExpressionAttributeValues': {':none': None},
        },
    ],
    ids=['no-enabled', 'null-enabled'],
)
def test_a_group_written_without_an_enabled_value_still_grants_its_project(
    context, two_group_project, sole_member, edit
):
    t = two_group_project
    only, table, _ = sole_member
    table.update_item(Key={'group_name': t.groups[0].name}, **edit)
    context.projects.user_projects_dao.refresh_all_projects()
    assert is_memberof(context, only, t.project)


def test_a_project_without_an_enabled_value_keeps_its_rows_and_gains_none(
    context, two_group_project
):
    # every release writes a new project without the value until it is first enabled:
    # it grants nothing, and rows a record holds without it are not taken away
    t = two_group_project
    dao = context.projects.user_projects_dao
    table = context.projects.projects_dao.table
    never = context.projects.create_project(
        CreateProjectRequest(
            project=Project(
                name='never-enabled', title='ne', ldap_groups=[t.groups[0].name]
            )
        )
    ).project
    assert 'enabled' not in table.get_item(Key={'project_id': never.project_id})['Item']
    table.update_item(
        Key={'project_id': t.project.project_id}, UpdateExpression='REMOVE enabled'
    )
    try:
        dao.refresh_all_projects()
        assert dao.is_user_in_project(t.user.username, t.project.project_id)
        assert not dao.is_user_in_project(t.user.username, never.project_id)
    finally:
        table.update_item(
            Key={'project_id': t.project.project_id},
            UpdateExpression='SET enabled = :on',
            ExpressionAttributeValues={':on': True},
        )
        context.projects.projects_dao.delete_project(never.project_id)


def test_a_project_group_with_no_group_record_keeps_its_members(
    context, two_group_project, sole_member, caplog
):
    t = two_group_project
    only, table, _ = sole_member
    table.delete_item(Key={'group_name': t.groups[0].name})
    context.projects.user_projects_dao.refresh_all_projects()
    assert is_memberof(context, only, t.project)
    assert f'names group {t.groups[0].name}, which has no group record' in caplog.text


def test_an_explicitly_disabled_group_loses_its_project_at_start(
    context, two_group_project, sole_member
):
    t = two_group_project
    only, table, _ = sole_member
    table.update_item(
        Key={'group_name': t.groups[0].name},
        UpdateExpression='SET enabled = :off',
        ExpressionAttributeValues={':off': False},
    )
    context.projects.user_projects_dao.refresh_all_projects()
    assert not is_memberof(context, only, t.project)
    # still granted through the other, enabled group
    assert is_memberof(context, t.user, t.project)


def test_a_failed_read_at_start_changes_no_project(
    context, two_group_project, monkeypatch
):
    from botocore.exceptions import ClientError

    t = two_group_project
    dao = context.projects.user_projects_dao
    failing = context.accounts.create_group(
        Group(name='readfail-project-group', title='rf', group_type='project')
    )
    other = context.projects.create_project(
        CreateProjectRequest(
            project=Project(name='readfail', title='rf', ldap_groups=[failing.name])
        )
    ).project
    enable_project(context=context, project=other)
    # what the refresh would repair on the first project
    dao.create_user_project(project_id=t.project.project_id, username='stale_marker')
    dao.delete_user_project(project_id=t.project.project_id, username=t.user.username)

    projects = context.projects.projects_dao.table
    real_scan = projects.scan

    class FirstProjectFirst:
        # the first project is read before the one whose group cannot be read
        def scan(self, **request):
            page = real_scan(**request)
            page['Items'].sort(key=lambda p: p['project_id'] != t.project.project_id)
            return page

    real_get_group = context.accounts.group_dao.get_group

    def get_group(name):
        if name == failing.name:
            raise ClientError({'Error': {'Code': 'InternalServerError'}}, 'GetItem')
        return real_get_group(name)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(context.projects.projects_dao, 'table', FirstProjectFirst())
            patch.setattr(context.accounts.group_dao, 'get_group', get_group)
            with pytest.raises(ClientError):
                dao.refresh_all_projects()
        assert dao.is_user_in_project('stale_marker', t.project.project_id)
        assert not dao.is_user_in_project(t.user.username, t.project.project_id)
    finally:
        dao.delete_user_project(
            project_id=t.project.project_id, username='stale_marker'
        )
        dao.create_user_project(
            project_id=t.project.project_id, username=t.user.username
        )
        context.projects.projects_dao.delete_project(other.project_id)
        context.accounts.delete_group(failing.name, force=True)
