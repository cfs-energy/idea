# Scratch cleanup

When a job finishes, its scratch folder on a shared FSx for Lustre file system gets a removal date: 30 days after it ended for a success, and 60 for anything else. On that date every check runs again, and the folder goes only if all of them still pass and its owner has had two notices. Open it from **Manage jobs**, **Scratch cleanup**. A cluster with no Lustre attachment has nothing to show.

It is off on every cluster until an administrator turns it on, and a dry run until they say otherwise. Upgrading never starts it.

## Where removals come from

The finished-job path records each job that ran. **Successful** has to be shown: PBS exit status 0 on a job recorded as having run, with nothing saying it was stopped. **Failed** is anything else: a nonzero status, none at all, killed, deleted by its owner or an administrator, or an older record with no disposition, because a job deleted while running can still exit 0. An array has one record, which lists how each subjob ended, and a subjob's folder goes by its own entry there: a subjob that failed waits the failed time whatever the rest of its array did. The array's folders get their dates together, the successful time only when every subjob succeeded, so the folders of a mixed array all wait the failed time. A subjob with no entry of its own has only the array's record, which shows that every subjob is over and never that this one succeeded, since PBS keeps the last subjob's exit status on the array: its folder waits the longer of the two times. No subjob's folder goes while any of its array is active. A duplicate finish event changes nothing, a job that finishes again takes the new date, and a job the scheduler is tracking again loses its date.

An hourly check looks in that record for what is due. It never walks the file system to find candidates, so a folder no job record names cannot be chosen. Most jobs have no folder, and their record simply ends on the date.

## What a folder has to pass

Only `<mount>/<owner>/<job number>` and `<mount>/<owner>/<job number>[<index>]` can go, the folders the portal's job scripts create for a job and an array subjob. Every other name stays, and so do owners' own folders. Nothing directly under the mount qualifies, because anyone can rename a folder into a name there.

A folder stays unless all of these hold, and each runs again on the open folder immediately before it goes and every 15 seconds while it is being removed:

