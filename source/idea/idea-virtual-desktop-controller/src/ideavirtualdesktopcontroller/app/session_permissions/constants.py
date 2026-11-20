import re

SESSION_PERMISSIONS_DB_HASH_KEY = 'idea_session_id'
SESSION_PERMISSIONS_DB_RANGE_KEY = 'actor_name'
SESSION_PERMISSIONS_DB_IDEA_SESSION_OWNER_KEY = 'idea_session_owner'
SESSION_PERMISSIONS_DB_IDEA_SESSION_BASE_OS_KEY = 'idea_session_base_os'
SESSION_PERMISSIONS_DB_IDEA_SESSION_INSTANCE_TYPE_KEY = 'idea_session_instance_type'
SESSION_PERMISSIONS_DB_IDEA_SESSION_STATE_KEY = 'idea_session_state'
SESSION_PERMISSIONS_DB_IDEA_SESSION_NAME_KEY = 'idea_session_name'
SESSION_PERMISSIONS_DB_IDEA_SESSION_CREATED_ON_KEY = 'idea_session_created_on'
SESSION_PERMISSIONS_DB_IDEA_SESSION_TYPE_KEY = 'idea_session_type'
SESSION_PERMISSIONS_DB_IDEA_SESSION_HIBERNATION_KEY = 'idea_session_hibernation_enabled'
SESSION_PERMISSIONS_DB_PERMISSION_PROFILE_ID_KEY = 'permission_profile_id'
SESSION_PERMISSIONS_DB_ACTOR_TYPE_KEY = 'actor_type'
SESSION_PERMISSIONS_DB_EXPIRY_DATE_KEY = 'expiry_date'
SESSION_PERMISSIONS_DB_CREATED_ON_KEY = 'created_on'
SESSION_PERMISSIONS_DB_UPDATED_ON_KEY = 'updated_on'

SESSION_PERMISSIONS_FILTER_ACTOR_KEY = SESSION_PERMISSIONS_DB_RANGE_KEY
SESSION_PERMISSIONS_FILTER_SESSION_ID_KEY = SESSION_PERMISSIONS_DB_HASH_KEY
SESSION_PERMISSIONS_FILTER_SESSION_NAME_KEY = (
    SESSION_PERMISSIONS_DB_IDEA_SESSION_NAME_KEY
)
SESSION_PERMISSIONS_FILTER_SESSION_BASE_OS_KEY = (
    SESSION_PERMISSIONS_DB_IDEA_SESSION_BASE_OS_KEY
)
SESSION_PERMISSIONS_FILTER_SESSION_INSTANCE_TYPE_KEY = (
    SESSION_PERMISSIONS_DB_IDEA_SESSION_INSTANCE_TYPE_KEY
)
SESSION_PERMISSIONS_FILTER_SESSION_STATE_KEY = (
    SESSION_PERMISSIONS_DB_IDEA_SESSION_STATE_KEY
)
SESSION_PERMISSIONS_FILTER_SESSION_CREATED_ON_KEY = (
    SESSION_PERMISSIONS_DB_IDEA_SESSION_CREATED_ON_KEY
)
SESSION_PERMISSIONS_FILTER_SESSION_TYPE_KEY = (
    SESSION_PERMISSIONS_DB_IDEA_SESSION_TYPE_KEY
)
SESSION_PERMISSIONS_FILTER_PERMISSION_PROFILE_ID_KEY = (
    SESSION_PERMISSIONS_DB_PERMISSION_PROFILE_ID_KEY
)

# user and group names as the directory gives them; one entry per line in the host's permission file
ACTOR_NAME_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._@-]{0,255}')
ACTOR_NAME_RULE = (
    'Share entries must name a user or group using letters, digits, dots, '
    'underscores, @ or hyphens.'
)

# a group share is stored under this prefix so it never shares a key with a user of
# the same name; names cannot contain ':' (ACTOR_NAME_PATTERN)
GROUP_ACTOR_PREFIX = 'group:'
# the members a group share may admit; a larger group is refused when shared
MAX_GROUP_SHARE_MEMBERS = 100


def bare_actor_name(actor_name):
    """the user or group name without the group prefix"""
    if isinstance(actor_name, str) and actor_name.startswith(GROUP_ACTOR_PREFIX):
        return actor_name[len(GROUP_ACTOR_PREFIX) :]
    return actor_name


def share_actor_key(actor_type, actor_name):
    """the stored actor_name: the bare user name, or the group name with its prefix"""
    name = bare_actor_name(actor_name)
    if str(getattr(actor_type, 'value', actor_type)) == 'GROUP':
        return f'{GROUP_ACTOR_PREFIX}{name}'
    return name


def is_group_share(permission) -> bool:
    actor_type = getattr(permission, 'actor_type', None)
    return str(getattr(actor_type, 'value', actor_type)) == 'GROUP' or str(
        getattr(permission, 'actor_name', '') or ''
    ).startswith(GROUP_ACTOR_PREFIX)
