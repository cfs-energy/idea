# Job completion timing

A completion record describes the latest execution attempt for the exact PBS job or
array subjob ID. It does not accumulate earlier attempts or the waits between them.
Hooks identify an attempt by numeric PBS `run_count`; multiple hosts with the same
count belong to that attempt. Requeue, rerun and node replacement advance that count.
Scheduler restarts preserve the hook history in SQLite.

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
