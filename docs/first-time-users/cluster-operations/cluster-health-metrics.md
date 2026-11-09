# Cluster health metrics

The cluster reports its own health as `idea.health.*` metrics over DogStatsD, so what happened stays on record for Datadog's 15 months when logs have long gone. Every point carries `idea_cluster` and no agent container tags. They flow only when `metrics.provider` is `dogstatsd`.

Each concern is a collector with its own thread, its own time limit and its own off switch. One raising an error reports it as a gauge; one that hangs is left behind and reported, and no second copy starts behind it. Collectors other than the bastion run on the leader of their module.

## Off switches

`metrics.health.enabled` stops all of them. `metrics.health.<name>.enabled` stops one: `certificates`, `ecs`, `directory`, `home_probe`, `ad_automation`, `secrets`, `storage_mounts`, `drift_checks`, `stack_expiry`, `image_bakes`, `gateway_certificate`, `pbs`, `bastion`. A key that is absent reads as true. All take effect at the collector's next cycle except `bastion`, which is read when its stack deploys.

## Is the collector itself alive?

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.collector.ok` | `collector`, `reason` | 1 when the last run finished. Otherwise 0 and why: `access_denied`, `throttled`, `unreachable`, `aws_error`, `error`, `timeout` (past its limit), `stuck` (an earlier run has not come back). No points at all means the module is not running it. |
| `idea.health.collector.duration_seconds` | `collector` | How long the last run took. |

## Certificates

Every hour on the cluster manager; every hour on the virtual desktop controller for the gateway.

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.cert.days_to_expiry` | `source`, `lb`, `cert`, `type` | Days left, negative once expired. `source:acm` is every certificate on the external and internal load balancer listeners, SNI included, with `lb` and the domain as `cert`. `source:idea` is what IDEA reads itself: `cert:dcv_gateway` (the certificate secret, never the private key), `cert:module_tls` (`idea.crt` on the shared mount), `cert:directory_ldap` (OpenLDAP only). |
| `idea.health.cert.unread` | `lb`, `source`, `reason` | A certificate that could not be dated, such as a listener's held in IAM rather than ACM. `source:module_tls` is the module's own certificate file on the shared mount, read in a child process so a hung mount cannot hold up the collector, with `reason` `timeout`, `stuck` or `failed`. |

The OpenSSH user CA of the browser terminal has no expiry: its key is a secret, so its age is `idea.health.secret.age_days`.

Compared to Datadog's `aws.certificatemanager.days_to_expiry`: that arrives about every 12 hours, only from accounts on the AWS integration, and never from GovCloud. Monitor it over a day or more, grouped by its real tag `certificatearn`.

## ECS tasks and rollouts

Every five minutes, from the module's existing ECS read.

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.ecs.task_stops` | `service`, `reason` | Stopped tasks in a five minute bucket. `reason`: `health_check`, `deployment`, `oom`, `container_exit`, `failed_to_start`, `maintenance`, `user_stop`, `scheduler`, `spot`, `other`. Use `.as_count()`. |
| `idea.health.ecs.tasks_running`, `tasks_desired`, `tasks_pending` | `service` | The service's own counts. |
| `idea.health.ecs.rollout_in_progress`, `rollout_failed` | `service` | 1 while a deployment rolls out, or has failed. |
| `idea.health.ecs.rollouts` | `service` | Deployments started in a five minute bucket. |

ECS lists stopped tasks for at least an hour. Each read sends the whole total for every bucket under 50 minutes old, and Datadog keeps the last value per metric, tags and time, so a repeat read or a new leader rewrites a point rather than adding to it. Stops are lost only if every cluster manager task is gone for over 50 minutes.

## File server and directory

Every five minutes, one read per file system in `shared-storage` that has metrics credentials, plus `ds:DescribeDirectories` for the domain controller addresses. These are the conditions a file server needs to keep authenticating, so wrong settings show before the failure does. Reads settings, event names and event times only: never a file, a user, or the text of an event.

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.directory.ldap_up` | `svm`, `filesystem` | ONTAP's LDAP client status. |
| `idea.health.directory.ldap_session_signed` | `svm`, `filesystem` | 1 when `session-security-for-ad-ldap` is `sign` or `seal`. 0 will fail binds on a domain controller requiring signing. |
| `idea.health.directory.dns_on_domain_controllers` | `svm`, `filesystem` | 1 when the SVM's DNS servers are all the directory's domain controllers. On the VPC resolver the reverse lookup gives no SPN that Kerberos can use. Managed AD only. |
| `idea.health.directory.secd_events` | `event`, `filesystem` | `secd.*` events by name in a five minute bucket. Needs a cluster-level login, see below. |
| `idea.health.directory.check_ok` | `check`, `reason`, `filesystem` | Whether each read worked. `reason:forbidden` is a path the login's role lacks. `reason:unavailable` (value 1) is a read this kind of login can never make, so not a failure. |
| `idea.health.directory.home_stat_ok` | `reason` | A `stat` of `/data/home` and a lookup of a name that does not exist, from a separate process killed at 15 seconds. Exercises name mapping; reads no one's folder. `reason`: `ok`, `timeout`, `stuck` (still in the kernel, no new one started), `denied`, `error`. |
| `idea.health.directory.home_stat_seconds` | | How long that took, including about 30 ms of process start. |

