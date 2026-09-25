# Reporting

Open **Reporting** to review recorded cost and activity without changing cluster resources. The section is read-only.

Cluster administrators and cluster managers can open Reporting. Membership in the configured operations-lead group also grants Reporting and nothing else; it does not grant module or cluster administration. Module-only administrators do not receive Reporting automatically. Group changes take effect when the user's authentication is refreshed.

Set the group under **Administration > Settings > Users and sign-in > Sign-in**. The operations-lead group must be distinct from administrator, manager, and module groups. Add users to that group through **People and access** or the authoritative directory.

## Choose a period

Select **This month**, **Last month**, **Last 30 days**, or **Custom**. Custom dates use the cluster timezone, include the end date, cannot extend into the future, and can cover at most 366 days.

The overview shows recorded total spend, job count, estimated desktop hours, and the top project by recorded job-compute spend. The current day is provisional. Read the coverage details before comparing values: unavailable sources and missing history are not treated as zero.

## Review the tables

Use **Overview / By user**, **By project**, and **By facet**. Sort any column and use **Reporting preferences** to choose the page size, visible columns, and column order.

The tables include recorded spend facets, job counts, estimated node-hours, requested and elapsed walltime, walltime efficiency, and estimated desktop hours. Project attribution is available for job-compute spend; desktop, disk, shared-storage, and AI spend remain unallocated to projects. **Unassigned** means a stored record has no project. **Unallocated to project** means the source cannot supply project attribution.

Reporting reads stored cost, completed-job, and desktop records. It does not query live infrastructure or reconstruct missing history. A value marked **Partial**, **Estimated**, or **Unavailable** is not a billing statement.

## Export CSV

Choose **Export CSV** to download the selected table from the same reporting snapshot. The export uses the current sort and visible-column order and includes all matching rows, not only the displayed page.

Snapshots expire. If the page reports an expired snapshot, choose **Reload snapshot** before exporting. A reload can change values when collectors have stored newer records.
