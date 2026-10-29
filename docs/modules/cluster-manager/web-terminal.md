# Operating browser terminals

Terminal uses the existing portal login and cluster-manager ALB route. No public SSH listener or bearer token in a WebSocket URL is introduced. The only WebSocket route is `<cluster-manager API prefix>/terminal`. Its Origin must match the configured HTTPS portal origin. DNS names are compared without case or a trailing dot, and port 443 is the default; other ports must match exactly.

## Settings and deployment

Administrators can edit the four runtime controls in **Administration > Settings > Portal**. The CA public key is read-only. Timeout changes apply to new connections; the disable switch also closes active connections.

| Setting | Default | Meaning |
| --- | --- | --- |
| `cluster-manager.web_terminal.enabled` | `true` | Show the Shell button and desktop Terminal actions, and accept new connections. Disabling also closes active connections at the next watchdog check. |
| `cluster-manager.web_terminal.idle_timeout_seconds` | `1800` | Close connections without user input. Output and keepalives do not reset this clock. |
| `cluster-manager.web_terminal.max_sessions_per_user` | `3` | Account-wide connection limit, clamped to 1–10. |
| `cluster-manager.web_terminal.max_duration_seconds` | `43200` | Maximum duration of a connected transport. |
| `cluster-manager.web_terminal.ca_public_key` | Published at startup | Public OpenSSH user CA installed in host trust files. Never put the private key in settings. |

