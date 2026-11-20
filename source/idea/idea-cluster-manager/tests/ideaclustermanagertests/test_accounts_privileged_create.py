from unittest.mock import Mock

import pytest

from ideaclustermanager import AppContext
from ideaclustermanager.app.api.accounts_api import AccountsAPI
from ideadatamodel import errorcodes, exceptions
from ideasdk.api import ApiInvocationContext
from ideasdk.auth.token_service import ApiAuthorization, ApiAuthorizationType
from ideasdk.utils import GroupNameHelper


def call(context: AppContext, role, namespace, payload):
    names = GroupNameHelper(context)
    tokens = Mock()
    tokens.decode_token.return_value = {}
    tokens.get_authorization.return_value = ApiAuthorization(
        type=role,
        username=f'{role.value}-caller',
        groups=[
            names.get_cluster_administrators_group()
            if role == ApiAuthorizationType.ADMINISTRATOR
            else names.get_cluster_managers_group()
        ],
    )
    invocation = ApiInvocationContext(
        context=context,
        request={'header': {'namespace': namespace}, 'payload': payload},
        invocation_source='http',
        group_name_helper=names,
        logger=Mock(),
        token={'token_type': 'Bearer', 'token': 'marker'},
        token_service=tokens,
    )
    AccountsAPI(context).invoke(invocation)
    return invocation.response


MANAGER = ApiAuthorizationType.MANAGER
ADMINISTRATOR = ApiAuthorizationType.ADMINISTRATOR


def new_user(username, **fields):
    return {
        'user': {'username': username, 'email': f'{username}@example.com', **fields}
    }


@pytest.mark.parametrize(
    'group',
    [
        'get_cluster_administrators_group',
        'get_cluster_managers_group',
        'get_cluster_operations_leads_group',
    ],
)
def test_a_manager_cannot_create_an_account_inside_a_privileged_group(
    context: AppContext, group
):
    name = getattr(GroupNameHelper(context), group)()
    with pytest.raises(exceptions.SocaException) as refused:
        call(
            context,
            MANAGER,
            'Accounts.CreateUser',
            new_user('privcreate1', additional_groups=[name]),
        )
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    # refused before anything was made
    assert context.accounts.user_dao.get_user('privcreate1') is None


def test_a_manager_cannot_create_a_group_under_a_privileged_name(context: AppContext):
    name = GroupNameHelper(context).get_cluster_administrators_group()
    with pytest.raises(exceptions.SocaException) as refused:
        call(
            context,
            MANAGER,
            'Accounts.CreateGroup',
            {'group': {'name': name, 'group_type': 'cluster'}},
        )
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert context.accounts.group_dao.get_group(name) is None


@pytest.mark.parametrize(
    'namespace, payload',
    [
        ('Accounts.CreateUser', new_user('privcreate2', uid=7001)),
        ('Accounts.CreateUser', new_user('privcreate2', gid=7001)),
        (
            'Accounts.CreateGroup',
            {'group': {'name': 'privgroup2', 'group_type': 'project', 'gid': 7001}},
        ),
    ],
)
def test_only_an_administrator_names_a_uid_or_gid(
    context: AppContext, namespace, payload
):
    with pytest.raises(exceptions.SocaException) as refused:
        call(context, MANAGER, namespace, payload)
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert context.accounts.user_dao.get_user('privcreate2') is None
    assert context.accounts.group_dao.get_group('privgroup2') is None


def test_a_manager_still_creates_an_ordinary_account(context: AppContext):
    created = call(context, MANAGER, 'Accounts.CreateUser', new_user('privcreate3'))
    user = created['payload']['user']
    assert user['username'] == 'privcreate3'
    assert user['uid'] >= 5000 and user['gid'] >= 5000
    context.accounts.delete_user('privcreate3')


@pytest.mark.parametrize(
    'field, value, reason',
    [
        ('uid', 0, 'reserved for the system'),
        ('uid', 999, 'reserved for the system'),
        ('gid', 65534, 'reserved for the system'),
        ('uid', 'existing', 'already in use'),
        ('gid', 'existing', 'already in use'),
    ],
)
def test_an_administrator_named_id_is_checked(
    context: AppContext, field, value, reason
):
    existing = context.accounts.create_user(user=_user('privcreate4'))
    if value == 'existing':
        value = getattr(existing, field)
    try:
        with pytest.raises(exceptions.SocaException) as refused:
            call(
                context,
                ADMINISTRATOR,
                'Accounts.CreateUser',
                new_user('privcreate5', **{field: value}),
            )
        assert refused.value.error_code == errorcodes.INVALID_PARAMS
        assert reason in refused.value.message
        assert context.accounts.user_dao.get_user('privcreate5') is None
    finally:
        context.accounts.delete_user('privcreate4')


