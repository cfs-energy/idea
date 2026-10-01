# My costs

**My costs** is the default landing page. A valid personal landing preference takes priority over the cluster default. Without either preference, the portal opens My costs. A stored `home` preference also opens My costs. The portal title returns to your selected landing page. The header cost ticker opens My costs when it is shown.

The page always shows the signed-in user's costs and activity. There is no user filter. Use **Overview**, **Jobs**, **Desktops**, and **Storage**, the same tabs as Reporting without Breakdown.

Choose **This month**, **Last month**, **Last 30 days**, or **Custom** in **Reporting period**. Dates use the cluster timezone. Custom ranges include the end date and allow at most 366 days. Future dates are unavailable. The URL retains the period and tab.

For **This month** and **Last month**, **Overview** shows the selected month's recorded estimates in the cluster currency. **Total spend** includes jobs, desktops, desktop disks, shared storage, and AI. It compares this month so far with the full last month as a money difference. A zero AI tile is hidden. For **Last 30 days** or **Custom**, the selected-period total adds available job, desktop, and shared-storage costs. A separate **This month** section keeps the five-service monthly totals visible.

The overview shows spend on unused cores of finished jobs when measurements are available. **Jobs** shows CPU, memory, and walltime efficiency. CPU efficiency compares CPU time used with requested cores multiplied by elapsed time. Unused core-hours subtract CPU hours used from requested core-hours. Their cost multiplies each measured job's cost by its unused share of requested core time. These measures use requested cores rather than all instance vCPUs.

Memory efficiency compares peak use with the request. Without a request, it uses the job's own instance memory only for single-job capacity that is not kept running for reuse. One known instance memory size is required. That size is multiplied by the node count. Walltime efficiency compares elapsed time with requested time. Percentage tiles average jobs with the needed measurements.

| Efficiency | 70% or more | 40% to below 70% | Below 40% |
| --- | --- | --- | --- |
| CPU | Well sized | Some cores sat idle | Most cores sat idle |
| Memory | Well sized | Some memory unused | Most memory unused |
| Walltime | Close to requested | Finished well early | Finished far earlier than requested |

The single **Top jobs** table sorts by **Highest cost** or **Most unused cores**. Job names open your completed-job records. Hints can suggest fewer cores, per node for multi-node jobs. Low memory use without a memory request can suggest a smaller instance type. **Find rows** searches the table. **Table preferences** controls columns and their order. **Export CSV** includes all matching rows across pages.

**Desktops** shows spend, estimated hours, daily costs, costs by project, and idle desktop time with its cost. Idle hours count the time a desktop ran with nobody connected and CPU under the idle stop threshold, using the same rules as the idle stop. They come from the checks the idle stop makes every 30 minutes, so desktops without an idle stop in their schedule, and hours outside a schedule's stop window, are left out rather than counted as in use. Their cost is idle hours multiplied by each desktop's on-demand hourly price. The **Idle desktops** table hints at a shorter idle time before stopping when a desktop was idle for at least half of 2 or more checked hours. **Storage** shows shared-storage spend and measured storage use. Open **Storage usage: folders and quotas** on the Storage tab for folder sizes, file ages, bytes unchanged for 90 days, and available quotas. It loads only when opened.

| Tile | Includes |
| --- | --- |
| Jobs | Priced completed-job compute records. |
| Desktops | Recorded session intervals. Uncertain historical stop times are estimated. Deleting a legacy stopped desktop does not bill its stopped interval as running. |
| Desktop disks | Observed provisioned storage from collection onward. |
| Shared storage | Daily storage rates multiplied by dated byte shares. ONTAP includes SSD, throughput, excess IOPS, and capacity-pool bytes. EFS uses storage class. No storage cost-allocation tag is required. |
| AI | Project daily spend apportioned by recorded user tokens. |

A user absent from a complete ONTAP quota report counts as zero only when a default user quota rule exists. Historical dates without a dated share remain missing. Source times are recorded in UTC and grouped in the cluster timezone.

Monthly totals add available daily amounts. A dash means unavailable. Missing amounts are not treated as zero. Monthly service badges identify missing data, estimated shares, or collection in progress. Information buttons explain the amounts. Daily charts do not spread monthly totals across missing dates.

Choose **Refresh** to reload the selected-period report and request the next collector check. **Refresh requested** acknowledges the request. Collection runs at startup and every 15 minutes for all users. The collector checks refresh requests every minute. Current monthly values stay visible while collection runs or a request fails. Upstream billing and price caches can delay changes.
