# Operating browser terminals

Terminal uses the existing portal login and cluster-manager ALB route. No public SSH listener or bearer token in a WebSocket URL is introduced. The only WebSocket route is `<cluster-manager API prefix>/terminal`. Its Origin must match the configured HTTPS portal origin. DNS names are compared without case or a trailing dot, and port 443 is the default; other ports must match exactly.

## Settings and deployment

Administrators can edit the four runtime controls in **Administration > Settings > Portal**. The CA public key is read-only. Timeout changes apply to new connections; the disable switch also closes active connections.

| Setting | Default | Meaning |
| --- | --- | --- |
| `cluster-manager.web_terminal.enabled` | `true` | Show Terminal and accept new connections. Disabling also closes active connections at the next watchdog check. |
| `cluster-manager.web_terminal.idle_timeout_seconds` | `1800` | Close connections without user input. Output and keepalives do not reset this clock. |
| `cluster-manager.web_terminal.max_sessions_per_user` | `3` | Account-wide connection limit, clamped to 1–10. |
| `cluster-manager.web_terminal.max_duration_seconds` | `43200` | Maximum duration of a connected transport. |
| `cluster-manager.web_terminal.ca_public_key` | Published at startup | Public OpenSSH user CA installed in host trust files. Never put the private key in settings. |

