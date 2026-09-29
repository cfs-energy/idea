## Reporting

Reporting is read-only. Cluster administrators and managers receive access automatically. Membership in the configured operations-lead group adds Reporting without granting administration; module-only administrators do not receive it automatically.

Configure the group under **Settings > Users and sign-in > Sign-in**. It must not match an administrator, manager, or module group.

Choose **This month**, **Last month**, **Last 30 days**, or a custom inclusive range of at most 366 days in the cluster timezone. The tiles and tables use stored cost, completed-job, and desktop records. Read the coverage details: missing or unavailable data is not zero.

Use **Overview / By user**, **By project**, or **By facet**. Project attribution covers recorded job-compute spend; other cost facets can remain unallocated. Choose **Reporting preferences** to set the page size and column order.

**Export CSV** downloads every row in the selected table with the current sort and visible-column order. If the snapshot has expired, choose **Reload snapshot** first.
