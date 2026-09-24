# My costs

Open **My costs** from Home or the navigation bar. Costs are estimates in the cluster currency, using calendar months in the cluster timezone.

Home shows this month, last month, the snapshot's **As of** time, five cost tiles and a grouped comparison chart. Recent jobs and desktops follow; quick links are last. Each cost tile links to its daily chart on My costs.

My costs shows the same headline and tiles followed by five daily bar charts. Each day places this month beside last month. Last month's amount covers the full month; this month's amount covers the elapsed month. These are not percentage-change comparisons.

| Tile | Calculation |
| --- | --- |
| Jobs | Priced compute line items for completed jobs on each date. Running jobs, disks and scratch storage are excluded. |
| Desktops | Recorded session intervals intersected with each day, multiplied by the instance rate. Inferred stop times and incomplete restart history remain limitations. |
| Desktop disks | Owned provisioned size multiplied by its GB-month rate and the fraction of that calendar month. Stopped and retained disks count. Dated inventory begins with collection; earlier days, unobserved disks, snapshots and additional IOPS or throughput are not reconstructed. |
| Shared storage | Each file system's daily billed spend multiplied by the user's dated share of measured bytes. Complete measurements must cover every user on the same file system and establish the allocation pool. |
| AI | Each project's daily AI spend apportioned by that day's user tokens divided by project tokens. Missing spend or attribution, including spend without a token denominator, remains unknown. |

**Estimated costs** means all five facets have coverage. **Known costs** is the subtotal of available amounts. Each month is labelled independently. Unknown amounts display **--**. Confirmed absence of usage displays a real zero, and billing corrections can be negative. The tiles show **Partial**, **No data**, **Estimated share** or **Collecting** only when needed; their keyboard-accessible badges explain coverage for both months.

Daily charts use stored daily amounts. Unknown dates have no bar and appear in a compact **Missing: …** note. **View daily values** includes every elapsed date and its status. Future days and dates absent from shorter months remain blank. A partial bar represents only its known amount, and totals reconcile to the included daily amounts. No monthly figure is spread evenly into invented daily costs.

Open **Storage usage: folders and quotas** to load folder sizes, file ages, bytes unchanged for 90 days and available quotas. This separate view does not delay cost rendering. Open **How it is calculated** at the bottom for all rules, source dates, missing counts and exclusions.

## Collection and refresh

The cluster manager creates a dedicated personal-costs table at startup. Its leader collects every user, including users who have never opened the page, at startup and every 15 minutes. It writes immutable generation records before switching the user's head record. Readers keep one generation for the whole response; old records remain available for in-flight reads. The personal costs, summary and ticker APIs read stored records, with no synchronous billing, inventory, pricing or filesystem work. The summary compatibility API retains its trailing 30-day window. The MTD ticker uses the head's stored current-month total; other configured ticker periods have separate stored projections.

**Refresh** writes a deduplicated request for the collector's next minute check. **Refresh requested** acknowledges that request. The current amounts remain visible while collection runs or a request fails. The portal shares a snapshot cache between Home, My costs and the MTD ticker. It checks every 15 seconds while collection is pending, backs off to a minute after two minutes, and ordinarily checks every five minutes while visible. Billing source reads are cached for six hours and disk rates for a day; requesting refresh cannot make upstream billing arrive sooner.

A user without a generation sees **Collecting · about N min**. The estimate comes from the next scheduled collection and the last measured run duration, initially 20 minutes. An overdue collector shows **Collecting delayed** with an updated estimate. Missing sources produce explicit unavailable facets, never a fabricated zero.

## Storage evidence

Today's byte share is never reused to fill last month. Dated shares and disk observations are retained for 400 days from collection onward. Disk observations cover at most 15 minutes each; earlier observations survive deletion, and missed collection intervals remain unknown. Complete home-directory scans can establish measured bytes, but a home-directory listing alone does not establish the denominator for a filesystem bill. The collector verifies the mounted filesystem before retaining home measurements. For a verifiable EFS root mount, a complete filesystem scan can supply the denominator; users receive only their byte share and system or unrelated bytes remain unassigned. Subdirectory mounts, unverifiable proxy mounts and partial root scans cannot establish that denominator. It leaves shared-storage money unavailable when the allocation pool cannot be separated from system space or unrelated directories.

Incomplete scans, unreadable users, stale measurements, zero denominators and missing billing invalidate an allocation. Volume quota reports without evidence of complete filesystem coverage cannot establish a cost share. A filesystem with valid dated evidence can contribute a known subtotal while uncovered filesystems remain Partial.
