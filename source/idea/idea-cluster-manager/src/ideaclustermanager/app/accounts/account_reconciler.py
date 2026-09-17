"""Compare account state before applying any changes, under one cluster-wide lock."""

import math
import ldap
import threading
from copy import copy
from urllib.parse import quote

import arrow
import requests
from pydantic import StrictBool

from ideadatamodel import ListUsersRequest, SocaPaginator, SocaBaseModel, constants
from ideaclustermanager.app.accounts.reconcile_settings import approved_okta_origin
from ideasdk.metrics import BaseMetrics
from ideasdk.service import SocaService


class ReconcileUsersRequest(SocaBaseModel):
    dry_run: StrictBool = True
    override_max_disable_fraction: StrictBool = False


class AccountReconcileMetrics(BaseMetrics):
    def publish_report(self, report):
        for name in (
            'checked',
            'disabled',
            'reenabled',
            'missing',
            'errors',
            'refused',
        ):
            self.count(MetricName=f'accounts.reconcile.{name}', Value=report[name])


class AccountReconciler(SocaService):
    def __init__(self, context):
        super().__init__(context)
        self.context = context
        self.logger = context.logger('account-reconcile')
        self._exit = threading.Event()
        self._local_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._loop, name='account-reconcile', daemon=True
        )

    def service_id(self):
        return 'account-reconcile'

    def key(self, suffix):
        return f'{self.context.module_id()}.accounts.reconcile.{suffix}'

    def interval_seconds(self):
        minutes = self.context.config().get_int(self.key('interval_minutes'), 60)
        return max(1, min(1440, minutes)) * 60

    def start(self):
        self._thread.start()

    def stop(self):
        self._exit.set()
        if self._thread.is_alive():
            self._thread.join()

    def _loop(self):
        while not self._exit.is_set():
            try:
                if self.context.config().get_bool(self.key('enabled'), False):
                    self.run_once(periodic=True)
            except Exception:
                self.logger.exception('account reconciliation failed')
            self._exit.wait(60)

    def protected(self, user):
        username = user.username.lower()
        ldap_client = self.context.accounts.ldap_client
        root_username = getattr(ldap_client, 'ldap_root_username', '') or ''
        return self.context.accounts.is_cluster_administrator(
            user.username
        ) or username in {
            constants.IDEA_SERVICE_ACCOUNT.lower(),
            root_username.lower(),
            'root',
            'admin',
            'administrator',
            'ec2-user',
            'ssm-user',
            'centos',
        }

    def upstream(self, user, metadata=None):
        metadata = metadata if metadata is not None else {}
        sources = metadata.get('reconcile_sources', [])
        config = self.context.config()
        states = {}
        if config.get_string('directoryservice.provider') in (
            'aws_managed_activedirectory',
            'activedirectory',
        ):
            record = self.context.accounts.ldap_client.get_reconcile_user(
                user.username, user.email, metadata.get('directory_identity')
            )
            if record is None:
                states['directory'] = 'missing'
            else:
                metadata['directory_identity'] = record['directory_identity']
                control = record.get('user_account_control')
                if control is None:
                    raise ValueError('directory did not return userAccountControl')
                states['directory'] = 'disabled' if int(control) & 2 else 'enabled'

        org = config.get_string(self.key('okta.org_url'))
        secret = config.get_string(self.key('okta.api_token_secret_arn'))
        if bool(org) != bool(secret):
            raise ValueError('both Okta settings are required')
        if org and secret:
            org = approved_okta_origin(config, self.context.module_id(), org)
            token = config.get_secret(
                self.key('okta.api_token_secret_arn'), required=True
            )
            if not token:
                raise ValueError('Okta token is empty')
            response = requests.get(
                f'{org.rstrip("/")}/api/v1/users/{quote(user.email or user.username, safe="")}',
                headers={
                    'Authorization': f'SSWS {token}',
                    'Accept': 'application/json',
                },
                timeout=15,
                allow_redirects=False,
            )
            if response.status_code == 404:
                states['okta'] = 'missing'
            else:
                if response.status_code != 200:
                    raise ValueError('Okta lookup failed')
                status = response.json().get('status')
                if status in ('DEPROVISIONED', 'SUSPENDED', 'DEACTIVATED'):
                    states['okta'] = 'disabled'
                elif status == 'ACTIVE':
                    states['okta'] = 'enabled'
                elif status in (
                    'STAGED',
                    'PROVISIONED',
                    'RECOVERY',
                    'PASSWORD_EXPIRED',
                    'LOCKED_OUT',
                ):
                    states['okta'] = 'unavailable'
                else:
                    raise ValueError('unknown Okta status')

        # Only a recorded external revocation explains a disabled Cognito mirror.
        if config.get_bool(self.key('check_cognito'), False) and (
            user.enabled or not sources or 'cognito' in sources or not states
        ):
            record = self.context.accounts.user_pool.admin_get_user(
                user.username, use_cache=False
            )
            if record is None:
                states['cognito'] = 'missing'
            elif record.Enabled is False:
                states['cognito'] = 'disabled'
            elif record.Enabled is True:
                states['cognito'] = 'enabled'
            else:
                raise ValueError('Cognito did not return Enabled')
        return states

    def assert_lease(self):
        self.context.distributed_lock().assert_held(
            key=f'{self.context.module_id()}-account-reconcile'
        )

    def _reconcile(self, dry_run, override_max_disable_fraction=False):
        report = dict(
            dry_run=dry_run,
            checked=0,
            disabled=0,
            reenabled=0,
            missing=0,
            errors=0,
            refused=0,
            changes=[],
            skipped=[],
        )
        config = self.context.config()
        try:
            fraction = float(
                config.get_string(self.key('max_disable_fraction'), '0.25')
            )
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                raise ValueError('invalid safety cap')
            users = []
            cursor = None
            while True:
                page = self.context.accounts.list_users(
                    ListUsersRequest(paginator=SocaPaginator(cursor=cursor))
                )
                users.extend(page.listing or [])
                cursor = page.paginator.cursor if page.paginator else None
                if not cursor:
                    break
            enabled = 0
            identities = {}
            for user in users:
                self.assert_lease()
                if self.protected(user):
                    report['skipped'].append(user.username)
                    continue
                report['checked'] += 1
                try:
                    metadata = dict(
                        self.context.accounts.user_dao.get_user(user.username) or {}
                    )
                    # Inventory scans are eventually consistent. Use the current
                    # DAO state for decisions and the safety-cap denominator.
                    user = copy(user)
                    user.enabled = metadata.get('enabled', user.enabled)
                    enabled += int(user.enabled is True)
                    sources = metadata.get('reconcile_sources', [])
                    if metadata.get('disable_pending'):
                        # This finishes an already committed revocation, independent of upstream health.
                        if not dry_run:
                            self.assert_lease()
                            self.context.accounts.disable_user(
                                user.username,
                                reconcile_sources=sources,
                            )
                        continue
                    if not user.enabled and (
                        not sources or not config.get_bool(self.key('reenable'), True)
                    ):
                        continue
                    previous_identity = metadata.get('directory_identity')
                    states = self.upstream(user, metadata)
                    if metadata.get('directory_identity') != previous_identity:
                        identities[user.username] = metadata['directory_identity']
                except Exception as error:
                    # Exception strings from HTTP clients can contain credentials or URLs.
                    result_code = None
                    if isinstance(error, ldap.LDAPError) and error.args:
                        details = error.args[0]
                        if (
                            isinstance(details, dict)
                            and type(details.get('result')) is int
                        ):
                            result_code = details['result']
                    report['errors'] += 1
                    self.logger.warning(
                        f'upstream account read failed for {user.username}: '
                        f'exception_type={type(error).__name__}, ldap_result_code={result_code}'
                    )
                    continue
                missing = 'missing' in states.values()
                report['missing'] += int(missing)
                action = None
                if user.enabled and (missing or 'disabled' in states.values()):
                    action = 'disable'
                elif (
                    not user.enabled
                    and sources
                    and all(states.get(source) == 'enabled' for source in sources)
                    and states
                    and all(s == 'enabled' for s in states.values())
                ):
                    action = 'enable'
                if action:
                    report['changes'].append(
                        dict(username=user.username, action=action, upstream=states)
                    )
            proposed = sum(
                change['action'] == 'disable' for change in report['changes']
            )
            report.update(
                would_disable=proposed,
                would_reenable=sum(c['action'] == 'enable' for c in report['changes']),
                eligible_enabled=enabled,
                max_disable_fraction=fraction,
            )
            if report['errors'] or (
                not override_max_disable_fraction
                and enabled
                and proposed / enabled > fraction
            ):
                report['refused'] = 1
                report['reason'] = (
                    'upstream read failed'
                    if report['errors']
                    else 'max_disable_fraction exceeded'
                )
            if not dry_run and not report['refused']:
                for username, identity in identities.items():
                    self.assert_lease()
                    self.context.accounts.user_dao.update_user(
                        {
                            'username': username,
                            'directory_identity': identity,
                        }
                    )
                for change in report['changes']:
                    self.assert_lease()
                    try:
                        if change['action'] == 'disable':
                            self.context.accounts.disable_user(
                                change['username'],
                                preserve_directory=True,
                                reconcile_sources=[
                                    source
                                    for source, state in change['upstream'].items()
                                    if state in ('disabled', 'missing')
                                ],
                            )
                            report['disabled'] += 1
                        else:
                            self.context.accounts.enable_user(change['username'])
                            report['reenabled'] += 1
                        change['applied'] = True
                    except Exception:
                        report['errors'] += 1
                        change['applied'] = False
                        self.logger.warning(
                            f'account change failed for {change["username"]}'
                        )
        except Exception:
            report['errors'] += 1
            report['refused'] = 1
            report['reason'] = 'account inventory or configuration read failed'
        if report['refused']:
            self.logger.warning(f'account reconciliation refused: {report["reason"]}')
        self.logger.info(f'account reconciliation report: {report}')
        AccountReconcileMetrics(self.context).publish_report(report)
        return report

    def run_once(
        self, dry_run=True, periodic=False, override_max_disable_fraction=False
    ):
        with self._local_lock:
            key = f'{self.context.module_id()}-account-reconcile'
            self.context.distributed_lock().acquire(key=key)
            try:
                config = self.context.config()
                checkpoint = self.key('last_completed')
                if periodic:
                    entry = config.db.cluster_settings_table.get_item(
                        Key={'key': checkpoint},
                        ConsistentRead=True,
                    ).get('Item', {})
                    if (
                        arrow.utcnow().timestamp() - float(entry.get('value', 0))
                        < self.interval_seconds()
                    ):
                        return {'skipped': 'interval'}
                    dry_run = config.get_bool(self.key('dry_run'), True)
                report = self._reconcile(
                    dry_run,
                    override_max_disable_fraction=override_max_disable_fraction
                    and not periodic,
                )
                self.assert_lease()
                if periodic:
                    config.db.set_config_entry(checkpoint, arrow.utcnow().timestamp())
                return report
            finally:
                self.context.distributed_lock().release(key=key)
