# Job completion timing

A completion record describes the latest execution attempt for the exact PBS job ID.
It does not accumulate earlier attempts or the waits between them.
Hooks identify an attempt by numeric PBS `run_count`; multiple hosts with the same
count belong to that attempt. Requeue, rerun and node replacement advance that count.
Scheduler restarts preserve the hook history in SQLite.

A job is recorded only when PBS has shown it is over: history says finished, or PBS
answers that it no longer knows the job, in which case the record is made from the
scheduler's cached copy. A job missing from a listing is asked about on its own first.
While PBS still has it in another state, or gives no answer, it stays tracked.

Fresh terminal PBS history supplies both timestamps from one snapshot. Neither
missing endpoint is filled from cached job timestamps or hooks. If history has
neither endpoint, the latest hook attempt can supply the pair. When PBS history is
unavailable, hooks replace both cached endpoints, including missing values. Without
hooks, the cached timestamp pair is retained as a unit. If the selected hook start or
end predates the cached start, those hooks describe a superseded attempt; both
endpoints, duration and estimated cost are left unknown.

For hook recovery, start is the earliest known host start and end is the latest known
host end within the selected attempt. A missing or reversed endpoint leaves duration
and estimated cost unavailable. An end-only hook proves execution; it must not become
a zero-second, never-started record. Requested walltime and the current clock are not
execution measurements. Missing starts are left unknown even when the end hook
contains measured walltime.

Duplicate and reordered hooks merge within their attempt. A begin hook delivered
after completion does not rewrite the completed record: unknown duration and cost
remain unknown. Jobs with no start or execution hooks retain the never-started
behavior. The completion timestamp for such a job records observation of completion,
not measured execution time.

## Array jobs

An array job (`qsub -J`) is one job under its own ID, `123[]`. Its subjobs, `123[1]`
and so on, share its job UID, stack and licenses, and are never jobs of their own. It
has one row in Active jobs, which reads Running from its first subjob's start, one
completion record, one cost and at most one started and one completed email.

The record is written when PBS history shows the array finished. Its start is the first
subjob's start and its end is the last subjob's end, so its duration and estimated cost
cover the time its capacity was in use, waits between subjobs included. It lists how each
subjob ended: state, exit status, whether `qdel` ended it, start and end. Its own exit
status is 0 only when every submitted index finished with 0, and otherwise the first
failed subjob's. PBS keeps the last subjob's status on the array, so that value is not
used. An array with a subjob that `qdel` ended is recorded as ended by `qdel`, even when
every subjob exited 0. If the subjobs' history cannot be read, the record has no exit
status and no subjob list, and nothing treats it as a success.

Begin and end hooks from subjobs are kept as one run per host for the array, from the
first begin to the last end there. They can supply the pair when PBS history is gone,
as for any job; a subjob's own result comes only from PBS history.

The emails describe the array and do not carry the subjob list; that is on the record.

The array's nodes stay while it has subjobs left to run, idle or not, and its stack is
not deleted by the stack timeout once a subjob has started. Before that delete the
scheduler asks PBS for the array and for every subjob, and deletes only when the array
and each index it was submitted with read queued, held or waiting. A subjob deleted
while queued never ran, has no start time, and counts as not started. A subjob in any other
state, an index PBS does not list or no answer keeps the stack. Subjobs run on the
capacity one subjob asks for, so on a single-job queue they run one after another.