* PBS lists no job under that number in any state, no node is running one, and the job cache tracks none.
* The owner has no active job at all. There is no setting for this.
* No active job depends on it (a dependency PBS has already dropped is read from the finished job's record; jobs finished before 26.11.2 have none), names the folder in its working directory, output paths, environment, arguments or script, or mentions the job's number in its script, arguments or environment.
* The job cache or job index holds a finished record for that number and owner, with start and end times, and the job ended long enough ago for how it finished. A job ended with `qdel` counts as failed even when it exited 0; jobs finished before 26.11.2 are read from their PBS comment for this.
* The folder and the owner folder above it match the job owner's account, and it holds no one else's files.
* Everything in the folder appeared and last changed while the job ran, from its first attempt's start to its end. A later change means something still uses it. Anything older means the data came from somewhere else, such as a reused job number.
* No file in it was read after the job ended. On a mount using `noatime`, as IDEA's default Lustre options do, reads leave no trace and this check sees nothing.
* The owner has had both notices.

Anything else stays. The page gives the reason for each job folder, and only a count for everything else, because a backfill scan can read folder names their owners keep private. If the scheduler, the job cache or the job index cannot answer, the run stops and nothing is removed.

While a folder is being removed, its owner's jobs cannot start: the PBS runjob hook keeps them queued with a plain reason, and PBS tries them again each scheduling cycle. A job waits at most 30 minutes plus one cycle (30 seconds by default). After that the removal stops itself and a later check finishes it, once the owner has no active job. A removal that stops renewing its hold loses it within 5 minutes and stops before its next file, and a replaced task loses both together.

### What this cannot see

Job scripts make these folders, so nothing on disk names the job that made one. The checks are strong evidence, not proof: a folder someone makes by hand under their own name, with that job's number, during that job's run, looks the same.

The checks also cannot see:

* someone using a folder from a desktop or a shell, with no job, without changing anything in it
* another user's job, which the hold does not stop, reaching a folder through a path it builds at run time, or from a file its script sources, with neither the path nor the job number in what it submitted
* anything using the file system from outside this scheduler

The notices are the safeguard for those. A folder that changes after its job ended stays. Each folder gets its own two notices: a job with folders on two file systems is mentioned for each, and a different directory put under the same name starts again.

## Turning it on

1. In **Settings**, under **Jobs**, **Scratch cleanup**, add the file system to **File systems to clean** and turn on **Automatic cleanup**. It runs as a dry run. **Last runs** shows what each check would have removed.
2. When that looks right, turn off **Dry run only**. From then on, jobs that finish get armed dates.
3. For what was already there, select **Preview** under **Backfill**, review it, then **Run backfill** and type the phrase it shows.

Nothing is recorded while it is off or in dry run, so jobs that finish then get a date only from a backfill. Turning it on never removes anything from before.

Only a cluster administrator can run or read a preview or start a backfill, not a scheduler manager. A backfill walks the file system once and puts numbered folders of finished jobs on the record, with the date their job's ending gives them. It removes nothing itself and causes no bulk surprise: each folder then gets both notices, one week apart at least, and every check. Named folders stay. On a first backfill the preview usually stops on **Most job folders per run** or **Most capacity per run**, because most folders are job folders. It says what to set. Hourly checks do not use the folder limit, and they do use the capacity limit, so set that one back afterwards. Folders of an owner with an active job stay out of a backfill: run another when those jobs have ended.

**Stop all removals** takes effect within a couple of seconds, including during a removal, though a very slow file system can stretch that. A folder interrupted that way is finished by a later check, after everything passes again. Moving a folder stops its removal too. A job administrator can stop removals, and only a cluster administrator can allow them again.

## Owners

Each owner gets at most one email a week, on the set day, in UTC, and only when there is something new. It gives counts and total size, the soonest date, the five largest folders, how to keep one, and a link. The email stays that size however many folders there are.

Each folder appears in exactly two digests: a first notice once 14 days or less remain, and a final notice in the digest before it goes. A folder goes only after both, at least 7 days after the first and 3 after the final. If a digest did not go, the date moves.

To keep a folder, an owner copies what they need to their home directory, renames the folder to anything that is not just a number, or changes a file in it. Windows desktops do not mount Lustre, so those users reach it from a Linux desktop or SSH.

A digest needs SES and email notifications on, and an enabled account with an address. Otherwise the folder waits and the check alerts. There is no setting to go without notices: with email off, nothing is removed.

What went since the last digest is in the next one, including a removal that stopped part way, and the job's details say so.

## Stops and alerts

A check stops and alerts rather than continuing when:

* the path is not the configured Lustre file system
* the file system is linked to a data repository, or that cannot be checked, because a delete could reach the S3 copy
* PBS, the job cache or the job index cannot be read, or an active job's script cannot be read
* PBS lists no active jobs while the job cache tracks some or nodes are running jobs
* a check would remove more than the allowed share of capacity, or a backfill more than the allowed share of job folders
* a removal fails, or something writes to a folder while it is being removed

An alert is an error in the scheduler log containing `SCRATCH_CLEANUP_ALERT`, the `scratch_cleanup.run_failed` metric, and the top of this page. Put your monitor on the log word or the metric. The cluster administrator gets one summary a week, on the digest day: what is scheduled, what waits for undeliverable notices, what went, and what stopped.

## Settings

Only a cluster administrator can change these, because they decide whether root deletes job data.

| Setting | Default | Meaning |
| --- | --- | --- |
| Automatic cleanup | Off | Arms the removal dates of jobs that finish |
| Dry run only | On | Reports what would go and removes nothing |
| Stop all removals | Off | Stops everything within a couple of seconds |
| File systems to clean | None | Shared storage names, for example `scratch` |
| Successful jobs: remove after | 30 days | From when the job ended; at least 14 |
| Failed jobs: remove after | 60 days | From when the job ended; at least 14 |
| Weekly digest day | 0 | 0 is Monday, 6 is Sunday |
| Most folders per run | 25 | Per hourly check; the rest wait for the next |
| Most capacity per run | 25% | Above this the check stops and alerts |
| Most job folders per run | 50% | For a backfill; ignored below 10 job folders |

## Records

The **Removal log** keeps every removal: folder, owner, job, size, who, when, and the result. The record, log and run history are in the `<cluster>.scheduler.scratch-cleanup` DynamoDB table, and each removal is also logged by the scheduler. Metrics are `scratch_cleanup.used_percent`, `kept_folders`, `removed_folders`, `removed_bytes`, `runs` and `run_failed`.
