## Reporting

Reporting is read-only. Cluster administrators and managers receive access automatically. Membership in the configured operations-lead group adds Reporting without granting administration. Module-only administrators do not receive it automatically. Group changes take effect when authentication is refreshed.

Configure the group under **Settings > Users and sign-in > Sign-in**. It must not match an administrator, manager, or module group.

Choose **This month**, **Last month**, **Last 30 days**, or **Custom** in **Reporting period**. Dates use the cluster timezone. Custom ranges include the end date and allow at most 366 days. Future dates are unavailable. The current day is provisional.

Use **Overview**, **Jobs**, **Desktops**, **Storage**, and **Breakdown**. Every Reporting reader can select one user in the **User** picker to filter every view and its CSV exports. Choose **All users** or **Clear filter** to remove the filter. The URL retains the period, user, tab, Breakdown grouping, and Breakdown sort settings.

**Overview** shows recorded **Total spend** for jobs, desktops, desktop disks, shared storage, and AI. The service amounts add up to the total. Zero and unavailable service tiles are hidden. The coaching sentence and unused-core tiles estimate costs from jobs that finished in the period; Job spend uses recorded job costs for the period. A second coaching sentence estimates desktop spend on idle desktop time when idle checks are available. Daily job costs and available project budgets also appear here. Budgets use their own current budget periods.

**Jobs** shows CPU, memory, and walltime efficiency, unused core-hours, and their cost. Percentage tiles average jobs with the needed measurements. CPU efficiency compares CPU time used with requested cores multiplied by elapsed time. Unused core-hours subtract CPU hours used from requested core-hours. Their cost is each measured job's cost multiplied by its unused share of requested core time. Both use requested cores rather than all instance vCPUs.

Memory efficiency compares peak use with requested memory. Without a request, it uses the memory of the job's own instances only for single-job capacity that is not kept running for reuse. One known instance memory size is required. That size is multiplied by the node count. Walltime efficiency compares elapsed time with requested time.

| Efficiency | 70% to 100% | 40% to below 70% | Below 40% |
| --- | --- | --- | --- |
| CPU | Well sized | Some cores sat idle | Most cores sat idle |
| Memory | Well sized | Some memory unused | Most memory unused |
| Walltime | Close to requested | Finished well early | Finished far earlier than requested |

Use the single **Top jobs** table to sort by **Highest cost** or **Most unused cores**. Job names link to completed-job records only with scheduler administration access; other Reporting readers see the names as text. Hints can suggest fewer cores, per node for multi-node jobs. Low memory use without a memory request can suggest a smaller instance type. **Find rows** searches the table. **Table preferences** controls columns and their order.

Desktop costs come from stored daily costs; desktop hours are estimated from sessions overlapping the period.

**Desktops** shows spend, estimated hours, daily costs, costs by project, and idle desktop time with its cost. Idle hours count the time a desktop ran with nobody connected and CPU under the idle stop threshold, using the same rules as the idle stop. They come from the checks the idle stop makes every 30 minutes, so desktops without an idle stop in their schedule, and hours outside a schedule's stop window, are left out rather than counted as in use. Their cost is idle hours multiplied by each desktop's on-demand hourly price. The **Idle desktops** table hints at a shorter idle time before stopping when a desktop was idle for at least half of 2 or more checked hours. **Storage** shows shared-storage spend and measured storage use. With **All users** selected, these tabs include comparisons by user. Storage also shows measured use by tier when available.

On **Breakdown**, switch between **User** and **Project** with **All users** selected. A user filter shows just that user's row and hides this switch. Project totals assign recorded job-compute spend to projects. Other service costs appear under **No project** in this table. **Unassigned** means a job record has no project. Choose **Reporting preferences** to set the page size, visible columns, and column order.

Breakdown's **Export CSV** includes all pages for the selected user or **All users**, with the current sort and visible-column order. **Find on this page** does not limit that export. Other tables export all rows matching **Find rows**.

An expired report reloads once automatically. **Reload** requests the report again. **Try again** retries a failed load. Retry a failed CSV download after the report loads. Newer stored records can change the values.

Open the information buttons for definitions, missing data, and source dates. Missing or unavailable data is not zero. Stored estimates do not reconstruct missing history and are not a billing statement.

The **User** picker includes all users with recorded activity in the period, including users outside the top spenders and users with unpriced jobs. It includes finished jobs, overlapping desktops, measured storage use, and positive recorded costs; inactive accounts and system identities are excluded. The list comes from the Reporting-gated **GetSummary** response, without an additional unfiltered insights request. **ListRows** applies the selected username on the server before pagination. Missing sources show **No users found in available records** rather than claiming there were no users.

Memory above 100% shows **Used more than requested**; walltime above 100% shows **Ran longer than requested**. Both use a warning status. CPU efficiency remains capped at 100%. For jobs without a retained select expression, the CPU request is the job-wide **Resource_List.ncpus** total and is not multiplied by node count again. My costs retains its personal completed-job links and uses recorded calendar-month costs in its spend tiles.
