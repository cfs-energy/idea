# My costs

My costs shows stored estimates for this month and last month in the cluster timezone. Daily charts do not spread monthly totals across missing dates. **--** means unavailable, not zero; badges identify partial, estimated, collecting, or missing coverage.

| Tile | Includes |
| --- | --- |
| Jobs | Priced completed-job compute records. |
| Desktops | Recorded session intervals; uncertain historical stop times are estimated. Deleting a legacy stopped desktop does not bill its stopped interval as running. |
| Desktop disks | Observed provisioned storage from collection onward. |
| Shared storage | Daily storage rates multiplied by dated byte shares. ONTAP includes SSD, throughput, excess IOPS, and capacity-pool bytes; EFS uses storage class. No storage cost-allocation tag is required. |
| AI | Project daily spend apportioned by recorded user tokens. |

A user absent from a complete ONTAP quota report counts as zero only when a default user quota rule exists. Historical dates without a dated share remain missing. Source times are recorded in UTC and grouped in the cluster timezone.

Choose **Refresh** to request the next collector check. Current values stay visible while collection runs. Upstream billing and price caches can delay changes.

Open **How it is calculated** for coverage, source dates, and exclusions. Open **Storage usage: folders and quotas** separately; it does not delay the cost page.
