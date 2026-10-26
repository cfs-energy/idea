---
description: Tell users what is happening during a maintenance window, from the portal and at job submission
---

# Maintenance Banner

A maintenance window puts a warning banner on every page of the web portal, including the sign-in page, and refuses new portal and direct `qsub` submissions from non-admin users with the same configured message. It is off by default.

Turn it on before draining for an upgrade. Running and already-queued jobs are untouched. Root and cluster administrators (accounts with sudo access) can still submit through the portal and `qsub`; the portal checks the authenticated user and the hook checks the PBS submitter rather than the privileged socket caller. If the account lookup fails, the admission path logs the failure and refuses submission with the maintenance message. Use maintenance instead of manually disabling queues with qmgr.

## Settings

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `cluster-manager.maintenance.enabled` | bool | `false` | Shows the banner and refuses job submissions |
| `cluster-manager.maintenance.message` | string | empty | Plain text. No markup, no links |
| `cluster-manager.maintenance.ends_at` | string | empty | Optional ISO 8601 timestamp |

All three are read live from the config store. The queuejob admission path reads the current setting on every submission. Changing it needs no redeploy and no module restart; clearing `enabled` restores submission. Open portal pages pick a change up within a minute, and the scheduler within about half a minute.

`ends_at` is optional. When it is set, the banner appends "until" and the time, rendered in each user's own timezone. A value with no offset is read as UTC, so `2026-09-15T18:00:00Z` and `2026-09-15T18:00:00` mean the same thing. A value that cannot be parsed is dropped and the rest of the banner still shows. Portal and `qsub` submission rejections append the same end time in UTC.

If the window is turned on with no message, all paths use "This cluster is undergoing maintenance." Portal and `qsub` rejections prefix the notice with "Cluster is in maintenance: ".

## Setting it from the portal

Administration, then Settings, then **Maintenance notice**. Set the message and the end time, turn the toggle on, and save. All three values are written together, so the banner never appears carrying the previous window's text.

You need to be a cluster administrator. Every signed-in user can read the three keys, which is how the banner reaches them; the rest of the cluster-manager settings stay admin-only.

## Setting it from the CLI

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.maintenance.enabled,Type=bool,Value=true" \
      "Key=cluster-manager.maintenance.message,Type=string,Value=HPC scheduler is closed for a cluster upgrade. Running desktops are unaffected." \
      "Key=cluster-manager.maintenance.ends_at,Type=string,Value=2026-09-15T18:00:00Z" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

To close the window, set `enabled` back to `false`.

## What it does not cover

The banner is served by the cluster-manager module. During the few minutes that module is itself being replaced, the portal is down and serves nothing at all, so nobody sees a banner. Announce that outage separately.

Direct `qsub` receives the maintenance message while PBS and the scheduler admission service are running. Once PBS is stopped, it cannot run its hook, so `qsub` reports the normal PBS connection failure.
