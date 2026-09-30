# Reporting

Open **Reporting** to review recorded cost and activity without changing cluster resources. The section is read-only.

Cluster administrators and cluster managers can open Reporting. Membership in the configured operations-lead group also grants Reporting access. It does not grant module or cluster administration. Module-only administrators do not receive Reporting automatically. Group changes take effect when the user's authentication is refreshed.

Set the group under **Administration > Settings > Users and sign-in > Sign-in**. The operations-lead group must be distinct from administrator, manager, and module groups. Add users to that group through **People and access** or the authoritative directory.

## Choose a period

Select **This month**, **Last month**, **Last 30 days**, or **Custom** in **Reporting period**. Dates use the cluster timezone. Custom ranges include the end date and can cover at most 366 days. Future dates are unavailable.

Every Reporting reader can choose one user in the **User** picker. The selection applies to every tab and its CSV exports. Choose **All users** or **Clear filter** to remove it. The URL keeps the period, user, tab, Breakdown grouping, and Breakdown sort settings.

The current day is provisional. Open the information buttons for definitions, missing data, and source dates. Unavailable sources and missing history are not treated as zero.

## Review the tables

Use **Overview**, **Jobs**, **Desktops**, **Storage**, and **Breakdown**.

**Overview** shows **Total spend** and the recorded costs for jobs, desktops, desktop disks, shared storage, and AI. The service amounts add up to the total. Service tiles with zero or unavailable amounts are hidden. A coaching sentence shows how much finished-job spend paid for unused cores when the measurements are available. The page also shows CPU efficiency, unused core-hours, daily job costs by project, and available project budgets. Budgets use each project's current budget period.

**Jobs** shows efficiency measures and job costs by queue, project, and instance family. One **Top jobs** table combines up to 50 highest-cost jobs with up to 25 jobs with the most unused core-hours. Choose **Highest cost** or **Most unused cores** to sort it. Open a job name to view its completed-job record. Use **Find rows** to search the table. **Table preferences** controls visible columns and their order.

**Desktops** shows recorded desktop spend, estimated desktop hours, daily costs, and costs by project. **Storage** shows recorded shared-storage spend and the most recent measured storage use in the period. With **All users** selected, these tabs also show comparisons by user. Storage also shows measured SSD and capacity-pool use by date when available. Dates missing a measurement for a displayed storage tier are omitted from that chart.

**Breakdown** switches between **User** and **Project** when **All users** is selected. Choosing one user shows only that user's row and hides the grouping switch. Sort any column and use **Reporting preferences** to choose the page size, visible columns, and column order.

Breakdown includes recorded service costs, job counts, estimated node-hours, requested and elapsed walltime, walltime efficiency, and estimated desktop hours. Its project totals assign job-compute spend to projects. Desktop, disk, shared-storage, and AI spend remain under **No project** in this table. **Unassigned** means a stored job record has no project.

Reporting uses stored cost, completed-job, desktop, and storage records. It does not reconstruct missing history. These estimates are not a billing statement.

## Understand job efficiency

Efficiency measures use jobs that finished in the selected period. Each percentage tile averages the jobs with the measurements needed for that measure. The CPU information button also gives the average weighted by requested core-hours.

| Measure | Calculation |
| --- | --- |
| CPU efficiency | CPU time used divided by (requested cores multiplied by elapsed time). |
| Memory efficiency | Peak memory used divided by requested memory. Without a memory request, it uses the memory on the instances reserved for that job alone. |
| Walltime efficiency | Elapsed time divided by requested time. |
| Unused core-hours | Requested cores multiplied by elapsed hours, minus CPU hours used. |
| Cost of unused core-hours | Each measured job's cost multiplied by its unused share of requested core time. |

Unused core-hours and their cost are measured against the cores requested. They are not measured against all vCPUs on an instance. The memory fallback requires single-job capacity that is not kept running for reuse. It also requires one known instance memory size. For multiple nodes, that size is multiplied by the node count. Missing measurements leave the measure unavailable.

| Efficiency | 70% or more | 40% to below 70% | Below 40% |
| --- | --- | --- | --- |
| CPU | Well sized | Some cores sat idle | Most cores sat idle |
| Memory | Well sized | Some memory unused | Most memory unused |
| Walltime | Close to requested | Finished well early | Finished far earlier than requested |

Per-job hints suggest fewer requested cores when less than half were used. The suggested core count is per node for multi-node jobs. Memory hints compare the request with peak use. Without a memory request, low use of the job's own instance memory can prompt a smaller instance type.

Breakdown calculates walltime efficiency from total elapsed time divided by total requested time. That percentage can exceed 100%.

## Export CSV

On **Breakdown**, choose **Export CSV** to download the selected table from the loaded report. The export uses the current user selection, sort, and visible-column order. It includes rows across all pages. **Find on this page** only filters the displayed page and does not limit this export.

Tables such as **Top jobs** also have **Export CSV**. These exports include all rows matching the table's search in the current sort and visible-column order.

An expired report reloads once automatically. Use **Reload** to request the report again. Use **Try again** after a loading error. A reload can change values when newer records are available. If an export fails, wait for the report to load and choose **Export CSV** again.
