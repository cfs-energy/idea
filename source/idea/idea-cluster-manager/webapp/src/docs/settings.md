## Settings

Open **Administration > Settings**. The page has 32 task-based cards across 12 groups. Choose a group or search labels, descriptions, former tab names, and configuration keys. A result opens its group and highlights the setting.

The groups are **General**, **Desktops**, **Jobs**, **AI access**, **Notifications**, **Users and sign-in**, **Costs**, **Network**, **Storage**, **Backup**, **Monitoring**, and **Deployment**. Each contains task-based cards. **Advanced** holds rarely changed tuning values and opens for a matching search.

Choose **Edit** for one card, then **Save** or **Cancel**. Only changed values are submitted, validation errors preserve drafts, and secret fields store references rather than secret contents.

Runtime cards have no badge. A card shows at most one badge: **Applies after restart**, or **Applies on next upgrade** when it contains a deployment setting. Saving does not perform that restart or upgrade.

Install-time values, generated resource identities, internal wiring, and package or driver pins can be read-only because changing the stored value would not replace deployed infrastructure or installed software. Use the related installation, image, migration, or upgrade workflow.

**Desktop schedule** is one table for all days. **Notifications** is one table for desktop and job events and their templates. Other guided editors keep their own save buttons; there is no page-wide Save button.

Under **Costs > Storage cost collection**, enable ONTAP storage collection, set a read-only username and password secret ARN for each attachment, save, deploy required secret permissions, and restart cluster-manager.

Under **Users and sign-in > Account synchronization**, **Scheduled reconciliation** saves immediately. **Preview changes** and **Run reconciliation** use the saved policy even when scheduling is off. Advanced edits require **Save reconciliation settings**.

### Backfill history

Open **Monitoring > Metrics > Backfill history**. Run a dry run first, review its counts, then clear **Dry run** to send retained job or cost history. Status refreshes every ten seconds. An expired lease reads **interrupted**; review errors and partially sent points before retrying.
