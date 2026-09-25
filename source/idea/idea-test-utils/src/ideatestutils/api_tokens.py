"""Token API fixtures with in-memory AWS responses."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import secrets

from botocore.exceptions import ClientError

from ideadatamodel import CreateApiTokenRequest
from ideasdk.api import ApiInvocationContext
from ideasdk.auth import TokenService, TokenServiceOptions
from ideasdk.utils import GroupNameHelper


def api_token_environment(module_id='cluster-manager'):
    context = Mock()
    context.cluster_name.return_value = secrets.token_hex(8)
    context.module_id.return_value = module_id
    context.config().get_list.return_value = ['actor', 'auth_type']
    context.config().get_bool.return_value = True
    context.config().get_string.return_value = 'pool'
    table = Mock()
    table.name = f'{context.cluster_name()}.cluster-manager.api-tokens'
    users = Mock()
    users.get_item.return_value = {
        'Item': {'username': 'user-a', 'enabled': True, 'created_on': 1000}
    }
    context.aws().dynamodb_table().Table.side_effect = (
        lambda name: users if name.endswith('.accounts.users') else table
    )
    groups = context.aws().cognito_idp().admin_list_groups_for_user
    groups.return_value = {'Groups': [{'GroupName': f'{module_id}-users-module-group'}]}
    rows = {}

    def put(Item, **kwargs):
        rows[Item['token_id']] = deepcopy(Item)

    def get(Key, **kwargs):
        row = rows.get(Key['token_id'])
        return {'Item': deepcopy(row)} if row else {}

    def query(IndexName, KeyConditionExpression, **kwargs):
        value = KeyConditionExpression.get_expression()['values'][1]
        return {
            'Items': [deepcopy(row) for row in rows.values() if row[IndexName] == value]
        }

    def update(Key, ExpressionAttributeValues, **kwargs):
        row = rows.get(Key['token_id'])
        if (
            not row
            or (row.get('last_used_on') or 0) > ExpressionAttributeValues[':cutoff']
        ):
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'
            )
        row['last_used_on'] = ExpressionAttributeValues[':now']

    table.put_item.side_effect = put
    table.get_item.side_effect = get
    table.query.side_effect = query
    table.update_item.side_effect = update
    table.delete_item.side_effect = lambda Key: rows.pop(Key['token_id'], None)
    service = TokenService(
        context,
        TokenServiceOptions(
            cognito_user_pool_provider_url='https://example.invalid/pool',
            cognito_user_pool_domain_url='https://example.invalid',
            administrators_group_name='administrators',
            managers_group_name='managers',
        ),
    )
    context.token_service = service
    created = service.create_api_token(
        'user-a', CreateApiTokenRequest(name='automation', expires_in_days=30)
    )
    return SimpleNamespace(
        context=context,
        service=service,
        table=table,
        users=users,
        groups=groups,
        rows=rows,
        created=created,
    )


def api_token_invocation(env, namespace, payload=None, token=None):
    return ApiInvocationContext(
        context=env.context,
        request={'header': {'namespace': namespace}, 'payload': payload or {}},
        invocation_source='http',
        group_name_helper=GroupNameHelper(env.context),
        logger=env.context.logger(),
        token={'token_type': 'Bearer', 'token': token or env.created.token},
        token_service=env.service,
    )
