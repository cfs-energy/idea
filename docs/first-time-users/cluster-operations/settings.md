# Settings

Open **Administration > Settings**. The page consolidates settings into 32 task-based cards across 12 groups. Choose a group, or search group names, former tab names, labels, descriptions, and configuration keys. A result opens the group and highlights the setting. Existing Settings links redirect to the matching group.

Choose **Edit** on one card, change its values, then choose **Save** or **Cancel** before editing another card. Only changed values are submitted. Validation errors preserve the draft. Secret fields store Secrets Manager references, not secret contents.

## Groups and cards

| Group | Cards |
| --- | --- |
| General | Portal appearance, Regional defaults, Maintenance notice |
| Desktops | Desktop placement, Desktop policy, Desktop schedule, Stopped desktop cleanup |
| Jobs | Job limits and placement, Fair-share scheduling, Scratch storage |
| AI access | Amazon Bedrock access, models, usage, and spending controls |
| Notifications | Email delivery, Notifications, Email templates |
| Users and sign-in | Account synchronization, Sign-in, Directory connection, Directory mapping, AD automation |
| Costs | Cost collection, Storage cost collection, Cost estimation |
| Network | Encryption and access, Load balancers and certificates, Network and connectivity |
| Storage | File systems |
| Backup | Backup policy |
| Monitoring | Analytics, Logs, Metrics and Backfill history |
| Deployment | Installed deployment, Resource tags |

The **Advanced** section in a card is collapsed by default. Search opens it when the matching setting is there. Use Advanced for tuning values that are rarely changed.

## Effects and read-only values

Runtime settings have no badge. A card shows at most one effect badge: **Applies after restart**, or **Applies on next upgrade** when any editable value in the card has a deployment effect. Saving does not restart a service or start an upgrade.

Installed regions, generated resource identities, internal wiring, one-time installation values, and pinned package or driver values can be read-only. A portal edit would not safely recreate infrastructure or replace installed software, so use the relevant installation, migration, image-build, or upgrade workflow instead.

Availability depends on your module permissions and the deployed modules. Attaching or migrating a filesystem, changing a directory provider, and replacing infrastructure still require their dedicated workflows.

## Schedule and notification tables

Edit the single **Desktop schedule** table under **Desktops**. It contains every day, schedule type, and any custom start or stop time. Save the table once; do not edit separate day cards.

Edit the single **Notifications** table under **Notifications**. It combines desktop and job events, enablement, and email-template selection. Turning a master switch off retains the event rows but prevents delivery.

The remaining guided editors keep their own save buttons. There is no page-wide Save button.

## Costs and reconciliation

Open **Costs > Storage cost collection** to enable storage costs shown under **Costs and usage > By user**. Configure a read-only ONTAP username and password secret ARN for every attachment, save, deploy required secret permissions, and restart cluster-manager. See [Costs by user](../../modules/cluster-manager/costs-by-user.md).

Under **Users and sign-in > Account synchronization**, **Scheduled reconciliation** saves immediately and notifies the periodic worker. Advanced policy edits require **Save reconciliation settings**. **Preview changes** and **Run reconciliation** use the saved policy and remain available when scheduling is off. See [Account synchronization and reconciliation runs](account-reconciliation.md).

See [Backfill history](metrics-history.md) for retained-job and cost metric backfills.