Deploy cluster-manager before updating bastions or launching new desktops. It initializes `<cluster>.cluster-manager.terminal`, including TTL, and the CA even when terminal access is disabled. A provisioning failure logs its error code and leaves Terminal unavailable while the rest of cluster-manager starts; requests retry initialization at most once every 30 seconds per replica, logging each failure. Correct the logged cause; the next request after the cooldown retries automatically. The stack creates `<cluster>-<module>-terminal-ca` in Secrets Manager, encrypted using `cluster.secretsmanager.kms_key_id` (the cluster's configured key). First startup atomically replaces the seed with an ed25519 private key. The resource policy restricts CA reads, `PutSecretValue`, version-stage changes, restore, and rotation to the cluster-manager ECS task role. `UpdateSecret`, `DeleteSecret`, `PutResourcePolicy`, and `DeleteResourcePolicy` also exempt the stack's CloudFormation execution role so stack updates and deletes work. Protect that role like the signing task role: anyone who can assume that role can replace the CA or remove its policy protections. Identity permissions must also allow the operation. The application does not persist ephemeral client keys or certificates.

For `Terminal CA reset to seed; restore the current CA`, restore the previous valid CA secret version to `AWSCURRENT`, then restart cluster-manager. Do not generate a new CA to repair an accidental seed reset.

The bastion image includes tmux and starts `common/terminal_host.py` in the background after ordinary SSH starts. Linux desktop bootstrap installs tmux and system Python, then runs the same helper using the already installed AWS CLI. On desktops, `idea-terminal-trust.service` refreshes trust on every boot, independently of SSH startup, using `/usr/local/libexec/idea-terminal-host.py` and `/etc/environment` (including the cluster proxy settings). A systemd sleep hook clears trust readiness before hibernation and schedules another refresh on resume. The principal command rejects IDEA certificates until that boot or resume has successfully refreshed trust; a failed refresh leaves ordinary SSH and other site CAs available. Trust installation runs even when terminal access is disabled and is non-fatal to ordinary SSH. Desktop bootstrap queues the trust service without waiting; the service retries every 30 seconds after each failed helper invocation until it succeeds. Bastions retry failed trust setup every 30 seconds after each bounded attempt and reload sshd on success. A stopped desktop ends connection attempts and directs its owner to start it from My desktops before selecting Connect; provisioning and resume states remain retryable. SSH authentication refusals during boot or resume are retried within the client's eight-attempt budget; persistent refusals then request administrator attention. Desktop retries also recover from delayed network or proxy availability; persistent failures are logged to the service journal. Browser access fails closed until trust and the pin are installed.

The helper reads the public CA from settings. It appends the IDEA key to the effective `TrustedUserCAKeys` file if the site already has one, preserving site keys and replacing only the previous IDEA key on rotation. Otherwise it installs `/etc/ssh/idea_terminal_ca.pub`. It installs a principal command which accepts IDEA certificates only for SSSD users with no local `/etc/passwd` identity. IDEA certificate principals are namespaced as `idea:<username>` so implicit username matching cannot bypass that check. Other site CAs retain their implicit username principal. An existing site `AuthorizedPrincipalsFile` or different `AuthorizedPrincipalsCommand` requires explicit integration: the helper stops before changing trust rather than replacing the site's principal policy. Password and keyboard-interactive authentication remain disabled on bastions, and PAM account checks remain in force.

**Older desktops:** launch a new Linux desktop after upgrading the cluster to 26.11.1. Stopping and starting a desktop created before the upgrade does not install this bootstrap configuration. Copy any needed local data before terminating an old desktop; shared home data remains available on the new desktop.

On a desktop already provisioned with this release, refresh trust as root (for example through an SSM shell):

```bash
sudo systemctl restart idea-terminal-trust.service
sudo systemctl is-active idea-terminal-trust.service
sudo test -f /run/idea-terminal-trust-ready
sudo sshd -t
sudo systemctl try-reload-or-restart sshd || sudo systemctl try-reload-or-restart ssh
```

Check `journalctl -u idea-terminal-trust.service` if refresh fails. Do not insert an unverified host key into the terminal table.

Host pins live in the terminal table under `host:bastion` or `host:<EC2 instance ARN>`. A desktop role can write only the row matching its own EC2 credential identity. The bastion role can write only its bastion row. Cluster-manager fails closed on missing or mismatched host keys. Container bastion connections use the private IP of a running task, selected deterministically while the task set stays unchanged. Different targets have separate tmux sessions. Container replacements do not preserve tmux processes.

Cluster-manager is added as an SSH source on the desktop security group; desktops also accept traffic from the VPC CIDR and the bastion. All targets are chosen by the server: an owned READY Linux session and its running EC2 instance in this cluster, or the configured bastion. Client-supplied hosts, ports, usernames, or commands are not accepted by the ticket API.

## Tickets, limits, and audit

`Terminal.CreateTicket` requires a normal authorized user access token. Tickets last at most 30 seconds (integer expiry timestamps can shorten this by less than one second), are single-use across replicas, and bind the authenticated user, target, and random browser secret. The WebSocket sends the ticket and binding in its first message, never the URL. The server checks Origin before upgrading, consumes the ticket atomically, and rechecks the enabled account and target before SSH login.

Each login mints a five-minute certificate for the account's POSIX login with principal `idea:<username>`. Its validity starts 60 seconds before minting to tolerate clock skew and ends five minutes after minting. Its key ID contains the terminal session ID; its only extension is `permit-pty`. SSH checks validity at login, so expiry never cuts a live shell. Reconnects require a fresh ticket, key, and certificate.

Conditional DynamoDB leases (three by default) enforce the account-wide connection limit across replicas. They renew every 15 seconds and expire after 60 seconds; failed renewal closes the connection. After a process crash, a stale slot can take up to a minute to become available. A 32 KiB WebSocket frame limit, 16 KiB output chunks, receipt acknowledgements, bounded SSH windows, and write deadlines constrain memory and slow clients. The duration and idle watchdog runs every 15 seconds.

Service audit entries contain start/stop, username, target ID, terminal session ID, elapsed seconds, and a fixed close reason. Ticket API payloads are redacted. Terminal content and keystrokes must never be logged. Do not enable AsyncSSH packet debugging.

## Rotate the CA

Rotation changes trust and requires a coordinated rollout. It does not terminate established SSH connections by itself.

1. Disable `cluster-manager.web_terminal.enabled` and wait for active browser connections to close (the watchdog checks every 15 seconds). Keep ordinary SSH available for recovery. Keep the setting disabled throughout rotation: signing reads the current secret on every new login, so a key write takes effect immediately.
2. Use ECS Exec to open `/bin/bash` in a running cluster-manager application container (`cluster-manager-container`), using the intended account, region, cluster, and task. The command runs with that task's signing role; an operator cannot assume the ECS task role directly. Run the following inside the container. It verifies the live disable flag, creates the key in memory, and writes a fresh immutable secret version without printing private material. Do not enable Python/SDK debug logging.

   ```python
   import os
   import uuid
   import asyncssh
   import boto3

   cluster = os.environ['IDEA_CLUSTER_NAME']
   module = os.environ['IDEA_MODULE_ID']
   settings = boto3.resource('dynamodb').Table(cluster + '.cluster-settings')
   row = settings.get_item(
       Key={'key': 'cluster-manager.web_terminal.enabled'}, ConsistentRead=True
   ).get('Item', {})
   if row.get('value') is not False:
       raise RuntimeError('Disable Terminal before rotating the CA')
   key = asyncssh.generate_private_key('ssh-ed25519')
   boto3.client('secretsmanager').put_secret_value(
       SecretId=cluster + '-' + module + '-terminal-ca',
       ClientRequestToken=uuid.uuid4().hex,
       SecretString=key.export_private_key().decode(),
   )
   print(key.export_public_key().decode().strip())
   ```

   Start `python3.13` in the ECS Exec shell to run this snippet. Only the public key is printed. Version-stage reassignment is task-role-only. The CloudFormation execution role also has the mutation exception described above, including `UpdateSecret` and resource-policy changes; use the task-credential procedure here for rotation.
3. Restart cluster-manager while the setting remains disabled. Startup validates the secret and publishes its public key even with access disabled. Confirm the public fingerprint against step 2.
4. While the setting remains disabled, replace bastion tasks and run the desktop refresh commands above on every running Linux desktop. Stopped desktops provisioned with the boot service refresh independently of SSH startup when they next boot (or refresh after hibernation resume); IDEA certificates remain denied if refresh fails. Inventory all older desktops without that service, including stopped desktops: migrate their data and terminate them before re-enabling Terminal. Do not leave an offline host trusting a compromised CA.
5. Verify the new public key on every running host, a successful `idea-terminal-trust.service` on desktops, and removal of all older desktops without the boot service. Then re-enable Terminal and verify a new connection. A stopped desktop with the boot service must pass refresh when it next starts.

## Renderer

The terminal page pins `ghostty-web` 0.4.0, using libghostty VT compiled to WebAssembly and its canvas renderer. The built-in fit addon drives the existing resize/SIGWINCH messages. Selection, clipboard paste, font controls, the mobile key bar, reconnects, and tmux reattachment remain available. xterm.js remains on the log-tail page only.

The renderer module loads only when the terminal mounts. Vite emits the packaged WASM as a hashed `static/media/ghostty-vt.*.wasm` asset served by the portal. No CDN or external asset host is used. Connect stays disabled until initialization completes; a loading failure asks the user to reload.

This repository does not set a portal Content-Security-Policy, so no CSP change is needed here. If a deployment adds a CSP at a reverse proxy, permit the portal origin in `script-src` and `connect-src`, and add `'wasm-unsafe-eval'` to `script-src` for WASM compilation. Broad `'unsafe-eval'` permission is unnecessary. Keep any existing nonce/hash allowances for portal bootstrap scripts.

The adapter preserves single-click HTTP/HTTPS links with `noopener,noreferrer` and blocks other URI schemes. A `beforeinput` bridge handles phone keyboards while ghostty-web retains IME composition. Shift Escape uses ghostty-web's handled-key convention to move focus to an enabled control.

Runtime theme changes use the public canvas render API and recolor default text/background in both the screen and scrollback without resetting the session. Version 0.4.0 exposes resolved RGB values rather than default-color flags, so explicitly colored `#cccccc` text and black backgrounds also follow the portal theme. Its link detector does not join URLs across wrapped lines or expose OSC 8 link destinations. Screen-reader terminal output is out of scope for this HPC terminal ([upstream issue 187](https://github.com/coder/ghostty-web/issues/187)); there is no alternate renderer or accessibility toggle.