The ONTAP login used by storage metrics needs these paths added once per SVM, from `fsxadmin` on the cluster shell. Until then those checks read `forbidden` and everything else carries on:

```bash
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/name-services/ldap -access readonly
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/name-services/dns -access readonly
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/protocols/cifs/services -access readonly
```

secd event counts need a cluster-level login. ONTAP keeps the event log at cluster level and will not add it to an SVM role (`A Vserver admin cannot use command directory /api/support/ems/events with access level readonly`). With an SVM login, `check:secd_events` reads `reason:unavailable` and `secd_events` sends nothing. secd failures are still covered by `ldap_up` and `home_stat_ok`.

## Directory automation

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.ad.bind_rejected` | `code` | Each rejected bind by the admin account, with the AD sub-code (`52e` wrong password, `532` expired, `775` locked out). As it happens, per task. |
| `idea.health.ad.requests` | `namespace`, `outcome` | Requests handled: `success`, `dropped`, `retry`. |
| `idea.health.ad.errors` | `kind` | `loop`, `message`, `delete_message`. |
| `idea.health.ad.queue_depth`, `queue_in_flight` | | The queue, every minute. |
| `idea.health.ad.seconds_since_queue_read` | | Rises when the agent stopped consuming: a rejected bind raises before it reaches the queue. Stays under about 30 when healthy. |
| `idea.health.ad.admin_password_days_to_expiry` | | From the directory's own record. |

## Image bakes

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.bake.row_state` | `kind`, `os`, `arch`, `variant`, `status` | 1 for the state each row is in, every five minutes and at once on a failure. A state it left reads 0 once. |
| `idea.health.bake.row_on_release` | `kind`, `os`, `arch`, `variant` | 1 when the row is `current` on the running release. Pinned and unsupported rows are left out. |
| `idea.health.bake.rows_behind_release` | `kind` | How many rows are not. Rows in flight count, so it stays above 0 for the length of the bakes after an upgrade. |
| `idea.health.bake.failures` | `kind`, `os`, `arch`, `variant`, `reason` | Each failure that logs `IDEA_IMAGE_BAKE_FAILED`, sent as it happens: in Datadog within a minute or two. `reason`: `time_limit`, `capacity`, `check_<name>`, `checks_unreadable`, `ready_gate`, `canary`, `release_mismatch`, `no_source_image`, `no_base_stack`, `image_unavailable`, `other`. |
| `idea.health.bake.last_duration_seconds` | `kind`, `os`, `arch`, `variant` | The row's last attempt. |
| `idea.health.bake.image_age_days` | `kind`, `os`, `arch`, `variant` | Age of the image the row launches. |

## Desktops

