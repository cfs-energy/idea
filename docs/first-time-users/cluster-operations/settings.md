# Settings

Open **Administration > Settings**. Choose a group to open its own subpage, or search group names, former tab names, labels, descriptions and configuration keys. A matching setting opens its subpage and scrolls to its editor. Links can be bookmarked. Existing Settings links still open the corresponding group.

Edit text, numbers, switches, choices or lists, then select **Save** for that section. Only changed values in that section are submitted. Validation errors keep your edits available to correct and retry. Secrets use references to Secrets Manager, with an **Open secret** link; do not enter secret contents.

Every setting has an effect badge:

| Badge | When the saved value takes effect |
| --- | --- |
| Applies now | When the module next reads its refreshed configuration. Refresh can take several seconds. Defaults used for new desktops or jobs apply to new launches. |
| Applies when the service restarts | After the affected service restarts. Save does not restart it. |
| Applies on the next upgrade | When the affected deployment stacks or bootstrap configuration are applied during the next upgrade. Save records the value without starting an upgrade. |

**Advanced** at the bottom of each subpage contains tuning settings and is collapsed by default. Searching for an advanced setting opens it automatically. Some older keys have no confirmed live reader; their descriptions explicitly identify the conservative restart classification. A restart is not a guarantee that an unused compatibility key has an effect.

| Group | Editors |
| --- | --- |
| Portal appearance and reports | Portal branding, embedded reports, locale and timezone, the optional personal cost header and the default landing page. |
| Desktop access and lifecycle | Desktop defaults, daily schedules, idle behaviour, cleanup and launch settings. |
| Job limits and placement | Admission, placement and scheduler tuning; queue overrides remain in Manage jobs. |
| AI access and spending | Model access, desktop defaults, usage collection and budget settings. |
| Account synchronization | Periodic reconciliation policy and external directory secret references. |
| Maintenance notice | Notice text, enabled state and optional end time. |
| Email and notifications | Templates, notification policies and event-to-template mappings. |
| Mail delivery | Mail sender, region, delivery switch and sending rate. |
| Sign-in and directory | Directory, identity-provider and session settings. |
| Network access | Network policy, encryption, service capacity and server tuning. |
| Backup and recovery | Cluster and desktop backup policies and default rules. |
| Cost collection | Cost and storage collection intervals, rates and credentials. |
| Monitoring and logs | Analytics, metrics, logging and Backfill history controls. |
| Resource tags | Global resource tags; project tags remain in Projects. |
| Storage | Mount settings, storage provisioning options and existing attachment settings. |
| Software and drivers | Package and driver configuration. |

**Deployment details** is a compact information subpage linked to Operations. Generated resource identities, internal module wiring and one-time installation state are not writable settings. The external directory's approved token destinations remain an independently managed trust boundary.

The backend catalog defines the available keys, types, validation and effects. Groups and editors depend on administrative access and deployed modules. Storage fields are available for existing attachments; attaching or migrating a filesystem still requires its storage workflow. Changing directory providers or replacing infrastructure also requires the associated migration procedure.

Under **Portal appearance and reports**, `cluster-manager.web_portal.cost_ticker.enabled` controls the personal cost header and `.period` selects WTD, MTD, QTD or YTD. `cluster-manager.web_portal.default_landing_page` selects the destination used after sign-in and at the portal root. All three apply at runtime. A user can override only their own landing page from **My account**; **Cluster default** follows this administrator setting.

The existing guided editors keep their save behaviour: desktop dialogs and the AI enablement dialog save on **Save**; **Add Model** saves immediately; removing a model saves after confirmation. Email templates use their create or update form. The **Reconciliation on** switch saves immediately and notifies its worker. Other reconciliation edits require **Save reconciliation settings**. Maintenance edits require **Save**. There is no page-wide Save button.

See [Account synchronization and reconciliation runs](account-reconciliation.md) and [Backfill history](metrics-history.md) for run controls.
