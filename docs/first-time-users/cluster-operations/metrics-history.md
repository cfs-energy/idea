# Backfill history

Open **Administration > Settings > Monitoring and logs - Read-only** and find **Backfill history**. Backfill rebuilds metrics from retained finished jobs and historical cost rows, using their original timestamps. Enable historical ingestion for each destination `idea.*` metric before sending points.

For jobs, select **Start date (UTC)** and **End date (UTC, inclusive)** within the last fifteen months. For costs, expand **Advanced** and set **Cost days** to the number of trailing full days, excluding today. The default is 400 days; the maximum reaches back fifteen months.

Leave **Dry run** selected and choose **Run jobs** or **Run cost** first. A dry run reads the source and builds counts without sending points. Review the result, then clear **Dry run** and run again to send the history. Available history depends on the retained source data. Unsupported metric types, including distributions, are skipped.

The **Jobs** and **Cost rows** status lines refresh every ten seconds. They show state, dry-run mode, rows scanned, points built, sent and skipped, errors, and the last error when present. Sent points have been accepted by the destination API; this does not establish when they appear in reports.

Each backfill service allows one run at a time. Its button is disabled while that run is running or status is unavailable. Runs execute in the background and save their status. A restart does not resume the run; an expired lease is shown as **interrupted**. Review errors and any partially sent points before starting another run.