def _user(username):
    from ideadatamodel import User

    return User(username=username, email=f'{username}@example.com')


def test_a_manager_names_the_gid_of_a_read_only_directory_group(
    context: AppContext, monkeypatch
):
    monkeypatch.setattr(context.ldap_client, 'is_readonly', lambda: True)
    monkeypatch.setattr(
        context.ldap_client, 'get_group', lambda group_name, **_: {'gid': 7006}
    )
    created = call(
        context,
        MANAGER,
        'Accounts.CreateGroup',
        {
            'group': {
                'name': 'privgroup6',
                'group_type': 'project',
                'ds_name': 'privgroup6',
                'gid': 7006,
            }
        },
    )
    assert created['payload']['group']['gid'] == 7006
    context.accounts.group_dao.delete_group('privgroup6')


@pytest.mark.parametrize(
    'namespace, payload, created',
    [
        ('Accounts.CreateUser', new_user('privcreate7', uid=2001, gid=2001), 'user'),
        (
            'Accounts.CreateGroup',
            {'group': {'name': 'privgroup7', 'group_type': 'project', 'gid': 2002}},
            'group',
        ),
    ],
)
def test_an_administrator_names_an_id_below_the_sequence_start(
    context: AppContext, namespace, payload, created
):
    # the sequence never skips an id in use: below its start is where named ids go
    answer = call(context, ADMINISTRATOR, namespace, payload)
    try:
        record = answer['payload'][created]
        assert record['gid'] in (2001, 2002)
    finally:
        if created == 'user':
            context.accounts.delete_user('privcreate7')
        else:
            context.accounts.group_dao.delete_group('privgroup7')


def test_a_read_only_directory_group_keeps_its_own_low_gid_at_start_up(
    context: AppContext, monkeypatch
):
    accounts = context.accounts
    monkeypatch.setattr(context.ldap_client, 'is_readonly', lambda: True)
    monkeypatch.setattr(accounts, '_get_ds_group_name', lambda groupname: groupname)
    monkeypatch.setattr(
        accounts, '_get_gid_from_existing_ldap_group', lambda *_, **__: 500
    )
    accounts.provision_operations_leads_group('privleads8')
    try:
        assert accounts.group_dao.get_group('privleads8')['gid'] == 500
        assert accounts.operations_leads_configuration_status['status'] == 'ready'
    finally:
        accounts.group_dao.delete_group('privleads8')


@pytest.fixture
def two_users(context: AppContext, monkeypatch):
    pool = Mock()
    monkeypatch.setattr(context.accounts, 'user_pool', pool)
    made = [context.accounts.create_user(user=_user(f'modify{n}')) for n in (1, 2)]
    yield context.accounts, pool, made
    for user in made:
        context.accounts.delete_user(user.username)


def test_modify_user_sudo_changes_the_user_pool_group(two_users):
    from ideadatamodel import User

    accounts, pool, made = two_users
    name = made[0].username
    accounts.modify_user(User(username=name, sudo=True))
    pool.admin_add_sudo_user.assert_called_once_with(name)
    assert accounts.get_user(name).sudo is True
    accounts.modify_user(User(username=name, sudo=False))
    pool.admin_remove_sudo_user.assert_called_once_with(name)
    assert accounts.get_user(name).sudo is False
    # a repeat still reconciles the user pool's group
    accounts.remove_sudo_user(name)
    assert pool.admin_remove_sudo_user.call_count == 2


def test_modify_user_checks_a_changed_id_and_moves_the_personal_group(two_users):
    from ideadatamodel import User

    accounts, _, made = two_users
    first, second = made
    with pytest.raises(exceptions.SocaException) as refused:
        accounts.modify_user(User(username=first.username, uid=second.uid))
    assert 'already in use' in refused.value.message
    with pytest.raises(exceptions.SocaException):
        accounts.modify_user(User(username=first.username, gid=0))
    # its own ids are not "in use" by someone else
    accounts.modify_user(User(username=first.username, uid=first.uid, gid=first.gid))
    accounts.modify_user(User(username=first.username, gid=2101))
    group = accounts.group_dao.get_group(first.group_name)
    assert int(group['gid']) == 2101
    assert accounts.get_user(first.username).gid == 2101


