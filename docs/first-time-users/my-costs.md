---
description: My Costs shows each user what the cluster recorded against them over the last 30 days.
---

# My Costs

Click "**My Costs**" under **Home** on the left sidebar. It always shows your own numbers: the request carries no username and the server reads yours from your session token, so there is no way to ask it about another user, administrators included. An administrator who needs someone else's numbers uses the admin pages.

Everything on the page covers a trailing 30 day window ending today.

The sections appear in the order AI usage, Desktops, Jobs. If you have not used a model in the window the AI section is not shown at all. It does appear, with a message, when IDEA could not read your usage, because a failed read is not the same as no usage.

## AI usage

One row per project you belong to and used, with the requests and tokens IDEA attributed to you, and your share of that project's Bedrock spend. Expand a row to see the same figures per model.

The cost is an apportionment, not a measurement. IDEA reads the project's Bedrock spend for the window, then splits it by your share of the project's tokens. If two people in a project spent tokens on different models at different prices, the split will not match what each of them actually cost.

When the project's spend cannot be read, the row shows **Not available** rather than a zero.

## Jobs

The jobs you completed in the window, taken from the cost estimate the scheduler recorded for each one at submission time. The totals and the per project and per queue breakdowns cover every job in the window; the table underneath lists your 20 most recent.

A job's estimate is what the scheduler predicted the job would cost, not what the instances it ran on were billed at. Spot pricing, savings plans and reserved capacity are not reflected.

## Desktops

Your virtual desktop sessions that were up at some point in the window, with the hours IDEA recorded and those hours priced at the public on-demand rate for the instance type.

A desktop you have since deleted still appears, shown as **Terminated**, and still counts toward your hours and cost for the time it ran. IDEA keeps a small record of a terminated desktop for 400 days, which is what this page reads once the desktop itself is gone.

IDEA does not record billable desktop uptime, so the hours are derived: a running session is counted to now, and a session that is no longer running is counted to its last update. A session that was stopped and then edited reads long, and a session stopped and restarted inside the window reads as one stretch.

When there is no on-demand price for an instance type, the row shows its hours and **Price not available** instead of a cost.

The same rule applies to the section total. If none of your sessions could be priced the Cost reads **Not available** rather than $0.00. If only some could, the total carries a `*` and a line underneath saying how many sessions are missing from it. Job costs behave the same way when the scheduler recorded no estimate for a job.

{% hint style="info" %}
If every desktop reads **Price not available**, the cluster-manager role is probably missing `pricing:GetProducts`. That permission ships with the cluster-manager policy, so an existing cluster needs the module redeployed to pick it up. Tell your administrator.
{% endhint %}

## What this page is not

{% hint style="warning" %}
These are IDEA's own measurements, not your AWS bill, which is why every figure is labeled **Estimated**. Nothing on this page comes from AWS Cost Explorer and nothing is reconciled against an invoice. Use it to see the shape of what you are consuming, not to settle a chargeback.
{% endhint %}

Costs that IDEA never sees do not appear here at all: storage, data transfer, the cluster's own infrastructure, and anything you launched outside IDEA. A section shows as unavailable when the underlying read failed or the module is not deployed. An empty section means IDEA recorded nothing for you in the window.

## For administrators

Administrators get a second, all-users view at **Cluster Management > User Costs**. It lists every user with a measured cost in the window, one row each, with AI tokens and cost, desktop hours and cost, job count and cost, and a total. Selecting a user shows the same three sections that user sees on their own page.

The two pages read the same measurements and apply the same rules, including **Not available** in place of a misleading zero. Where a whole source could not be read, the listing says so above the table.

### Pricing outside the commercial partition

The AWS Price List Query API has a single commercial endpoint and none in the GovCloud or China partitions, so on those partitions IDEA prices instance hours from the public AWS price list files instead. It reads the EC2 offer file for the cluster region over HTTPS from `pricing.us-east-1.amazonaws.com`, so the cluster needs outbound HTTPS to that host; no credentials are sent and the file is public.

The offer file is a few hundred megabytes, so it loads on a background thread and refreshes once a day, and desktop and job costs read **Not available** until the first load finishes. A load that fails is logged and not retried for six hours. The same fallback is used in the commercial partition when the Pricing API call itself fails.

Access is enforced on the server, not by hiding the link. The admin API requires elevated access, and the self-scoped one accepts no username at all.
