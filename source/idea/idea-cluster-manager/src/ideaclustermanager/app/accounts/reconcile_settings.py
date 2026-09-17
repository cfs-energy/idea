"""Token destinations are approved through deployment configuration, never the portal."""

from urllib.parse import urlparse


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
