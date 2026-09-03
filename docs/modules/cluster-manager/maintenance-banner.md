---
description: Tell users what is happening during a maintenance window, from the portal and at job submission
---

# Maintenance Banner

A maintenance window puts a warning banner on every page of the web portal, including the sign-in page, and makes the scheduler refuse new job submissions with the same message. It is off by default.

It is meant for the days before an upgrade, when the scheduler has been stopped but the portal is still up. Without it, a user submitting a job gets a connection error and no explanation.

## Settings

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `cluster-manager.maintenance.enabled` | bool | `false` | Shows the banner and refuses job submissions |
| `cluster-manager.maintenance.message` | string | empty | Plain text. No markup, no links |
| `cluster-manager.maintenance.ends_at` | string | empty | Optional ISO 8601 timestamp |

All three are read live from the config store. Changing them needs no redeploy and no module restart. Open portal pages pick a change up within a minute, and the scheduler within about half a minute.

`ends_at` is optional. When it is set, the banner appends "until" and the time, rendered in each user's own timezone. A value with no offset is read as UTC, so `2026-09-15T18:00:00Z` and `2026-09-15T18:00:00` mean the same thing. A value that cannot be parsed is dropped and the rest of the banner still shows.

If the window is turned on with no message, the banner and the job rejection both read "This cluster is undergoing maintenance."

## Setting it from the portal

Cluster Management, then Settings, then the **Maintenance** tab. Set the message and the end time, turn the toggle on, and save. All three values are written together, so the banner never appears carrying the previous window's text.

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

A user running `qsub` directly on the login node is also not covered. Submissions from the portal are refused with the maintenance message; a direct `qsub` against a stopped scheduler fails the way PBS fails.