@pytest.mark.parametrize('spelling', [' {name} ', '{upper}'])
def test_the_built_in_administrator_is_guarded_in_any_spelling(
    context: AppContext, spelling
):
    name = context.config().get_string('cluster.administrator_username', required=True)
    asked = spelling.format(name=name, upper=name.upper())
    for operation in (
        context.accounts.disable_user,
        context.accounts.delete_user,
        context.accounts.remove_sudo_user,
    ):
        with pytest.raises(exceptions.SocaException) as refused:
            operation(asked)
        assert refused.value.error_code == errorcodes.AUTH_INVALID_OPERATION


@pytest.fixture
def read_only_directory(context: AppContext, monkeypatch):
    monkeypatch.setattr(context.ldap_client, 'is_readonly', lambda: True)
    monkeypatch.setattr(
        context.ldap_client,
        'get_group',
        lambda group_name, **_: (
            {'gid': {'dirgroup9': 7009, 'lowgroup9': 500}.get(group_name)}
            if group_name in ('dirgroup9', 'lowgroup9')
            else None
        ),
    )
    monkeypatch.setattr(
        context.ldap_client,
        'get_user',
        lambda username, **_: (
            {'username': username, 'uid': 7101, 'gid': 7102}
            if username == 'diruser9'
            else None
        ),
    )
    return context


@pytest.mark.parametrize(
    'gid, answer',
    [(7010, 'differs from the gid of directory group'), (None, 7009), (7009, 7009)],
)
def test_a_read_only_directory_group_takes_its_gid_from_the_directory(
    read_only_directory, gid, answer
):
    context = read_only_directory
    group = {'name': 'privgroup9', 'group_type': 'project', 'ds_name': 'dirgroup9'}
    if gid is not None:
        group['gid'] = gid
    try:
        if isinstance(answer, str):
            with pytest.raises(exceptions.SocaException) as refused:
                call(context, MANAGER, 'Accounts.CreateGroup', {'group': group})
            assert answer in refused.value.message
            assert context.accounts.group_dao.get_group('privgroup9') is None
        else:
            created = call(context, MANAGER, 'Accounts.CreateGroup', {'group': group})
            assert created['payload']['group']['gid'] == answer
    finally:
        context.accounts.group_dao.delete_group('privgroup9')


def test_a_read_only_directory_group_below_1000_can_be_mapped(read_only_directory):
    context = read_only_directory
    group = {'name': 'privgroup10', 'group_type': 'project', 'ds_name': 'lowgroup9'}
    try:
        created = call(context, ADMINISTRATOR, 'Accounts.CreateGroup', {'group': group})
        assert created['payload']['group']['gid'] == 500
    finally:
        context.accounts.group_dao.delete_group('privgroup10')


@pytest.mark.parametrize(
    'username, ids, refused',
    [
        ('diruser9', {'uid': 7199}, 'differs from the directory account'),
        ('nodiruser9', {'uid': 7199}, 'is not in the directory'),
    ],
)
def test_ids_named_on_a_read_only_directory_must_match_its_account(
    read_only_directory, username, ids, refused
):
    context = read_only_directory
    with pytest.raises(exceptions.SocaException) as answer:
        call(context, ADMINISTRATOR, 'Accounts.CreateUser', new_user(username, **ids))
    assert refused in answer.value.message
    assert context.accounts.user_dao.get_user(username) is None


def test_a_name_the_user_pool_holds_is_not_deleted_by_the_rollback(
    context: AppContext, monkeypatch
):
    import botocore.exceptions

    pool = Mock()
    pool.admin_create_user.side_effect = botocore.exceptions.ClientError(
        {'Error': {'Code': 'UsernameExistsException', 'Message': 'marker'}},
        'AdminCreateUser',
    )
    monkeypatch.setattr(context.accounts, 'user_pool', pool)
    with pytest.raises(exceptions.SocaException) as refused:
        context.accounts.create_user(user=_user('poolheld9'))
    assert refused.value.error_code == errorcodes.AUTH_USER_ALREADY_EXISTS
    # the portal lists no such account: the answer says where the name is held
    assert 'held in the user pool' in refused.value.message
    pool.admin_delete_user.assert_not_called()


def test_a_group_named_like_a_personal_group_is_not_adopted(context: AppContext):
    name = GroupNameHelper(context).get_user_group('adopt9')
    context.accounts.create_group(
        group=__import__('ideadatamodel').Group(
            name=name, group_type='project', title='marker'
        )
    )
    try:
        with pytest.raises(exceptions.SocaException) as refused:
            context.accounts.create_user(user=_user('adopt9'))
        assert 'is not the personal group' in refused.value.message
        assert context.accounts.user_dao.get_user('adopt9') is None
        assert context.accounts.group_dao.get_group(name)['group_type'] == 'project'
    finally:
        context.accounts.group_dao.delete_group(name)


