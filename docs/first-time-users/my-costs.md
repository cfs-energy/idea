# My costs

The portal opens **My costs** by default. A valid personal landing-page preference takes priority over the cluster default. Without either preference, the portal opens My costs. A stored `home` preference also opens My costs. The portal title returns to the selected landing page. You can open **My costs** from the navigation bar or the header cost ticker when it is shown.

My costs always shows the signed-in user's costs and activity. It has no user filter. Use **Overview**, **Jobs**, **Desktops**, and **Storage**, the same tabs as Reporting without Breakdown.

Choose **This month**, **Last month**, **Last 30 days**, or **Custom** in **Reporting period**. Dates use the cluster timezone. Custom ranges include the end date and allow at most 366 days. Future dates are unavailable. The selected period and tab stay in the URL.

**Overview** shows monthly costs in the cluster currency. **Total spend** includes all five services in the calculation table below. For **This month** and **Last month**, the service tiles show the selected calendar month. The total compares this month so far with the full last month. It shows the difference as a money amount. The AI tile is hidden when its amount is zero.

For **Last 30 days** or **Custom**, the selected-period total adds available job, desktop, and shared-storage costs. A separate **This month** section retains the five-service monthly totals. Desktop disks and AI are included in those monthly totals.

The overview also shows a sentence about finished-job spend on unused cores when measurements are available. CPU efficiency, unused core-hours, their cost, and daily job costs by project appear below the spend tiles. Available budgets for your projects use their current budget periods.

**Jobs** shows CPU, memory, and walltime efficiency alongside costs by queue, project, and instance family. The single **Top jobs** table sorts by **Highest cost** or **Most unused cores**. Job names open your completed-job records. Use **Find rows** to search the table. **Table preferences** controls visible columns and their order. **Export CSV** includes all matching rows across pages in the current sort and column order.

CPU efficiency measures CPU time used against requested cores multiplied by elapsed time. Unused core-hours subtract CPU hours used from requested cores multiplied by elapsed hours. Their cost is each measured job's cost multiplied by its unused share of requested core time. These measures use requested cores rather than all instance vCPUs.

Memory efficiency compares peak memory use with requested memory. Without a memory request, it uses the job's own instance memory only for single-job capacity that is not kept running for reuse. One known instance memory size is required. That size is multiplied by the node count. Walltime efficiency compares elapsed time with requested time. Percentage tiles average jobs with the needed measurements.

| Efficiency | 70% or more | 40% to below 70% | Below 40% |
| --- | --- | --- | --- |
| CPU | Well sized | Some cores sat idle | Most cores sat idle |
| Memory | Well sized | Some memory unused | Most memory unused |
| Walltime | Close to requested | Finished well early | Finished far earlier than requested |

Job hints can suggest fewer requested cores when less than half were used. For multi-node jobs, the suggestion is per node. Low memory use without a memory request can suggest a smaller instance type.

**Desktops** shows desktop spend, estimated hours, daily costs, and costs by project. **Storage** shows shared-storage spend and measured storage use. Open **Storage usage: folders and quotas** on the Storage tab for folder sizes, file ages, bytes unchanged for 90 days, and available quotas. This view loads only when opened.

| Tile | Calculation |
| --- | --- |
| Jobs | Priced compute line items for completed jobs on each date. Running jobs, disks and scratch storage are excluded. |
| Desktops | Recorded session intervals intersected with each day, multiplied by the instance rate. Deleting a legacy stopped desktop does not bill its stopped interval as running. An uncertain historical stop time is marked as estimated. Incomplete restart history remains a limitation. |
| Desktop disks | Owned provisioned size multiplied by its GB-month rate and the fraction of that calendar month. Stopped and retained disks count. Dated inventory begins with collection. Earlier days, unobserved disks, disk snapshots, and additional IOPS or throughput are not reconstructed. |
| Shared storage | Daily rate estimate multiplied by the user's dated byte share. ONTAP includes provisioned SSD, throughput, IOPS above the included 3 per GB, and capacity-pool bytes. EFS includes storage by class, excluding throughput and requests. With a default user quota rule, users absent from a complete quota report count as zero bytes. |
| AI | Each project's daily AI spend apportioned by that day's user tokens divided by project tokens. Missing spend or attribution, including spend without a token denominator, remains unknown. |

Monthly totals add the available amounts. Unknown amounts display a dash. Confirmed absence of usage is zero. Billing corrections can be negative. Monthly service tiles can show **No data**, **Estimated share**, or **Collecting**. Open each tile's information button for the reason or calculation note. Missing amounts are not treated as zero.

Monthly totals use stored daily amounts. A partial total includes only known amounts. No monthly figure is spread evenly into invented daily costs. The job and desktop charts show costs for the selected period.

Source timestamps are recorded in UTC, including on hosts configured with another timezone, then grouped into calendar days in the cluster timezone.

## Collection and refresh

The cluster manager creates a dedicated personal-costs table at startup. Its leader collects costs at startup and every 15 minutes for every user, including users who have never opened the page. It saves a complete new set of results before making them available. Each response uses one saved set of results. Older results remain available to requests already in progress.

The monthly costs, summary, and ticker read stored results without waiting for billing, inventory, pricing, or filesystem work. The summary API retains its trailing 30-day window. The month-to-date ticker uses the saved current-month total. Other configured ticker periods have their own saved totals.

**Refresh** reloads the selected-period report and queues a collection request. Repeated pending requests are combined. The collector checks requests every minute. **Refresh requested** acknowledges the request. Current monthly amounts remain visible while collection runs or a request fails.

My costs and the month-to-date ticker share saved monthly results in the browser. The browser checks every 15 seconds while collection is pending. After two minutes, checks slow to once a minute. It ordinarily checks every five minutes while visible. Billing source reads are cached for six hours. Disk rates are cached for a day. Requesting refresh cannot make upstream billing arrive sooner.

A user without saved monthly results sees **Collecting · about N min**. The estimate uses the next scheduled collection and the last measured run duration. The initial assumed duration is 20 minutes. An overdue collector shows **Collecting delayed** with an updated estimate. Missing sources leave service costs unavailable.

## Storage evidence

Today's byte share is never reused to fill last month. Historical days without a dated share remain missing. ONTAP shares use the sum of all user quota bytes rather than volume usage after storage savings. The sum includes root and unresolved identities whose costs remain unassigned. Dated shares and disk observations are retained for 400 days from collection onward. Disk observations account for at most 15 minutes each. Earlier observations survive deletion. Missed collection intervals remain unknown.

Complete home-directory scans can establish measured bytes. A home-directory listing alone cannot establish the filesystem total needed to calculate a cost share. The collector verifies the mounted filesystem before retaining home measurements. For a verifiable EFS root mount, a complete filesystem scan can supply that total. Users receive only their byte share. System or unrelated bytes remain unassigned. Subdirectory mounts, unverifiable proxy mounts, and partial root scans cannot establish that total. Shared-storage costs stay unavailable when user storage cannot be separated from system space or unrelated directories.

Incomplete scans, unreadable users, stale measurements, a zero filesystem total, and missing billing prevent a valid cost share. Volume quota reports must account for the complete filesystem to establish a cost share. A filesystem with valid dated evidence can contribute a known subtotal. Missing evidence from other filesystems leaves that subtotal partial.
