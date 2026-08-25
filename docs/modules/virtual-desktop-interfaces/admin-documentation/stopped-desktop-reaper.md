---
description: Opt-in cleanup of desktops left stopped past a cutoff, and of session records whose instance is gone
---

# Stopped desktop reaper

The stopped desktop reaper deletes virtual desktops that have sat stopped longer than a cutoff, and session records whose EC2 instance no longer exists, through the same delete path an administrator uses on the Sessions page.

## What it targets

* A session in state **Stopped** whose EC2 instance is `stopped` and has been stopped for longer than `stopped_after_days`. The stop time is read from the instance's `StateTransitionReason`, which EC2 formats like `User initiated (2026-07-30 14:02:11 GMT)`.
* A session in state **Stopped** or **Error** whose EC2 instance is `terminated` or no longer exists. Such a record holds one of the owner's session slots for nothing.

A deleted session is torn down the way a user-initiated delete is: the instance is terminated, and the session record, its schedules, permissions and search index entry are removed. The owner receives the same "session deleted" notification as for any other delete, when that notification is enabled.

## Safety rules

* The feature is **off by default**, and **dry run is on by default**. A dry run logs one line per desktop it would delete and deletes nothing.
* A stop time that cannot be read is never guessed at: the desktop is skipped and logged.
* An instance carrying any of the `keep_tags` tag keys, with any value, is never deleted.
* A session an administrator has exempted from inside IDEA is never deleted, whatever its tags.
* The instance state is read again immediately before acting; a desktop that is running again by then is skipped.
* At most `max_per_pass` deletions happen per pass. The remaining desktops are picked up on the following passes.
* An EC2 error other than "instance not found" skips the desktop rather than treating it as gone.

The reaper runs on the same 30-minute schedule as the other controller sweeps, with a short time budget per pass; a large cluster is walked to the end across passes.

## Settings

All settings live under `virtual-desktop-controller.dcv_session.stopped_session_reaper`. All but `keep_tags` can be edited from **Virtual Desktops > Settings > Server**; `keep_tags` is edited with `idea-admin.sh`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Run the sweep at all. |
| `dry_run` | `true` | Log what would be deleted without deleting anything. |
| `stopped_after_days` | `30` | A desktop stopped longer than this many days, per the EC2 stop time, is deleted. |
| `warn_days_before` | `7` | Email the owner this many days before deletion and wait that long before deleting. `0` sends no notice. |
| `max_per_pass` | `25` | Deletions per pass. The rest wait for the next pass. |
| `keep_tags` | `['idea:keep', 'ideal:keep']` | An instance carrying any of these tag keys is never deleted. |

## Warning the owner

With `warn_days_before` above zero, the owner is emailed before the desktop is deleted and the deletion waits until they have had the whole warning window. With `stopped_after_days: 30` and `warn_days_before: 7`, a desktop stopped on day 0 gets the notice on day 23 and is deleted on day 30. A desktop that is already past the cutoff when the feature is turned on is warned first and deleted `warn_days_before` days later.

The email, `virtual-desktop-controller.session-reaper-warning` under **Cluster Settings > Email Templates**, says how many days the desktop has been stopped, the date (UTC) it will be deleted unless it is started before then, that starting it resets the clock, and that an administrator can exempt it if it must be kept. It is sent once per stop: starting the desktop clears the notice, and the next stop starts the count again. The notice can be switched off under `dcv_session.notifications.reaper_warning`, in which case the warning window still applies but no email goes out. The session detail page shows when the notice was sent.

## Exempting a desktop

There are two ways to keep a desktop out of the reaper's reach.

**From IDEA.** On **Virtual Desktops > Sessions**, select one or more sessions and choose **Actions > Exempt from Reaper**. The reason you enter is shown on the session and written to an `idea:keep` tag on the instance, so the exemption is visible in EC2 and honored by any external tooling that reads the tag; with no reason, your username is used. **Clear Reaper Exemption** removes the flag and that tag. Exempt sessions carry a "Reaper exempt" badge in the list. The flag on the session record is what the reaper checks, so the exemption holds even if the tag could not be written.

**From EC2.** Add a tag whose key is one of the `keep_tags` to the instance, with any value. This is honored regardless of who set it and is the option for tooling outside IDEA.

## Recommended rollout

1. Set `enabled` to true and leave `dry_run` on.
2. After a pass or two, read the virtual desktop controller log. Each pass logs a summary such as `stopped desktop reaper pass (dry run): candidates=12 kept=2 no_stop_time=0 not_due=7 raced=0 reaped=2 orphans_cleaned=1 failed=0`, with one `[dry run] would delete session ...` line per desktop, naming the session, the owner and how long it has been stopped.
3. Exempt anything that should stay, or adjust `stopped_after_days` and `warn_days_before`.
4. Set `dry_run` to false.
