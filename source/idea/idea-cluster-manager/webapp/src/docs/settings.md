## Settings

Open **Administration > Settings** and choose a group, or search labels, descriptions and configuration keys in the results table. Selecting a result opens its group and highlights the setting.

Each section starts as key-value pairs; choose **Edit**, then **Save** or **Cancel**, and finish that section before editing another one. Only changed values are submitted, validation errors preserve edits, and secret fields store references rather than secret contents.

Only settings that need a restart or upgrade have an effect badge:

| Badge | When the saved value takes effect |
| --- | --- |
| Restart required | After the affected service restarts. Save does not restart it. |
| Applies on next upgrade | When the affected deployment stacks or bootstrap configuration are applied during the next upgrade. Save records the value without starting an upgrade. |

**Advanced** at the bottom of each subpage contains tuning settings and is collapsed by default. Searching for an advanced setting opens it automatically. Some older keys have no confirmed live reader; their descriptions explicitly identify the conservative restart classification. A restart is not a guarantee that an unused compatibility key has an effect.

| Group | Editors |
| --- | --- |
| Portal appearance and reports | Portal branding, embedded reports, locale and timezone, the optional personal cost header and the default landing page. |
| Desktop access and lifecycle | Desktop defaults, daily schedules, idle behaviour, cleanup and launch settings. |
| Job limits and placement | Admission, placement and scheduler tuning; queue overrides remain in Manage jobs. |
| AI access and spending | Model access, desktop defaults, usage collection and budget settings. |
| Account synchronization | Periodic reconciliation policy and external directory secret references. |
| Email and notifications | Templates, notification policies and event-to-template mappings. |
| Maintenance notice | Notice text, enabled state and optional end time. |
| **Advanced** | **Rarely changed settings below this heading.** |
| Sign-in and directory | Directory, identity-provider and session settings. |
| Network access | Network policy, encryption, service capacity and server tuning. |
| Storage | Mount settings, storage provisioning options and existing attachment settings. |
| Backup and recovery | Cluster and desktop backup policies and default rules. |
| Cost collection | Cost and storage collection intervals, rates and credentials. |
| Monitoring and logs | Analytics, metrics, logging and Backfill history controls. |
| Resource tags | Global resource tags; project tags remain in Projects. |
| Deployment details | Region, version, package and driver configuration, and a link to Operations. |

Generated resource identities, internal module wiring and one-time installation state are not writable settings. The external directory's approved token destinations remain an independently managed trust boundary.

The backend catalog defines the available keys, types, validation and effects. Groups and editors depend on administrative access and deployed modules. Storage fields are available for existing attachments; attaching or migrating a filesystem still requires its storage workflow. Changing directory providers or replacing infrastructure also requires the associated migration procedure.

The portal appearance group can enable a cached personal cost header, choose its WTD, MTD, QTD or YTD period, and choose the default destination opened after sign-in. These settings apply at runtime. Users choose **Cluster default** or their own destination under **My account**.

The existing guided editors keep their save behaviour: desktop dialogs and the AI enablement dialog save on **Save**; **Add Model** saves immediately; removing a model saves after confirmation. Email templates use their create or update form. The **Reconciliation on** switch saves immediately and notifies its worker. Other reconciliation edits require **Save reconciliation settings**. Maintenance edits require **Save**. There is no page-wide Save button.

### Backfill history

Open **Administration > Settings > Monitoring and logs** and find **Backfill history**. Backfill rebuilds metrics from retained finished jobs and historical cost rows, using their original timestamps. Enable historical ingestion for each destination `idea.*` metric before sending points.

For jobs, select **Start date (UTC)** and **End date (UTC, inclusive)** within the last fifteen months. For costs, expand **Advanced** and set **Cost days** to the number of trailing full days, excluding today. The default is 400 days; the maximum reaches back fifteen months.

Leave **Dry run** selected and choose **Run jobs** or **Run cost** first. A dry run reads the source and builds counts without sending points. Review the result, then clear **Dry run** and run again to send the history. Available history depends on the retained source data. Unsupported metric types, including distributions, are skipped.

The **Jobs** and **Cost rows** status lines refresh every ten seconds. They show state, dry-run mode, rows scanned, points built, sent and skipped, errors, and the last error when present. Sent points have been accepted by the destination API; this does not establish when they appear in reports.

Each backfill service allows one run at a time. Its button is disabled while that run is running or status is unavailable. Runs execute in the background and save their status. A restart does not resume the run; an expired lease is shown as **interrupted**. Review errors and any partially sent points before starting another run.
