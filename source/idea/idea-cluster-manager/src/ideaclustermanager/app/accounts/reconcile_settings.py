"""Token destinations are approved through deployment configuration, never the portal."""

from urllib.parse import urlparse


DEFAULTS = {
    'enabled': False,
    'interval_minutes': 60,
    'dry_run': True,
    'reenable': True,
    'max_disable_fraction': 0.25,
    'check_cognito': False,
    'okta.org_url': None,
    'okta.api_token_secret_arn': None,
}


def read_reconcile_settings(config, module_id, status=False):
    """Read the table, including absent rows, without the stream or portal caches."""
    keys = dict(DEFAULTS)
    if status:
        keys.update(last_completed=None, last_run=None, last_saved=None)
    settings = {}
    for key, default in keys.items():
        entry = config.db.cluster_settings_table.get_item(
            Key={'key': f'{module_id}.accounts.reconcile.{key}'},
            ConsistentRead=True,
        ).get('Item', {})
        target = settings
        parts = key.split('.')
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = entry.get('value', default)
    return settings


def approved_okta_origin(config, module_id, org):
    parsed = urlparse(org)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ('', '/')
        or parsed.port not in (None, 443)
        or any(c.isspace() for c in org)
    ):
        raise ValueError('Okta org_url must be an HTTPS origin on port 443')
    origin = f'https://{parsed.hostname}'
    approved = config.get_list(
        f'{module_id}.accounts.reconcile.okta.approved_origins', []
    )
    if origin not in approved:
        raise ValueError('Okta origin is not deployment-approved')
    return origin