def test_automatic_ids_skip_one_an_administrator_named_ahead(context: AppContext):
    made = [context.accounts.create_user(user=_user('ahead1'))]
    try:
        ahead = _user('ahead2')
        ahead.uid = made[0].uid + 2
        made.append(context.accounts.create_user(user=ahead))
        for name in ('ahead3', 'ahead4'):
            made.append(context.accounts.create_user(user=_user(name)))
        uids = [user.uid for user in made]
        assert len(set(uids)) == len(uids)
    finally:
        for user in made:
            context.accounts.delete_user(user.username)


def test_deleting_an_account_archives_its_home(context: AppContext, monkeypatch):
    from ideaclustermanager.app.accounts import user_home_directory

    archived = []
    monkeypatch.setattr(
        user_home_directory.UserHomeDirectory,
        'archive',
        lambda home: archived.append(home.user.username),
    )
    context.accounts.create_user(user=_user('archive9'))
    context.accounts.delete_user('archive9')
    assert archived == ['archive9']


def call_as(context: AppContext, kind, groups, namespace, payload):
    names = GroupNameHelper(context)
    tokens = Mock()
    tokens.decode_token.return_value = {}
    tokens.get_authorization.return_value = ApiAuthorization(
        type=kind, username='caller-marker', groups=groups
    )
    invocation = ApiInvocationContext(
        context=context,
        request={'header': {'namespace': namespace}, 'payload': payload},
        invocation_source='http',
        group_name_helper=names,
        logger=Mock(),
        token={'token_type': 'Bearer', 'token': 'marker'},
        token_service=tokens,
    )
    AccountsAPI(context).invoke(invocation)
    return invocation.response


def test_a_module_administrator_cannot_grant_another_modules_administration(
    context: AppContext,
):
    names = GroupNameHelper(context)
    own = names.get_module_administrators_group(module_id=context.module_id())
    other = names.get_module_administrators_group(module_id='scheduler')
    with pytest.raises(exceptions.SocaException) as refused:
        call_as(
            context,
            ApiAuthorizationType.USER,
            [own],
            'Accounts.AddUserToGroup',
            {'usernames': ['nobody9'], 'group_name': other},
        )
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    with pytest.raises(exceptions.SocaException) as refused:
        call_as(
            context,
            ApiAuthorizationType.USER,
            [own],
            'Accounts.CreateUser',
            new_user('modadmin9', additional_groups=[other]),
        )
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert context.accounts.user_dao.get_user('modadmin9') is None


@pytest.mark.parametrize(
    'namespace',
    [
        'Accounts.DisableGroup',
        'Accounts.EnableGroup',
        'Accounts.DeleteGroup',
        'Accounts.ModifyGroup',
    ],
)
def test_a_manager_cannot_change_a_privileged_group_itself(
    context: AppContext, namespace
):
    name = GroupNameHelper(context).get_cluster_managers_group()
    payload = (
        {'group': {'name': name, 'title': 'marker'}}
        if namespace == 'Accounts.ModifyGroup'
        else {'group_name': name}
    )
    with pytest.raises(exceptions.SocaException) as refused:
        call(context, MANAGER, namespace, payload)
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_the_sudoers_group_is_guarded_in_any_case(context: AppContext, monkeypatch):
    real = context.config().get_string
    monkeypatch.setattr(
        context.config(),
        'get_string',
        lambda key, *a, **k: (
            'Sudo-Marker'
            if key == 'directoryservice.sudoers.group_name'
            else real(key, *a, **k)
        ),
    )
    with pytest.raises(exceptions.SocaException) as refused:
        call(
            context,
            MANAGER,
            'Accounts.AddUserToGroup',
            {'usernames': ['nobody9'], 'group_name': 'sudo-marker'},
        )
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


@pytest.mark.parametrize('name', ['team,ou=marker', 'Team-Marker', 'ab', 'team marker'])
def test_a_group_name_outside_the_portals_set_is_refused(context: AppContext, name):
    with pytest.raises(exceptions.SocaException) as refused:
        call(
            context,
            ADMINISTRATOR,
            'Accounts.CreateGroup',
            {'group': {'name': name, 'group_type': 'project'}},
        )
    assert refused.value.error_code == errorcodes.INVALID_PARAMS
    assert context.accounts.group_dao.get_group(name) is None


