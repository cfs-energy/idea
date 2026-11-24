# SSH access

If needed, you can SSH to the bastion host. First open your IDEA [web-interface.md](web-interface.md "mention") and click "**SSH access**" session on the left sidebar

<figure><img src="../../.gitbook/assets/ftu_access_ssh_menu.webp" alt=""><figcaption></figcaption></figure>

IDEA will offers you two ways to establish an SSH connection. Follow the instructions displayed on the screen whether you are planning to use a regular SSH client or PuTTY (Windows)

<figure><img src="../../.gitbook/assets/ftu_access_ssh_example.webp" alt=""><figcaption><p>Follow the instructions to configure your SSH client</p></figcaption></figure>

## Who can SSH to a host

Everyone can SSH to the bastion. A Linux desktop accepts SSH only from its owner, cluster administrators and the image's local accounts. A compute node launched for one job accepts SSH only from that job's owner, cluster administrators and local accounts, so the owner's multi-node jobs still reach each node over SSH. Cluster administrators are the users sudo admits on the host: members of the directory's sudoers group (`directoryservice.sudoers.group_name`) on Active Directory, or users with an LDAP sudo rule on OpenLDAP. Local accounts (root, the image's default user such as `ec2-user`, `rocky` or `ubuntu`, and `ssm-user`) keep the access they had. Session Manager, DCV sessions, desktop sharing and the browser terminal for the owner are unaffected.

Compute nodes on shared capacity (keep-forever or terminate-when-idle queues) run several users' jobs and still accept SSH from any directory user.

The rule is a PAM account check in `/etc/pam.d/sshd` that runs `/usr/local/libexec/idea-ssh-access`; the allowed owner is in `/etc/idea/ssh-allowed-users`, written at every host bootstrap. A refused user gets the normal SSH refusal, and the host logs `idea-ssh-access: refused SSH for <user> from <address>: not this host's owner or a cluster administrator` to the auth log (`/var/log/secure`, `/var/log/auth.log` or the journal). Running Linux desktops get the rule from the desktop refresh: a desktop gets it when it next becomes ready after a start or resume, or from the six-hourly check described under post-quantum key exchange, without an SSH restart. Compute nodes launched before this release keep the earlier behaviour until they are replaced.

## Post-quantum key exchange

On supported Linux hosts, the SSH configuration puts supported post-quantum key exchange algorithms ahead of the existing algorithms. Hybrid ML-KEM is included when the installed SSH software supports it. Older servers that offer no supported post-quantum algorithm keep their existing configuration.

For running desktops, the controller checks every six hours and attempts at most five refreshes per pass. Only READY Linux desktops with a server instance and no recorded successful refresh are eligible. Larger fleets take multiple passes; stopped desktops are skipped until READY. The SSH service restarts after the new configuration passes validation. Open SSH sessions stay connected.

### Bastion SSH source addresses

`ssh_client_ip` in values.yml is the source of truth for `cluster.network.ssh_client_ip`.
Each upgrade replaces the stored list, including removing addresses. An empty list or an
omitted key restores the default cluster and operator prefix lists for SSH. The upgrade
preview labels changed lists as `VALUES_UPDATE` and shows the old and new addresses. Replacing a differing stored list requires drift confirmation (or `--accept-config-drift` for unattended runs). Both IP settings must be lists of IPv4 addresses or network CIDRs without host bits; addresses normalize to `/32` and duplicates are removed. `ideactl config set` can change the setting
immediately, but the next upgrade restores values.yml; edit that file for a lasting change.