Deploy cluster-manager before updating bastions or launching new desktops. It initializes `<cluster>.cluster-manager.terminal`, including TTL, and the CA even when terminal access is disabled. A provisioning failure logs its error code and leaves Terminal unavailable while the rest of cluster-manager starts; requests retry initialization at most once every 30 seconds per replica, logging each failure. Correct the logged cause; the next request after the cooldown retries automatically. The stack creates `<cluster>-<module>-terminal-ca` in Secrets Manager, encrypted using `cluster.secretsmanager.kms_key_id` (the cluster's configured key). First startup atomically replaces the seed with an ed25519 private key. The resource policy restricts `PutSecretValue`, version-stage changes, restore, and rotation to the cluster-manager ECS task role. CA reads, `UpdateSecret`, `DeleteSecret`, `PutResourcePolicy`, and `DeleteResourcePolicy` also exempt the stack's CloudFormation execution role so stack updates and deletes work. Protect that role like the signing task role: anyone who can assume that role can read or replace the CA or remove its policy protections. Identity permissions must also allow the operation. The application does not persist ephemeral client keys or certificates.

For `Terminal CA reset to seed; restore the current CA`, restore the previous valid CA secret version to `AWSCURRENT`, then restart cluster-manager. Do not generate a new CA to repair an accidental seed reset.

The bastion image includes tmux and starts `common/terminal_host.py` in the background after ordinary SSH starts. Linux desktop bootstrap installs tmux and system Python, then runs the same helper using the already installed AWS CLI. On desktops, `idea-terminal-trust.service` refreshes trust on every boot, independently of SSH startup, using `/usr/local/libexec/idea-terminal-host.py` and `/etc/environment` (including the cluster proxy settings). A systemd sleep hook clears trust readiness before hibernation and schedules another refresh on resume. The principal command rejects IDEA certificates until that boot or resume has successfully refreshed trust; a failed refresh leaves ordinary SSH and other site CAs available. Trust installation runs even when terminal access is disabled and is non-fatal to ordinary SSH. Desktop bootstrap queues the trust service without waiting. It is a background (`Type=simple`) service, so boot targets, cloud-init, and bootstrap reboots never wait for trust. It retries every 30 seconds after each failed helper invocation until it succeeds, except when the site principal policy described below needs explicit integration: the helper then exits with status 78 and the unit stays `failed` until the site policy is integrated. While retrying, the unit is `active (running)`; readiness is only `/run/idea-terminal-trust-ready`. Trust edits `sshd_config` while holding `/run/idea-terminal-trust.lock`, and desktop bootstrap runs `userdata_customizations.sh` under the same lock; site tooling that edits `sshd_config` later should take it too (`flock /run/idea-terminal-trust.lock <command>`). Before every reload the helper checks the file sshd will load, drop-ins included, with `sshd -t`; if sshd rejects it, the helper restores the previous `sshd_config`, skips the reload and fails the attempt, so a bad edit never reaches the running sshd. A failed sshd reload is retried with the next refresh attempt. Bastions retry failed trust setup every 30 seconds after each bounded attempt, stop on status 78, and reload sshd on success. A stopped desktop ends connection attempts and directs its owner to start it from My desktops before selecting Connect (the desktop's Terminal action is disabled while it is stopped); provisioning and resume states remain retryable. SSH authentication refusals are treated as trust still refreshing, since sshd starts before trust lands after every launch, OS reboot, resume, or bastion task start. They are retried within the client's eight-attempt budget (about a minute); a refusal that persists past it requests administrator attention. A desktop whose instance is stopped while My desktops still shows it as ready is retried until the desktop state catches up, and a desktop in an error or deleting state ends with a message pointing to My desktops. Desktop retries also recover from delayed network or proxy availability; persistent failures are logged to the service journal. Browser access fails closed until trust and the pin are installed.

The helper reads the public CA from settings. It appends the IDEA key to the effective `TrustedUserCAKeys` file if the site already has one, preserving site keys and replacing only the previous IDEA key on rotation. Otherwise it installs `/etc/ssh/idea_terminal_ca.pub`. It installs a principal command which accepts IDEA certificates only for SSSD users with no local `/etc/passwd` identity. IDEA certificate principals are namespaced as `idea:<username>` so implicit username matching cannot bypass that check. Other site CAs retain their implicit username principal. An existing site `AuthorizedPrincipalsFile` or different `AuthorizedPrincipalsCommand` requires explicit integration: the helper stops before changing trust rather than replacing the site's principal policy. After integrating that policy, run the refresh commands below. Password and keyboard-interactive authentication remain disabled on bastions, and PAM account checks remain in force.

**Older desktops:** a Linux desktop launched before 26.11.1 gets the same helper, unit and sleep hook from the desktop refresh (refresh version 5): the next time it reaches READY after a start or resume, or within six hours from the background sweep. The refresh installs tmux and system Python if missing, replaces each file only when it differs, and starts the trust service only when something changed or the service is not running, so running it again changes nothing and leaves sshd alone. The refresh itself never edits `sshd_config`; the helper does, as above. Until the desktop's host key is published, its shell button stays disabled with "Terminal becomes available after this desktop next starts." A refresh that cannot install the packages fails and is retried by the next sweep. Windows desktops have no Terminal.

On a desktop already provisioned with this release, refresh trust as root (for example through an SSM shell):

```bash
sudo rm -f /run/idea-terminal-trust-ready
sudo systemctl reset-failed idea-terminal-trust.service
sudo systemctl --no-block restart idea-terminal-trust.service
sudo timeout 360 sh -c 'until test -f /run/idea-terminal-trust-ready || systemctl is-failed -q idea-terminal-trust.service; do sleep 5; done; test -f /run/idea-terminal-trust-ready' || { sudo journalctl -u idea-terminal-trust.service -n 50 --no-pager; false; }
sudo sshd -t && test -f /run/idea-terminal-trust-ready
```

Removing the previous ready marker and failed state first means only this refresh can satisfy the wait; IDEA certificates are already denied while the marker is absent. The restart returns at once. The wait ends when the marker appears, when the unit fails with status 78, or after six minutes; on failure it prints the service journal and exits non-zero. A refresh that keeps failing retries every 30 seconds and does not hang the command. Treat a missing ready marker as a failed refresh, even though the unit shows `active` while it retries. The helper reloads sshd itself; the last line checks the sshd configuration and the marker again, so the block exits non-zero on a failed refresh even when run as a script without `set -e` (for example through SSM Run Command). Do not insert an unverified host key into the terminal table.

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
5. Verify the new public key on every running host, `/run/idea-terminal-trust-ready` on desktops, and removal of all older desktops without the boot service. Then re-enable Terminal and verify a new connection. A stopped desktop with the boot service must pass refresh when it next starts.

## Renderer

The terminal panel pins `ghostty-web` 0.4.0, using libghostty VT compiled to WebAssembly and its canvas renderer. The panel is mounted beside the portal routes, so its tabs and connections survive navigation; each tab is one connection and counts toward the per-account limit. The built-in fit addon drives the existing resize/SIGWINCH messages; a hidden tab keeps its size. The **Shell** button appears when `Terminal.ListTargets` returns the bastion; a desktop's shell button (and its **Terminal** action) is enabled when the desktop is in that list. xterm.js remains on the log-tail page only.

The renderer module loads only when the first tab opens. Vite emits the packaged WASM as a hashed `static/media/ghostty-vt.*.wasm` asset served by the portal. No CDN or external asset host is used. A tab connects once initialization completes; a loading failure asks the user to reload.

This repository does not set a portal Content-Security-Policy, so no CSP change is needed here. If a deployment adds a CSP at a reverse proxy, permit the portal origin in `script-src` and `connect-src`, and add `'wasm-unsafe-eval'` to `script-src` for WASM compilation. Broad `'unsafe-eval'` permission is unnecessary. Keep any existing nonce/hash allowances for portal bootstrap scripts.

The adapter preserves single-click HTTP/HTTPS links with `noopener,noreferrer` and blocks other URI schemes. A `beforeinput` bridge handles phone keyboards while ghostty-web retains IME composition. Shift Escape uses ghostty-web's handled-key convention to move focus to an enabled control. Copy uses the custom key handler (Cmd+C on macOS, Ctrl+Shift+C elsewhere); unhandled Ctrl+C is encoded by ghostty-web as an interrupt, and Cmd+V, Ctrl+Shift+V and the context menu reach ghostty-web's own paste handling (bracketed paste when the shell enables it).

Touch support applies only to a coarse pointer (`(pointer: coarse)`) and never to a laptop. ghostty-web's hidden textarea is the input: a tap focuses it to raise the native keyboard. It holds a 64-space buffer with the caret in the middle, so native caret gestures become Left/Right keys (application-cursor aware) before the caret is recentred; jumps to either end, such as vertical drags, are ignored. A drag is replayed as ghostty-web wheel events with momentum, which scroll its scrollback on the normal screen and send Up/Down on the alternate screen (tmux), as a laptop trackpad does. Pinch changes the stored text size (10 to 28). A long press shows the visible screen as DOM text for native selection handles and the system Copy/Paste menu; Paste sends to the shell. The key bar appears until the first hardware-only key (a modifier, Escape, Tab, arrows, function keys) arrives.

Runtime theme changes use the public canvas render API and recolor default text/background in both the screen and scrollback without resetting the session. Version 0.4.0 exposes resolved RGB values rather than default-color flags, so explicitly colored `#cccccc` text and black backgrounds also follow the portal theme. Its link detector does not join URLs across wrapped lines or expose OSC 8 link destinations. Screen-reader terminal output is out of scope for this HPC terminal ([upstream issue 187](https://github.com/coder/ghostty-web/issues/187)); there is no alternate renderer or accessibility toggle.
