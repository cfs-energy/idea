# Reconcile account state

Cluster-manager can detect IDEA users disabled or removed upstream. Periodic reconciliation is off by default. The administrator-only `Accounts.ReconcileUsers` API defaults to a dry run and shares the periodic service's function. Administrators configure policy under **Administration → Settings → Account synchronization** and inspect or start runs under **Administration → People and access → Reconciliation runs**.

## Sources and actions

For `aws_managed_activedirectory` and `activedirectory`, the existing LDAP client reads `userAccountControl`; bit `0x2` means disabled. On the first successful applied scan, a unique email match (or username when email has no match) establishes a persistent AD objectGUID mapping. Subsequent scans use that identity, including after username changes. A successful identity lookup with no object means missing. No match by either email or username is also missing. An ambiguous match is a read error. LDAP failures and absent attributes are errors, never missing users. OpenLDAP is not an AD account-state source.

Optionally, `check_cognito` reads fresh Cognito `Enabled` state through the existing user pool client, for native and federation shadow users. Cognito does not automatically reflect external SAML or OIDC account state. For Okta, configure both an HTTPS org URL and a Secrets Manager ARN containing the raw API token. IDEA email (username if absent) is looked up at `/api/v1/users/{login}`. `DEPROVISIONED`, `SUSPENDED`, and `DEACTIVATED` mean disabled; HTTP 404 means missing. Other HTTP errors count toward the read-error cap. Only `ACTIVE` permits restoration. Other federation providers need their own upstream integration; checking Cognito alone is insufficient.

Disabled or missing accounts go through `AccountsService.disable_user`: Cognito, IDEA user and personal group, mirror synchronization, and the existing desktop event. Reconciliation never writes back to authoritative AD, including managed AD. A pending cleanup marker is stored with local revocation and cleared only after all disable effects succeed; later applied scans retry incomplete cleanup even when restoration is off. The controller clears all seven session schedules and stops sessions, preserving their disks. Queued resume events also check the owner before restarting. Queue authentication still uses the existing controller role ID.

The scheduler rejects new API submissions and direct PBS submissions from disabled owners. Its reconciliation sweep (normally every 60 seconds) removes queued, held and waiting jobs and provisioning entries. Provisioning also checks account state before allocating capacity. **Running jobs are left to finish.** The sweep re-reads PBS state before deletion. PBS dispatch and deletion are separate operations, so close scheduling first if bulk offboarding requires a strict boundary. Stopped desktop disks still cost money; existing running jobs incur compute charges until completion.

Restoration is on by default (`reenable=true`): only users with recorded reconciliation disable provenance are restored, after the original revoking sources and all configured external sources report enabled. Cognito's disabled mirror is ignored only when the recorded revocation came exclusively from external sources. Cognito-originated revocations require explicit restoration in Cognito first. Administrator disables, including pre-existing disables without provenance, are preserved. An administrator disabling an already reconciled account cancels its automatic restoration. It does not recreate schedules or jobs.

## Settings and safety

All settings are under `cluster-manager.accounts.reconcile`:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Start periodic reconciliation |
| `interval_minutes` | `60` | Minimum interval between replica runs, integer 1–1440 minutes |
| `dry_run` | `true` | Report periodic changes without applying |
| `reenable` | `true` | Allow restoration |
| `max_disable_fraction` | `0.25` | Maximum proposed disables / eligible enabled IDEA users, and maximum read errors / checked users |
| `check_cognito` | `false` | Check fresh Cognito account state |
| `okta.org_url` | unset | HTTPS Okta origin |
| `okta.api_token_secret_arn` | unset | Secrets Manager ARN containing the raw API token |
| `okta.approved_origins` | `[]` | Deployment-only list of exact HTTPS origins, including approved custom domains; not editable through the portal |

Cluster administrator, IDEA service account, directory bind account and reserved system usernames are excluded. The whole run is refused if **more than** the configured fraction would be disabled, or read errors affect more than that fraction of checked, unprotected users. An unreachable directory always refuses the run. A read error at or below the cap leaves that user unchanged; changes for users read successfully can still apply. Dry runs still report proposed changes on refusal. No new enable or disable is applied on refusal. Applied scans can still finish cleanup for previously committed revocations. The fraction must be between zero and one; `1` permits disabling all eligible enabled users and should only be used after reviewing a report.

```sh
ideactl config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> --force \
  'Key=cluster-manager.accounts.reconcile.enabled,Type=bool,Value=true' \
  'Key=cluster-manager.accounts.reconcile.interval_minutes,Type=int,Value=60' \
  'Key=cluster-manager.accounts.reconcile.dry_run,Type=bool,Value=true'
ideactl config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> --force \
  'Key=cluster-manager.accounts.reconcile.check_cognito,Type=bool,Value=true'
ideactl config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> --force \
  'Key=cluster-manager.accounts.reconcile.okta.approved_origins,Type=list<str>,Value=https://id.example.invalid' \
  'Key=cluster-manager.accounts.reconcile.okta.org_url,Type=str,Value=https://id.example.invalid' \
  'Key=cluster-manager.accounts.reconcile.okta.api_token_secret_arn,Type=str,Value=<SECRET_ARN>'
```