`idea.session.count` already gives the count by `state`, `base_os` and `project`. `stack_kind` is `built` for a base stack the image pipeline feeds and `custom` for a hand-built one.

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.desktop.ready_seconds` | `base_os`, `stack_kind` | Create to first READY (a distribution). Resumes and reboots do not count. A desktop whose host reboots itself after READY during bootstrap can count twice. |
| `idea.health.desktop.provisioning_failures` | `reason`, `base_os`, `stack_kind` | `timeout`, a bootstrap status such as `kernel-install-failed`, `launch_<EC2 error>`, `dcv_session_deleted`, `dcv_session_unstable`, `create_failed`, `no_host`, `internal`. |
| `idea.health.desktop.capacity_errors` | `code`, `instance_family` | Each launch attempt refused for capacity, including those a retry in another subnet then won. |

## Scheduler and PBS

Queue to start, node provisioning and job outcomes already exist: `idea.jobs_pending_duration`, `idea.jobs_provisioning_duration`, `idea.jobs_total_duration` and `idea.job.count` by `job_outcome`.

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.scheduler.pbs_up` | `reason` | `qstat` answered and the server is Active, every minute, killed at 20 seconds. |
| `idea.health.scheduler.pbs_check_seconds` | | How long `qstat` took. |
| `idea.health.scheduler.jobs` | `state` | Jobs the server holds by state. |
| `idea.health.scheduler.capacity_errors` | `reason`, `instance_family` | Each failed scaling activity of a compute stack, once: `insufficient_capacity`, `quota`, `other`. Seen when a stack has waited long enough for the scheduler to ask. |
| `idea.health.scheduler.provisioning_failures` | `reason_class`, `instance_family` | Jobs that could not be provisioned, by the lifecycle event's class. |
| `idea.health.orphan_sweep` | `kind` | What a sweep found left behind: `job`, `instance_without_queue`, `compute_image_builder`, `desktop_image_builder`. |

## Bastion

A separate process in each task, once a minute, using only what the task can see. It reports counts, and holds no name or address. One series per host, so sum by `idea_cluster`.

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.bastion.sshd_up` | `host` | sshd offered its banner on loopback: 0 when nothing answers or sshd refuses the connection, as it would a user's. |
| `idea.health.bastion.sshd_check_seconds` | `host` | How long that took. |
| `idea.health.bastion.sessions` | `host` | Logged-in connections. |
| `idea.health.bastion.tcp_established` | `host` | Established on port 22, including those not logged in yet. |
| `idea.health.bastion.connection_drops` | `reason`, `source` | `maxstartups`, `auth_timeout`, `client_timeout`. `source` is `loopback` (the task's own checks, which sshd refused; a refusal still counts as healthy, so the task is not replaced) or `remote` (every other connection refused). MaxStartups drops are counted from sshd's own totals, so every drop counts, not only those sshd names in its log. |

## Storage levels and scratch

| Metric | Tags | Meaning |
| --- | --- | --- |
| `idea.health.storage.mount_ok` | `mount` | The shared-storage mount answered, from a separate process killed at 15 seconds. |
| `idea.health.storage.mount_used_percent`, `mount_inodes_used_percent`, `mount_free_bytes`, `mount_read_seconds` | `mount` | Levels of each mount the cluster manager or scheduler has, every five minutes. On GovCloud this is the only record of EFS and Lustre levels. |
| `idea.health.secret.age_days`, `secret.days_to_rotation` | `secret` | Secrets tagged for this cluster, every six hours, from their metadata. No value is read. |
| `idea.scratch_cleanup.folders_by_reason` | `file_system`, `reason` | Each run's folders by the reason on the page. With the existing `used_percent`, `scheduled_folders`, `kept_folders`, `removed_folders`, `removed_bytes` and `run_failed`. |
| `idea.scratch_cleanup.folder_skipped` | `file_system` | A scheduled folder a run declined on its last check. |

## What each cluster reads

The same on every cluster, GovCloud included: no call here is commercial-only. ACM `DescribeCertificate`; load balancer `DescribeListenerCertificates`; ECS service and stopped-task descriptions; SQS queue attributes for the directory automation queue; Directory Service `DescribeDirectories`; Secrets Manager `ListSecrets` (metadata), the gateway certificate secret, and the OpenLDAP certificate secret where that is the provider; the ONTAP paths above; `stat` of `/data/home` and `statvfs` of each mount; the `idea.crt` file; `qstat`.

## Permissions and upgrade

Nothing here is part of an upgrade: the collectors run inside the modules once they have started, and `ideactl` neither waits for them nor reads them.

The cluster manager policy gains `elasticloadbalancing:DescribeListenerCertificates`, `acm:DescribeCertificate`, `secretsmanager:ListSecrets` and `ds:DescribeDirectories`, all read-only. They arrive with the cluster manager stack in the upgrade. Until that stack is deployed those collectors report `access_denied` and nothing else changes. No settings need writing: absent keys read as on.

Datadog's AWS integration metrics do not reach GovCloud. There, load balancer, DynamoDB, OpenSearch, FSx and Lambda metrics have no record in Datadog beyond the ones here.
