# Daily configuration checks

About once a day the cluster manager leader reads the settings that must hold on every cluster and reports any that differ. It changes nothing. Datadog receives a gauge for each check, then a log line starting `IDEA_DRIFT_CHECK`. Set `metrics.health.drift_checks.enabled` to `false` to stop them, or `metrics.health.enabled` with the other [health collectors](cluster-health-metrics.md); they are read every five minutes, and a key that is absent reads as on. The checks run only when `metrics.provider` is `dogstatsd`.

`idea.health.drift.state`, tagged `idea_cluster`, `check` and `scope`, is `0` ok, `1` unknown, `2` drift. Unknown means the read failed or the answer lacked the field, which says nothing about the setting, so it is never reported as ok. `scope` is the shared-storage name for file server checks and `cluster` otherwise. Each group of checks has 60 seconds. A group that overruns reports `timeout`, is left behind, and reports `stuck` until it returns, with no second copy started.

## What this cluster can tell about itself

| Check | Ok when | Read |
| --- | --- | --- |
| `ontap_cifs_session_security` | `session-security-for-ad-ldap` is `sign` or `seal` on the CIFS server | `/api/protocols/cifs/services` |
| `ontap_ldap_session_security` | Session security is `sign` or `seal` on the LDAP client | `/api/name-services/ldap` |
| `ontap_dns_domain_controllers` | Every SVM DNS server is a domain controller of the directory | `/api/name-services/dns`, `ds:DescribeDirectories` |
| `ontap_cifs_aes` | AES is on for Kerberos to the directory | `/api/protocols/cifs/services` |
| `ontap_secure_ddns` | Dynamic DNS is on and secure | `/api/name-services/dns` |
| `ontap_machine_account` | The CIFS server is enabled and ONTAP's LDAP client is up. This is ONTAP's view of its machine account: the directory's own record is not read | CIFS and LDAP above |
| `ad_agent_bind` | The directory accepts the password in the AD automation secret, as it is now. One bind; the password is never logged. AD automation clusters only | Secrets Manager, LDAP |
| `ssh_client_ip` | `cluster.network.ssh_client_ip` is not empty | Settings |
| `values_owned_settings` | Settings that `values.yml` overwrites on each upgrade match the copy in the cluster bucket. Drift here means the next upgrade changes that setting | `values/values.yml` in the cluster bucket, only those keys |
| `settings_must_match` | Keys the release fixes hold the template's value. Today, the desktop and scheduler provisioning timeouts. The log line names any that differ | Settings |

A cluster with no file server on ONTAP has no `ontap_*` checks. A file server with no metrics login reports `no_login`.

ONTAP reads use the storage metrics login, and read configuration and volume names only: never a file or a user. The login's role needs these paths added once per SVM, from `fsxadmin`. Until then those checks read `unknown` with `forbidden` in the log, and nothing else is affected. Without `/api/svm/svms` the CIFS read answers with no server even when one is joined, so the CIFS checks and `ontap_machine_account` read `unknown`, never drift. With it, an SVM that has no CIFS server reads `drift` when its shared-storage entry has `svm.smb_dns` (the cluster mounts it over SMB) and `ok` when it does not (NFS only):

```bash
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/name-services/ldap -access readonly
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/name-services/dns -access readonly
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/protocols/cifs/services -access readonly
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/svm/svms -access readonly
```

## What only Datadog can compare

A cluster cannot see the others, so what must agree between them is published as a value and compared in Datadog. `idea.health.drift.value` is `1`, tagged `check`, `setting`, `scope` and `current`, the value itself. The state of these checks only says whether they could be read.

| `check` | `setting` | `current` |
| --- | --- | --- |
| `ssh_client_ip` | `digest`, `count` | A hash of the sorted addresses, so they stay out of the tag, and how many |
| `bastion_settings` | `public`, `cloudwatch_logs`, `desired_count` | The setting |
| `fsx_parity` | `deployment_type`, `storage_type`, `backup_retention_days`, `disk_iops_mode`, `ha_pairs` | From `fsx:DescribeFileSystems`. Sizes are left out: they are meant to differ |
| `ontap_analytics` | `state` | `on`, `off`, or `mixed` across the data volumes |

To see which clusters disagree on a setting, in the last day:

```text
max:idea.health.drift.value{check:fsx_parity,setting:backup_retention_days} by {idea_cluster,current}
```

## Alert

Two definitions, to create in Datadog after the release is on a cluster. Add any tags your organisation requires on a monitor, such as `team`, or Datadog refuses them. The checks run every 23 hours, so a one day window always holds a reading, and a fix clears at the next run.

```json
{
  "name": "[IDEA] {{idea_cluster.name}} {{check.name}} drifted on {{scope.name}}",
  "type": "query alert",
  "query": "max(last_1d):max:idea.health.drift.state{*} by {idea_cluster,check,scope} >= 2",
  "message": "A setting that must hold on every cluster does not on {{idea_cluster.name}}. Search logs for IDEA_DRIFT_CHECK check={{check.name}}.",
  "options": {"thresholds": {"critical": 2}, "notify_no_data": true, "no_data_timeframe": 2880}
}
```

```json
{
  "name": "[IDEA] {{idea_cluster.name}} {{check.name}} not ok for two days on {{scope.name}}",
  "type": "query alert",
  "query": "min(last_2d):max:idea.health.drift.state{*} by {idea_cluster,check,scope} >= 1",
  "message": "This check has not read ok for two days. If it reads unknown it would not show drift either. Search logs for IDEA_DRIFT_CHECK check={{check.name}}.",
  "options": {"thresholds": {"critical": 1}}
}
```