Portal saves notify the periodic worker immediately. Each run reads the current settings table, so a saved interval is used when deciding whether the next run is due. A run already in progress finishes with the settings it read at the start. Settings written outside the portal are picked up within a minute. Redeploy its IAM policy after adding/changing the Okta secret ARN; the conditional grant allows only `secretsmanager:GetSecretValue` on that ARN. Customer-managed encryption keys also need an appropriate decrypt grant. Never put the token in settings. Change `dry_run` to `false` after review.

## Portal settings and on-demand runs

Open **Administration → Settings → Account synchronization** as a cluster administrator to configure the periodic policy. Open **Administration → People and access → Reconciliation runs** to inspect status or start an on-demand run. Both views read the settings table when opened. Use the **Reconciliation on** switch in Settings to save on/off immediately. The switch shows the saved on/off value. **Next run at** shows **Off** when disabled, **Due now** when enabled without a checkpoint, or the last periodic checkpoint plus the saved interval; **Last run at / result** shows the most recent manual or periodic run. Status refreshes every ten seconds while the runs view is open. Older settings may have no save time, and older runs may have no stored report.

Use **Run now (dry run)** to preview changes, then **Run now (apply)** to apply them. Both use saved settings and work when periodic reconciliation is off. The last report shows proposed and applied counts, individual changes, and per-user read errors. Reopening the page restores the last report; saved reports retain the first 100 rows, with a notice if rows were omitted. The immediate API response contains the full report.

**Advanced** is collapsed by default. It contains the interval, maximum disable fraction, periodic dry run, re-enable restored users, and Cognito checks. Okta fields appear when the configured provider is named Okta or you select **Also check Okta**. Existing Okta settings select this option automatically; clearing it and saving removes both values. Use **Save reconciliation settings** to apply advanced edits. Unsaved edits are marked and are not used by Run now.

The interval must be an integer from 1 to 1440 and the fraction a number from 0 to 1. Okta fields must both be empty or both supplied: an approved HTTPS origin and a Secrets Manager secret ARN. The token is never returned to the portal. The existing editable settings keys and administrator-only write API are unchanged; checkpoints, the saved report and the last-save timestamp are read-only.

A user with a stored directory identity is looked up by that identity. Otherwise, reconciliation looks up email first, then username (`sAMAccountName`) if email has no match. No match from either lookup means missing and proposes a disable; an ambiguous match or failed read produces an error row. Warnings include the exception message, with HTTP URLs redacted.

A large-offboard refusal offers **Proceed anyway**, which repeats the scan with the same dry-run or apply mode and bypasses only the proposed-disable cap for that run. A dry-run override still applies nothing; use **Run now (apply)** and confirm again to apply. Directory outages and read errors above the cap cannot be overridden. Periodic runs always enforce both caps.

## Manual dry run and reports

From `source/idea/ideactl`, using an administrator:

```sh
node tools/e2e/api.ts --alb-host control-plane.example.invalid \
  --username cluster-admin --password-file /secure/path/admin-password \
  --namespace Accounts.ReconcileUsers --payload '{"dry_run":true}'
```

Inspect `changes` (username, action, upstream states), `refused`, `reason`, and `errors`. Apply with `{"dry_run":false}`. After reviewing a cap refusal, an administrator can explicitly send `{"dry_run":false,"override_max_disable_fraction":true}` (the override defaults to false). Calls serialize with the periodic service through a distributed lock and consistently read checkpoint. Manual calls bypass the interval, never the lock, directory-outage refusal or read-error cap. Ownership is checked during scanning, before applying each account change, and before advancing the checkpoint; an unsafe or lost lease aborts the run. Reports distinguish proposed and applied changes (`applied`, `disabled`, `reenabled`); stopping downstream resources is asynchronous.

BaseMetrics publishes counts for `accounts.reconcile.checked`, `disabled`, `reenabled`, `missing`, `errors`, and `refused` to DogStatsD or CloudWatch. Disabled/reenabled count applied changes, remaining zero in dry runs. Refusals are logged; read warnings include exception messages with HTTP URLs redacted.

## Live proof

The `account-reconcile` proof-matrix check needs a disposable writable AD user OU, LDAP command-line tools, an administrator API login and a desktop request. Without writable directory access it reports **NOT RUN**. It creates a directory and IDEA user, witnesses a READY desktop, disables the object over LDAP, runs the API, waits for IDEA disabled and desktop STOPPED, and attempts cleanup. It does not raise the safety cap. See [the harness instructions](../../../source/idea/ideactl/tools/e2e/README.md).

A live deployment must still prove directory permissions and identity mapping, Okta secret/IAM access if configured, replica/checkpoint behavior, metric delivery, authenticated event delivery, EC2 stopping with no schedule restart, queued PBS deletion and submission rejection, and completion of pre-existing running jobs. Unit tests do not prove these integrations.
