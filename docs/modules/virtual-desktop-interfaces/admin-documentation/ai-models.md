---
description: What a desktop with Amazon Bedrock model access gets, and how to seed an existing one
---

# AI models on virtual desktops

A desktop launched into a project that has Amazon Bedrock enabled is bootstrapped with the model configuration and the Claude Code defaults already in place.

## What the bootstrap writes

A desktop is given these only when its project has Bedrock enabled and at least one application inference profile provisioned. A desktop in any other project gets none of them.

| Path | Contents |
| --- | --- |
| `/etc/environment` | `CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_MODEL` and `ANTHROPIC_SMALL_FAST_MODEL`, set to the project's application inference profile ARNs. Applies to every user on the desktop. |
| `/etc/idea/claude-code-defaults.json` | The settings offered to each user: `outputStyle`, `permissions.defaultMode` and `autoMode.environment`. |
| `/etc/idea/seed-claude-code-settings.py` | The seeder that merges those settings into a user's `~/.claude/settings.json`. |
| `/etc/profile.d/idea-claude-code.sh` | Runs the seeder on every login shell, for accounts at uid 1000 and above. |

The seeder adds a setting only if it has never offered that user the setting before, and records what it has offered in `~/.claude/.idea-seeded`. A value the user changed is left alone, and a setting the user deleted stays deleted. See [choose-an-ai-model.md](../user-documentation/choose-an-ai-model.md "mention") for what users see.

## Choose the permission mode

`virtual-desktop-controller.bedrock.claude_code.permission_mode` sets the permission mode a desktop is seeded with. Valid values are `auto`, `default`, `acceptEdits` and `plan`; the default is `auto`. The value `none` seeds no `permissions` block at all, which leaves the client on whatever mode it ships with.

```bash
./idea-admin.sh config \
  set "Key=vdc.bedrock.claude_code.permission_mode,Type=string,Value=default" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

The setting is read when a desktop is bootstrapped, so it applies to desktops created after the change.

## Seed an existing desktop

A desktop bootstrapped before its project had model access has neither the defaults file nor the seeder, and neither a reboot nor a new session adds them. Recreate that desktop.

On a desktop that does have `/etc/idea/claude-code-defaults.json`, run the seeder rather than copying the file by hand. Copying it would overwrite settings the user has already set, and would leave `~/.claude/.idea-seeded` out of step so a setting the user deleted comes back.

```bash
# one user
sudo -u <USERNAME> -H python3 /etc/idea/seed-claude-code-settings.py

# every account with a home directory on this desktop
for home in /data/home/*; do
  sudo -u "$(basename "$home")" -H python3 /etc/idea/seed-claude-code-settings.py
done
```

The seeder is safe to run repeatedly: it adds nothing it has offered before, and writes the settings file only when it has something to add.

{% hint style="info" %}
The model and provider come from `/etc/environment`, not from the seeded settings. If a desktop's `ANTHROPIC_MODEL` is missing or stale, recreate the desktop rather than editing the file: the ARNs are per project and per model, and a stale one fails as a permission error rather than as a missing model.
{% endhint %}