@pytest.mark.parametrize('readonly', [False, True])
def test_only_an_administrator_names_a_uid_on_any_directory(
    context: AppContext, monkeypatch, readonly
):
    monkeypatch.setattr(context.ldap_client, 'is_readonly', lambda: readonly)
    monkeypatch.setattr(
        context.ldap_client,
        'get_user',
        lambda username, **_: {'username': username, 'uid': 7301, 'gid': 7302},
    )
    with pytest.raises(exceptions.SocaException) as refused:
        call(context, MANAGER, 'Accounts.CreateUser', new_user('uidpick9', uid=7301))
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert 'Only an administrator can choose a user id' in refused.value.message
    assert context.accounts.user_dao.get_user('uidpick9') is None
    created = call(
        context, ADMINISTRATOR, 'Accounts.CreateUser', new_user('uidpick9', uid=7301)
    )
    try:
        assert created['payload']['user']['uid'] == 7301
    finally:
        context.accounts.delete_user('uidpick9')


def test_a_manager_cannot_create_over_an_existing_directory_entry(
    context: AppContext, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        context.accounts.task_manager,
        'send',
        lambda **kwargs: sent.append(kwargs['task_name']),
    )
    monkeypatch.setattr(
        context.ldap_client,
        'is_existing_user',
        lambda username: username == 'dirheld9',
    )
    assert not context.ldap_client.is_readonly()
    with pytest.raises(exceptions.SocaException) as refused:
        call(context, MANAGER, 'Accounts.CreateUser', new_user('dirheld9'))
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert context.accounts.user_dao.get_user('dirheld9') is None
    assert sent == []
    # an administrator may still adopt the entry, as a reinstall onto a kept directory does
    try:
        created = call(
            context, ADMINISTRATOR, 'Accounts.CreateUser', new_user('dirheld9')
        )
        assert created['payload']['user']['username'] == 'dirheld9'
    finally:
        context.accounts.delete_user('dirheld9')


@pytest.mark.parametrize(
    'username, uid, gid',
    [('diruser9', 7101, 7102), ('nodiruser9', None, None)],
)
def test_a_read_only_directory_account_takes_its_ids_from_the_directory(
    read_only_directory, monkeypatch, username, uid, gid
):
    context = read_only_directory
    homes = []
    monkeypatch.setattr(
        context.accounts.task_manager,
        'send',
        lambda **kwargs: (
            homes.append(kwargs['payload'])
            if kwargs['task_name'] == 'accounts.create-home-directory'
            else None
        ),
    )
    try:
        created = call(context, MANAGER, 'Accounts.CreateUser', new_user(username))
        user = created['payload']['user']
        row = context.accounts.user_dao.get_user(username)
        group = context.accounts.group_dao.get_group(user['group_name'])
        if uid is None:
            # absent from the directory: ids from the sequence, as before
            assert user['uid'] >= 5000 and user['gid'] >= 5000
        else:
            assert (user['uid'], user['gid']) == (uid, gid)
        assert (int(row['uid']), int(row['gid'])) == (user['uid'], user['gid'])
        assert int(group['gid']) == user['gid']
        assert homes == [{'username': username}]
    finally:
        context.accounts.delete_user(username)


def test_a_read_only_directory_keeps_a_deleted_accounts_home(
    context: AppContext, monkeypatch
):
    from ideaclustermanager.app.accounts import user_home_directory

    archived = []
    monkeypatch.setattr(
        user_home_directory.UserHomeDirectory,
        'archive',
        lambda home: archived.append(home.user.username),
    )
    context.accounts.create_user(user=_user('archive10'))
    monkeypatch.setattr(context.ldap_client, 'is_readonly', lambda: True)
    context.accounts.delete_user('archive10')
    assert archived == []


def test_a_demotion_repeated_after_the_user_pool_failed_completes(two_users):
    from ideadatamodel import User

    accounts, pool, made = two_users
    name = made[0].username
    accounts.add_sudo_user(name)
    pool.admin_remove_sudo_user.side_effect = [RuntimeError('throttled'), None]
    with pytest.raises(RuntimeError):
        accounts.modify_user(User(username=name, sudo=False))
    # the record already says not sudo; the user pool group must still be left
    accounts.modify_user(User(username=name, sudo=False))
    assert pool.admin_remove_sudo_user.call_count == 2
    assert accounts.get_user(name).sudo is False


def test_ending_sessions_comes_before_deleting_tokens_everywhere(
    two_users, monkeypatch
):
    accounts, pool, made = two_users
    name = made[0].username
    order = []
    monkeypatch.setattr(accounts, '_end_sessions', lambda u: order.append('cutoff'))
    monkeypatch.setattr(
        accounts, 'revoke_user_api_tokens', lambda u: order.append('delete')
    )
    for operation in (accounts.global_sign_out, accounts.reset_password):
        order.clear()
        operation(name)
        assert order == ['cutoff', 'delete']
    order.clear()
    accounts.disable_user(name)
    assert order[:2] == ['cutoff', 'delete']
