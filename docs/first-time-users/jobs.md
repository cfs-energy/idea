# Job status and costs

Open **My jobs** to see Active and Completed jobs. Select a job for its details.

The Active status cell and **Status Reason** in the details show the scheduler's
`status_reason`: the requested capacity and blocking queue usage, a provisioning
error, or the last error and attempt count when retries stop. A capacity wait is not
a permanent rejection and does not provide a queue position or start-time estimate.
An invalid instance/image architecture combination is rejected at submission, with
the validation message shown in the portal and by `qsub`.

Completed jobs retain their `disposition`: **Ran**, **Failed**, **Held**, or
**Deleted**. Deletions include owner cancellations and system deletions when the
owner is disabled or no longer exists. The reason remains in job details, even
when the job never started. Older records may have less information.

**Estimated Costs** uses measured elapsed runtime after an early stop, with the
pricing minimum and provisioning overhead where applicable. Jobs that never
started have no measured compute estimate. Missing pricing reads **Price not
available**. Recorded savings appear below the cost items. **Budget impact** shows
the recorded budget limit, actual and forecast spend, and the job's estimated share
when budget data is available. These figures are estimates, not a billing statement.

Administrators can open **Manage jobs > Queues** to see queued jobs and the
threshold / current usage beside a blocked queue's status.
