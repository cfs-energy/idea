# Expiring software stacks

A software stack the monthly image rebuild does not feed misses every kernel and fix after it is made. Each such stack can have an owner and an expiry date, so someone decides whether it is still needed.

Stacks on the monthly rebuild have no date. Neither has a stack whose AMI is in the exempt list. Everything else counts as hand-pinned, including a base stack pinned to its image.

| Setting, under `<vdc>.software_stacks.expiry` | Default | Effect |
| --- | --- | --- |
| `default_days` | `90` | For a stack with no date of its own, how long after it was created it expires, once `enforce_from` is set |
| `warn_days` | `14` | The new desktop form warns from this many days before |
| `exempt_amis` | empty | AMI IDs that never expire or warn. Nothing is exempt in code |
| `enforce_from` | empty | `YYYY-MM-DD`, the policy date. Setting it starts the dates of stacks with none of their own. From this day, expired stacks are not offered for new desktops. Empty means only a stack given its own date warns, and nothing is hidden |
| `digest_enabled` | `true` | One email per owner, once a week at most |

**On the new desktop form,** a stack near or past its date carries a warning that names the rebuilt stack for the same system to use instead, when the person's project has one. Once `enforce_from` has come, expired stacks are gone from the form and a request naming one is refused. Desktops already running on them keep working: starting, stopping and rebooting never read the date.

**On the Software Stacks page,** the Owner and Expires columns show each stack. Select one and choose **Extend Expiry** to add `default_days`, counted from today or from the current date, whichever is later. **Edit Stack** sets the owner or a specific date. A stack made from a desktop takes that desktop's owner.

**The weekly digest** lists each owner's stacks in one email. Stacks with no owner go to `cluster.administrator_username`, also as one email. A failed send waits for the next week rather than repeating.

## Upgrading

The upgrade rewrites no stack and changes nothing anyone sees. On a cluster where stacks have no owner and no date, such as every cluster at this release:

* Nothing is hidden, and no stack warns or reads as expired, however old it is, since `enforce_from` starts empty.
* No digest is sent.

To start the policy, add the images that must stay to `exempt_amis`, then set `enforce_from` to a date far enough ahead for people to move. From that moment each hand-pinned stack counts `default_days` from when it was created: those past it warn on the new desktop form and read Expired on the page, and the digest starts. Extend or disable the rest before the date comes. On that day expired stacks leave the form. Clearing `enforce_from` brings hidden stacks back and stops the counted dates at once. A date set on a stack with **Edit Stack** or **Extend Expiry** counts whether or not `enforce_from` is set, but only `enforce_from` hides anything.

## Instances with no desktop

**Diagnostics → Instances with no desktop** lists instances tagged as this cluster's desktops that no desktop row names, older than an hour, so nobody can stop them from the portal. `idea.health.desktop.orphan_instances` carries the count. Nothing acts on them: they may hold data. To check the matching while desktops are new, call `VirtualDesktopAdmin.ListOrphanInstances` with `min_age_minutes: 0`: it then lists every tagged instance no row names, however new, so a running desktop that appears there points at a broken match.

`idea.health.stacks.expiry`, tagged `state` (`expiring`, `expired`), counts enabled stacks, and `idea.health.stacks.expiry_enforced` is 1 once hiding has begun. `metrics.health.stack_expiry.enabled` stops these counts; the digest has its own switch.
