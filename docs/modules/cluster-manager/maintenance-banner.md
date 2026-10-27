---
description: Tell users what is happening during a maintenance window, from the portal and at job submission
---

# Maintenance Banner

A maintenance window puts a warning banner on every page of the web portal, including the sign-in page, and refuses new portal and direct `qsub` submissions from non-admin users with the same configured message. It is off by default.

Turn it on before draining for an upgrade. Running and already-queued jobs are untouched. Root and cluster administrators (accounts with sudo access) can still submit through the portal and `qsub`; the portal checks the authenticated user and the hook checks the PBS submitter rather than the privileged socket caller. If the account lookup fails, the admission path logs the failure and refuses submission with the maintenance message. The admin lookup is limited to 2 seconds and 4 concurrent checks; a slow or saturated lookup refuses the submission, so retry or submit serially. The configured image-pipeline validation user (default `idea-validate`) is also exempt, so compute image canaries can validate upgrade images during maintenance. This exemption uses the PBS submitter identity and leaves normal job validation and enabled-account checks in place. Use maintenance instead of manually disabling queues with qmgr.

## Settings

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `cluster-manager.maintenance.enabled` | bool | `false` | Shows the banner and refuses job submissions |
| `cluster-manager.maintenance.message` | string | empty | Plain text. No markup, no links |
| `cluster-manager.maintenance.ends_at` | string | empty | Optional ISO 8601 timestamp |

All three are read live from the config store. The queuejob admission path reads the current setting on every submission. Changing it needs no redeploy and no module restart; clearing `enabled` restores submission. Open portal pages pick a change up within a minute, and the scheduler within about half a minute.

`ends_at` is optional: an empty string or null clears it. When set, the banner adds "Expected to end <time>." in each user's own timezone (a period is added after the message unless it already ends in punctuation). Portal and `qsub` submission rejections add the same sentence as "Expected to end <YYYY-MM-DD HH:MM> UTC."

Settings updates accept the ISO 8601 forms shared by the portal and scheduler:

- Calendar dates: `YYYY-MM-DD` or `YYYYMMDD`.
- ISO week dates: `YYYY-Www-D` or `YYYYWwwD` (weekday `D` is 1-7). `YYYY-Www` and `YYYYWww` alone mean Monday of that week and cannot include a time.
- Calendar dates and week dates with a weekday may include `T` or one space, then `HH`, `HH:MM`, or `HH:MM:SS` for hyphenated dates; use `HH`, `HHMM`, or `HHMMSS` for compact dates. Seconds may have a decimal fraction introduced by `.` or `,`; a fraction that rounds up to a whole second in the portal parser is rejected.
- A time may end in `Z`, `±HH`, `±HHMM`, or `±HH:MM`. Offset hours must be 00-23 and minutes 00-59. Without an offset, the time is UTC; without a time, the date means midnight UTC. Surrounding whitespace is ignored.

Dates and times must be valid (years 0001-9999, hours 00-23, minutes and seconds 00-59), and converting to UTC must stay within years 0001-9999. Ordinal dates, incomplete calendar dates, arbitrary date/time separators, mixed compact/colon time forms, and non-string values other than null are rejected. For example, `2026-09-15T18:00:00Z`, `2026-09-15 18:00`, and `20260915T1800` all mean the same instant. An unparsable value set outside the settings API is omitted by the consumer that cannot parse it; its maintenance message still shows.

If the window is turned on with no message, the banner uses "This cluster is undergoing maintenance." Portal and `qsub` rejections use "Cluster is in maintenance." Configured messages are prefixed with "Cluster is in maintenance: ".

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
