# ideactl command reference

This page describes what the `ideactl` binary does today. Option spellings in the tables match `ideactl <command> --help`.

Every command also accepts `-h, --help` (`display help for command`). That flag is omitted from the tables below. With no arguments, `ideactl` prints the root help and exits 0.

`--aws-profile` is optional wherever it appears. When it is set, that action uses the named profile. `--cluster-name` and `--aws-region` are required by the parser wherever the table says yes, even when `--help` does not print the word required.

Local cluster files live under `$IDEA_USER_HOME/clusters/<cluster>/<region>/` (`IDEA_USER_HOME` defaults to `~/.idea`): `values.yml`, `config/`, `_cdk/`, `deployments/`, `logs/`, `support/`.

## Exit codes

These apply to every command unless a command section names a more specific code.

| Code | When |
| --- | --- |
| 0 | Success, `--help`, or the operator declined a confirmation prompt. |
| 1 | Missing required flags or arguments, invalid values, cluster configuration errors, a refused change set, a missing AWS profile, configuration tables that do not exist, or `ExitWithCode(1)`. |
| other | A spawned `cdk` process returned that code. |
| 1 plus a traceback | Any other thrown error, including a failed integration-test run and an aborted cluster deletion. The process prints `Command failed with error: ...` and then the stack. |

A configuration table that has not been created prints one red line ending in `Is the cluster configuration synced?` and exits 1.

## `ideactl`

IDEA cluster administration.

**Usage:** `ideactl [options] [command]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `-V, --version` | no | none | no |

**Reads:** `IDEA_VERSION.txt` for `-V`. **Changes:** nothing. **Example:** `ideactl -h`

## `about`

Print the release version.

**Usage:** `ideactl about [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--no-banner` | no | none | no |

**Reads:** `IDEA_VERSION.txt` (or `IDEA_VERSION`). **Changes:** nothing. **Exit codes:** 0. **Example:** `ideactl about`

`--no-banner` is accepted and ignored. The command always prints `ideactl <version>` and never prints a banner.

## `quick-setup-help`

Print the packaged `values.yml` template.

**Usage:** `ideactl quick-setup-help [options]`

No command-specific options.

**Reads:** the packaged `resources/config/values.yml`. **Changes:** nothing. **Example:** `ideactl quick-setup-help`

## `quick-setup`

Install a new cluster: generate and update configuration, bootstrap CDK, deploy modules in priority order, then wait for health checks.

**Usage:** `ideactl quick-setup [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--values-file <values-file>` | yes | none | no |
| `--existing-resources` | no | none | no |
| `--termination-protection <termination-protection>` | yes | `"true"` | no |
| `--deployment-id <deployment-id>` | yes | none | no |
| `--optimize-deployment` | no | none | no |
| `--force` | no | none | no |
| `--skip-config` | no | none | no |
| `--rollback` | no | `true` | no |
| `--no-rollback` | no | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |
| `--allow-replacement <logical-id>` | yes, repeatable | none | no |

**Reads:** `--values-file` or an interactive installer; DynamoDB cluster tables after update. **Changes:** local `values.yml` and `config/`, DynamoDB modules and settings tables, the `<cluster>-bootstrap` stack, module CloudFormation stacks, bootstrap packages in the cluster bucket. **Exit codes:** 1 if `--skip-config` is set without `--values-file`; 0 if the operator aborts the deploy prompt.

**Example:** `ideactl quick-setup --values-file ./values.yml --force --module-set default`

`--termination-protection` is a string. Values `true`, `yes`, `y`, `1`, and `on` (any case) count as true. `--skip-config` skips generate and update and still prints the settings table, modules table, bootstrap, and deploy.

## `config`

Configuration options. A group; it prints its subcommand list.

**Usage:** `ideactl config [options] [command]`

No command-specific options.

**Example:** `ideactl config -h`

## `config generate`

Render `values.yml` and the local `config/` tree from templates.

**Usage:** `ideactl config generate [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--values-file <values-file>` | yes | none | no |
| `--config-dir <config-dir>` | yes | none | no |
| `--force` | no | none | no |
| `--existing-resources` | no | none | no |
| `--regenerate` | no | none | no |

**Reads:** `--values-file` if given, otherwise an interactive installer (STS for the account id). **Changes:** writes `values.yml` and `config/` under `--config-dir` or `~/.idea/clusters/<cluster>/<region>/`. **Exit codes:** 1 if `--config-dir` does not exist, or cluster name or region is missing; 0 if the overwrite prompt is declined.

**Example:** `ideactl config generate --values-file ./values.yml --force`

With `--values-file`, `--existing-resources` and `--regenerate` are not used. Without `--force`, a non-empty target directory is prompted and then cleared. With `--force`, that cleanup is skipped.

## `config update`

Push the local `config/` tree into the cluster settings and modules tables.

**Usage:** `ideactl config update [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--force` | no | none | no |
| `--overwrite` | no | none | no |
| `--key-prefix <key-prefix>` | yes | none | no |
| `--config-dir <config-dir>` | yes | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |

**Reads:** `<config-dir>/config` or `~/.idea/clusters/<cluster>/<region>/config`. **Changes:** creates the DynamoDB tables if needed, then syncs modules and settings. Existing keys are skipped unless `--overwrite` is set. **Exit codes:** 1 if the config directory is missing or the local cluster name or region does not match; 0 if the operator chooses Exit.

**Example:** `ideactl config update --cluster-name sample-cluster --aws-region us-east-2 --force`

Without `--force`, the prompt is `Yes` / `Reload Changes` / `Exit`.

## `config set`

Write one or more typed settings keys.

**Usage:** `ideactl config set [options] <entries...>`

| Argument | Required | Help text |
| --- | --- | --- |
| `entries` | yes, variadic | `Key=KEY_NAME,Type=[str|int|float|bool|list<str>|list<int>|list<float>|list<bool>],Value=VALUE` |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--force` | no | none | no |

**Reads:** nothing local. **Changes:** DynamoDB settings rows via `setConfigEntry`. **Exit codes:** 1 on a malformed entry; 0 if the confirm prompt is declined.

**Example:** `ideactl config set --cluster-name sample-cluster --aws-region us-east-2 --force 'Key=cluster.locale,Type=str,Value=en_US'`

`list<bool>` is accepted and stored as a list of strings. Keys may not contain `,` or `:`.

## `config show`

Print cluster settings. The help summary says yaml. The default output is a table.

**Usage:** `ideactl config show [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `-q, --query <query>` | yes | none | no |
| `--format <format>` | yes | none (omitted means table; choices `table`, `yaml`, `raw`) | no |

**Reads:** DynamoDB `<cluster>.cluster-settings`. `--aws-region` is required by the parser and is not used to name the table. **Changes:** nothing.

**Example:** `ideactl config show --cluster-name sample-cluster --aws-region us-east-2 --format yaml`

`--query` is a regular expression matched from the start of the key, not a search anywhere in the key.

## `config export`

Write the DynamoDB settings back to a `config/` tree. Refuses a non-empty target directory.

**Usage:** `ideactl config export [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--export-dir <export-dir>` | yes | cluster `config/` directory | no |

**Reads:** `<cluster>.cluster-settings` and `<cluster>.modules`. **Changes:** writes `idea.yml` and `<module-id>/settings.yml` under the export directory.

**Example:** `ideactl config export --cluster-name sample-cluster --aws-region us-east-2 --export-dir /tmp/sample-cluster-config`

## `config delete`

Delete every settings key under each given prefix. There is no confirmation prompt.

**Usage:** `ideactl config delete [options] <config-key-prefixes...>`

| Argument | Required | Help text |
| --- | --- | --- |
| `config-key-prefixes` | yes, variadic | config key prefixes |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |

**Reads:** nothing local. **Changes:** DynamoDB settings rows matching each prefix. **Example:** `ideactl config delete --cluster-name sample-cluster --aws-region us-east-2 global-settings.custom_tags`

## `config diff`

Compare local `config/` files to the settings table as MODIFIED, DELETED, or ADDED.

**Usage:** `ideactl config diff [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--config-dir <config-dir>` | yes | cluster `config/` directory | no |

**Reads:** local `config/` and DynamoDB `<cluster>.cluster-settings`. **Changes:** nothing. **Example:** `ideactl config diff --cluster-name sample-cluster --aws-region us-east-2`

## `config preview-upgrade`

Print the configuration drift an upgrade would apply, without writing it.

**Usage:** `ideactl config preview-upgrade [options] [modules...]`

| Argument | Required | Help text |
| --- | --- | --- |
| `modules` | no, variadic | module ids |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--base-os <base-os>` | yes | none | no |
| `--values-file <values-file>` | yes | cluster `values.yml` | no |
| `--skip-global-settings-update` | no | none | no |

**Reads:** cluster tables, local or supplied `values.yml`, AMI maps. **Changes:** nothing. **Example:** `ideactl config preview-upgrade --cluster-name sample-cluster --aws-region us-east-2 --base-os amazonlinux2023`

## `config save-values`

Upload `values.yml` to the cluster bucket at `values/values.yml`.

**Usage:** `ideactl config save-values [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--values-file <values-file>` | yes | cluster `values.yml` | no |

**Reads:** the values file; DynamoDB `cluster.cluster_s3_bucket` or STS plus the conventional bucket name. **Changes:** S3 object `values/values.yml`. **Example:** `ideactl config save-values --cluster-name sample-cluster --aws-region us-east-2`

## `config download-values`

Download `values/values.yml` from the cluster bucket.

**Usage:** `ideactl config download-values [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--values-dir <values-dir>` | yes | none | no |

**Reads:** S3 `values/values.yml`. **Changes:** writes `values.yml` to `--values-dir` or the cluster directory. Reloads the body through YAML so the file is a dump of the parsed document, not the raw object bytes. **Exit codes:** 1 if the object is missing.

**Example:** `ideactl config download-values --cluster-name sample-cluster --aws-region us-east-2 --values-dir /tmp/sample-cluster`

## `cdk`

CDK options. A group.

**Usage:** `ideactl cdk [options] [command]`

No command-specific options. **Example:** `ideactl cdk -h`

## `cdk synth`

Synthesize the CloudFormation template for one module.

**Usage:** `ideactl cdk synth [options] <module>`

| Argument | Required | Help text |
| --- | --- | --- |
| `module` | yes | module id |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--deployment-id <deployment-id>` | yes | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |

**Reads:** DynamoDB cluster config. **Changes:** writes under the cluster `_cdk/` directory and runs `cdk synth`. **Example:** `ideactl cdk synth --cluster-name sample-cluster --aws-region us-east-2 metrics`

## `cdk diff`

Compare one module template to the deployed stack.

**Usage:** `ideactl cdk diff [options] <module>`

| Argument | Required | Help text |
| --- | --- | --- |
| `module` | yes | module id |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--deployment-id <deployment-id>` | yes | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |

**Reads:** DynamoDB cluster config and the live stack. **Changes:** none besides local CDK output. **Example:** `ideactl cdk diff --cluster-name sample-cluster --aws-region us-east-2 metrics`

## `cdk cdk-app`

Build exactly one stack and synthesize it. This is the `--app` re-entry the CDK CLI runs, not an operator command.

**Usage:** `ideactl cdk cdk-app [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--module-name <module-name>` | yes | none | yes |
| `--module-id <module-id>` | yes | none | yes |
| `--deployment-id <deployment-id>` | yes | none | no |
| `--termination-protection <termination-protection>` | yes | `"true"` | no |
| `--config-file <config-file>` | yes | none | no |
| `--synth-reads <synth-reads>` | yes | none | no |

**Reads:** DynamoDB, or `--config-file` and `--synth-reads` replay files. **Changes:** writes a CDK assembly under the process working directory. **Example:** `ideactl cdk cdk-app --cluster-name sample-cluster --aws-region us-east-2 --module-name metrics --module-id metrics`

## `bootstrap`

Render the CDK toolkit template and run `cdk bootstrap` for the cluster.

**Usage:** `ideactl bootstrap [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--termination-protection <termination-protection>` | yes | `"true"` | no |
| `--custom-permissions-boundary <name>` | yes | `""` | no |
| `--cloudformation-execution-policies <policies>` | yes | `""` | no |
| `--public-access-block-configuration <public-access-block-configuration>` | yes | `"true"` | no |
| `--module-set <module-set>` | yes | `"default"` | no |

**Reads:** DynamoDB `cluster.cluster_s3_bucket` and custom tags. **Changes:** writes `_cdk/cdk_toolkit_stack.yml`, then creates or updates `<cluster>-bootstrap` and uses the cluster bucket as the CDK staging bucket. Empty permissions-boundary and execution-policy strings are omitted from the CDK argv. **Exit codes:** the `cdk` process code.

**Example:** `ideactl bootstrap --cluster-name sample-cluster --aws-region us-east-2`

## `deploy`

Deploy module stacks. `all` may be the only module id and means every undeployed module (or every module with `--upgrade`).

**Usage:** `ideactl deploy [options] <modules...>`

| Argument | Required | Help text |
| --- | --- | --- |
| `modules` | yes, variadic | module ids, or `all` |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--termination-protection <termination-protection>` | yes | `"true"` | no |
| `--deployment-id <deployment-id>` | yes | none | no |
| `--upgrade` | no | none | no |
| `--force-build-bootstrap` | no | none | no |
| `--rollback` | no | `true` | no |
| `--no-rollback` | no | none | no |
| `--optimize-deployment` | no | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |
| `--allow-replacement <logical-id>` | yes, repeatable | none | no |

**Reads:** DynamoDB modules and settings. If the deployment includes `ecs`, also reads the account `awsvpcTrunking` setting. **Changes:** bootstrap packages in the cluster bucket, CloudFormation stacks via a change set that is inspected before execute, `deployments/<id>/<module>-outputs.json`. After the `cluster` module deploys, any `cluster.network.client_ip` address the cluster prefix list does not already hold is added to it; nothing is ever removed. Using `all` with any other module id exits 1 (`fatal error - use of "all" deployment must be the only requested module`). If `awsvpcTrunking` is not enabled, the command prints the exact `aws ecs put-account-setting-default` command and exits 1 without deploying.

If all requested modules are already deployed, the command keeps the already-deployed message and exits 1. Pass `--upgrade` to re-deploy them.

**Example:** `ideactl deploy --cluster-name sample-cluster --aws-region us-east-2 metrics`

A change set that would replace or remove a stateful resource is refused unless that logical id is passed to `--allow-replacement`.

## `replace`

Replace one stateful component, deliberately. An upgrade and a migration never replace one: the change-set guard refuses it, and the synthesized templates carry `UpdateReplacePolicy: Retain` on the resources an upgrade has never replaced, so an accidental replacement stops rather than proceeding. This is the path for the case where it is intended.

The jump host and the scheduler host are listed here but are outside that protection: an ordinary upgrade replaces both, so they carry no retain policy and this command is simply the deliberate way to do on purpose what an upgrade does on its own.

**Usage:** `ideactl replace [options] <component>`

| Argument | Required | Help text |
| --- | --- | --- |
| `component` | yes | one of: jump-host, search-domain, directory, user-pool, scheduler-host, shared-file-system, backup-vault |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |
| `--deployment-id <deployment-id>` | yes | none | no |
| `--confirm <component>` | yes | none | no |

The command always prints what is lost before anything else happens, then stops unless `--confirm` repeats the component name exactly. No other value proceeds, including `yes` or `true`. `shared-file-system` and `backup-vault` print their consequence and refuse: moving to a new file system is a copy while both exist, and a replacement backup vault is empty with no way to move the old recovery points into it.

It does not create a replacement by itself. CloudFormation replaces a resource when a property that cannot be changed in place changes, so the order is: change the setting, then run this. The printed warning names the properties that force it for that component. If the change set holds no replacement of that component, the deploy runs and nothing is replaced.

**Reads:** DynamoDB modules and settings. **Changes:** the one module stack that owns the component, through the same inspected change set as `deploy`, with the replacement permitted for that one resource type only. A removal is never permitted by this command.

**Example:** `ideactl replace search-domain --cluster-name sample-cluster --aws-region us-east-2 --confirm search-domain`

## `check-cluster-status`

GET each app module `/healthcheck` and the analytics dashboards URL. TLS is not verified.

**Usage:** `ideactl check-cluster-status [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--wait` | no | none | no |
| `--wait-timeout <seconds>` | yes | `900` | no |
| `--debug` | no | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |

**Reads:** DynamoDB cluster config, then HTTPS GET. **Changes:** nothing. **Exit codes:** 1 if any endpoint is not HTTP 200 after the last pass.

**Example:** `ideactl check-cluster-status --cluster-name sample-cluster --aws-region us-east-2 --wait --wait-timeout 120`

Without `--wait`, `--wait-timeout` does not change the single pass. With `--wait`, the loop sleeps 60 seconds between passes until every endpoint succeeds or the timeout is reached.

## `list-modules`

Print Title, Name, Module ID, Type, Stack Name, Version, and Status.

**Usage:** `ideactl list-modules [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |

**Reads:** DynamoDB `<cluster>.modules`. **Changes:** nothing. **Example:** `ideactl list-modules --cluster-name sample-cluster --aws-region us-east-2`

## `show-connection-info`

Print portal, bastion SSH, Session Manager, and analytics URLs for deployed modules.

**Usage:** `ideactl show-connection-info [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |

**Reads:** DynamoDB cluster config. **Changes:** nothing. **Example:** `ideactl show-connection-info --cluster-name sample-cluster --aws-region us-east-2`

If nothing is deployed, the command prints an error to stderr and still exits 0.

## `upgrade-cluster`

Upgrade an existing cluster: refuse a cluster with any deployed module below 25.11.0 or an unreadable version, refuse EOL base OS that is still referenced, preview drift, then run phases 1 to 4 (values base OS, global settings backup and rewrite, optional full config sync, AMI and instance-type keys, then module deploy). Empty `modules` means every module. Clusters below 26.09.0 require complete deployed-module coverage and a read-only plan of settings, values, templates, EOL tables, IAM policy capacity, image metadata and instance protection before mutation. Global replacement, full sync and AMI/settings updates cannot be skipped in this mode. The old scheduler periodic interval is copied to the reconciler interval only when absent; conflicts are reported and both keys remain. Settings and deployed module versions are read back before success. A values upload failure after deployment is a warning with a recovery command.

**Usage:** `ideactl upgrade-cluster [options] [modules...]`

| Argument | Required | Help text |
| --- | --- | --- |
| `modules` | no, variadic | module ids |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--termination-protection <termination-protection>` | yes | `"true"` | no |
| `--deployment-id <deployment-id>` | yes | none | no |
| `--base-os <base-os>` | yes | none | no |
| `--force-build-bootstrap` | no | none | no |
| `--rollback` | no | `true` | no |
| `--no-rollback` | no | none | no |
| `--optimize-deployment` | no | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |
| `--force` | no | none | no |
| `--accept-config-drift` | no | none | no |
| `--skip-global-settings-update` | no | none | no |
| `--disable-eol-stacks-in-use` | no | none | no |
| `--drain` | no | none | no |
| `--drain-timeout-minutes <minutes>` | yes | 240 minutes (effective) | no |
| `--skip-drain-check` | no | none | no |

**Reads:** cluster tables, `values.yml`, AMI maps, EC2 images and instance types, OpenSearch instance types, eVDI software-stack tables, and the host scheduler's PBS job inventory over Systems Manager when a scheduler cutover is pending. **Changes:** `values.yml`, a `config.golden.<timestamp>/` copy, DynamoDB settings, instance termination protection (cleared then restored), module stacks, and an upload of `values.yml` to the cluster bucket. When the run moves the scheduler from a host to a container, it closes submission before reading the host's inventory, including when that inventory is empty. A non-empty inventory without `--drain` restores the previous maintenance state and refuses deployment; `--drain` waits for it to empty. `--skip-drain-check` skips the inventory read but still closes submission for the whole run. **Exit codes:** 1 on the release floor refusal, EOL refusal, missing AMI, unsupported instance type, or configuration rows the run would overwrite whose value differs from generated configuration without `--accept-config-drift`; 0 if a confirmation is declined.

The cutover gate and Phase 0 DNS retention apply when the scheduler is in scope (explicitly or
through all modules), ECS will be enabled at synthesis (`enable_ecs: true` in `values.yml`, or an
ECS module row plus `ecs.enabled: true` in settings), and the scheduler stack still has an EC2
host. This includes scheduler-only runs after ECS capacity has already deployed. Both steps are
skipped once the scheduler host is gone.

Before closing submission, the upgrade saves its original enabled flag and message as JSON in
`cluster-manager.maintenance.upgrade_baseline`. Failed runs preserve that row and leave submission
closed. Retries keep the original baseline. Successful completion, including a retry after the
host is gone, restores both maintenance values and deletes the baseline after the values upload.
A refusal on a retry preserves an earlier failed run's baseline and closure.

ECS module-set rows remain held until cluster-manager's modules-table row records `deployed` at
the target release, even if the ECS stack has already deployed. Held rows are checked again after
deployment. A scoped run that excludes cluster-manager does not publish them.

**Example:** `ideactl upgrade-cluster --cluster-name sample-cluster --aws-region us-east-2 --base-os amazonlinux2023 --force`

`awsvpcTrunking` is checked whenever the run reaches the `ecs` module, which includes an all-module upgrade of a cluster that has it. `--disable-eol-stacks-in-use` disables in-use EOL eVDI stacks instead of refusing. The drift preview stops the run only where a row it overwrites differs from generated configuration, names those rows, and asks; `--force` skips the other confirmations but does not accept those rows, which is what `--accept-config-drift` is for.

## `migrate`

The supported path to the container control plane is `upgrade-cluster` with `enable_ecs: true` in `values.yml`. The `migrate` executor is incomplete. It refuses new and resumed runs before any mutation when any execution capability is missing, with one message naming all missing capabilities.

**Usage:** `ideactl migrate [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--state-bucket <state-bucket>` | yes | none | yes |
| `--target-base-os <target-base-os>` | yes | none | no |
| `--image-digest <image-digest>` | yes | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |
| `--selected-module <module-id>` | yes, repeatable | none | no |
| `--deployment-id <deployment-id>` | yes | none | no |
| `--resume <deployment-id>` | yes | none | no |
| `--accept-template-comparison <fingerprint>` | yes | none | no |
| `--accept-drift <fingerprint>` | yes | none | no |

**Changes:** none while capabilities are missing, including no operation record, maintenance setting, or admission change. **Exit codes:** 1 on refusal. Template or drift acceptance does not bypass this gate.

## `delete-cluster`

Delete a cluster. Bootstrap, databases, backups, and log groups stay unless their flags (or `--delete-all`) are set. The bootstrap bucket is retained unless `--delete-bootstrap` or `--delete-all` is set.

**Usage:** `ideactl delete-cluster [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--delete-bootstrap` | no | none | no |
| `--delete-databases` | no | none | no |
| `--delete-backups` | no | none | no |
| `--delete-cloudwatch-logs` | no | none | no |
| `--delete-all` | no | none | no |
| `--force` | no | none | no |

**Reads:** tagged EC2 instances and CloudFormation stacks, Cognito user pools, backup vault, DynamoDB table names, log groups. **Changes:** terminates instances, deletes stacks (module stacks, then the container capacity stack, then identity-provider, then the record sets services left in the private hosted zone, then the cluster stack), optionally recovery points, tables, log groups, the bootstrap stack, and the cluster bucket. **Exit codes:** unhandled abort errors exit 1 with a traceback. Declining the first prompt returns 0.

**Example:** `ideactl delete-cluster --cluster-name sample-cluster --aws-region us-east-2 --force`

## `delete-backups`

Delete completed or expired recovery points in `<cluster>-cluster-backup-vault`.

**Usage:** `ideactl delete-backups [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--force` | no | none | no |

**Reads:** the backup vault. **Changes:** recovery points. Declining the prompt returns without deleting. **Example:** `ideactl delete-backups --cluster-name sample-cluster --aws-region us-east-2 --force`

## `sso`

Single sign-on configuration. A group.

**Usage:** `ideactl sso [options] [command]`

No command-specific options. **Example:** `ideactl sso -h`

## `sso show-idp-info`

Print the Cognito redirect URL, and for SAML the entity id.

**Usage:** `ideactl sso show-idp-info [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--provider-type <provider-type>` | yes | none | yes |

**Reads:** DynamoDB identity-provider keys. **Changes:** nothing. **Example:** `ideactl sso show-idp-info --cluster-name sample-cluster --aws-region us-east-2 --provider-type OIDC`

`--provider-type` must be `SAML` or `OIDC` (case is folded for the check, then compared as given for the URL path). Help has no description text for this command or its flags.

## `sso configure`

Create or update the Cognito identity provider and app client, store the client secret, link existing users, then set `cognito.sso_enabled` to true.

**Usage:** `ideactl sso configure [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--provider-name <provider-name>` | yes | none | yes |
| `--provider-type <provider-type>` | yes | none | yes |
| `--provider-email-attribute <provider-email-attribute>` | yes | none | yes |
| `--refresh-token-validity-hours <hours>` | yes | none (code uses 12 when omitted or `<= 0`) | no |
| `--oidc-client-id <id>` | yes | none | no |
| `--oidc-client-secret <secret>` | yes | none | no |
| `--oidc-issuer <issuer>` | yes | none | no |
| `--oidc-attributes-request-method <method>` | yes | none (code uses `GET`) | no |
| `--oidc-authorize-scopes <scopes>` | yes | none (code uses `openid`) | no |
| `--oidc-authorize-url <url>` | yes | none | no |
| `--oidc-token-url <url>` | yes | none | no |
| `--oidc-attributes-url <url>` | yes | none | no |
| `--oidc-jwks-uri <uri>` | yes | none | no |
| `--saml-metadata-url <url>` | yes | none | no |
| `--saml-metadata-file <file>` | yes | none | no |

**Reads:** cluster config, optional SAML metadata file. **Changes:** Cognito IdP and user pool client, Secrets Manager secret `<cluster>-sso-client-secret`, identity-provider settings keys. OIDC requires client id, secret, and issuer. SAML requires metadata URL or file. Invalid SSO input is printed to stdout and the command still exits 0.

**Example:** `ideactl sso configure --cluster-name sample-cluster --aws-region us-east-2 --provider-name ExampleIdp --provider-type OIDC --provider-email-attribute email --oidc-client-id example-client --oidc-client-secret example-secret --oidc-issuer https://idp.example.invalid`

## `directoryservice`

Directory service commands. A group.

**Usage:** `ideactl directoryservice [options] [command]`

No command-specific options. **Example:** `ideactl directoryservice -h`

## `directoryservice create-service-account-secrets`

Create username and password secrets. This command does not read cluster configuration.

**Usage:** `ideactl directoryservice create-service-account-secrets [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--username <username>` | yes | none | no |
| `--password <password>` | yes | none | no |
| `--kms-key-id <kms-key-id>` | yes | none | no |
| `--purpose <purpose>` | yes | none | no |

**Reads:** nothing from the cluster tables. Prompts for username and password when either is missing. **Changes:** Secrets Manager secrets named `<cluster>-directoryservice-<purpose>-username` and `<cluster>-directoryservice-<purpose>-password`. If credentials are supplied and `--purpose` is omitted, the name uses the literal `None`. **Example:** `ideactl directoryservice create-service-account-secrets --cluster-name sample-cluster --aws-region us-east-2 --username svc --password "example-pass" --purpose service-account`

## `shared-storage`

Shared storage commands. A group.

**Usage:** `ideactl shared-storage [options] [command]`

No command-specific options. **Example:** `ideactl shared-storage -h`

## `shared-storage add-file-system`

Interactive questionnaire to create a new file system in cluster settings, then optionally deploy the shared-storage module.

**Usage:** `ideactl shared-storage add-file-system [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--kms-key-id <kms-key-id>` | yes | none | no |

**Reads:** prompts; optional EFS/FSx describe calls. The live adapter still requires a cluster name even though the parser does not. **Changes:** shared-storage settings keys, and a module deploy if the operator picks that next step. **Example:** `ideactl shared-storage add-file-system --cluster-name sample-cluster --aws-region us-east-2`

## `shared-storage attach-file-system`

Same questionnaire as add-file-system, for an existing file system (`use_existing_fs: true`).

**Usage:** `ideactl shared-storage attach-file-system [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | no |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--kms-key-id <kms-key-id>` | yes | none | no |

**Reads:** prompts plus EFS/FSx describe. **Changes:** shared-storage settings. Does not offer the deploy next step. **Example:** `ideactl shared-storage attach-file-system --cluster-name sample-cluster --aws-region us-east-2`

## `utils`

Utility commands. A group.

**Usage:** `ideactl utils [options] [command]`

No command-specific options. **Example:** `ideactl utils -h`

## `utils aws-services`

Print the static required/optional AWS service matrix. No AWS calls.

**Usage:** `ideactl utils aws-services [options]`

No command-specific options. **Example:** `ideactl utils aws-services`

## `utils check-aws-services`

Print which of those services exist in each requested region (SSM global-infrastructure parameters).

**Usage:** `ideactl utils check-aws-services [options] <aws-regions...>`

| Argument | Required | Help text |
| --- | --- | --- |
| `aws-regions` | yes, variadic | (none) |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--aws-profile <aws-profile>` | yes | none | no |

**Reads:** SSM `/aws/service/global-infrastructure/regions/<region>/services`. The first region is also used to build the AWS client. **Changes:** nothing. **Example:** `ideactl utils check-aws-services us-east-2 us-west-2`

## `utils vpc-endpoints`

VPC endpoint commands. A group.

**Usage:** `ideactl utils vpc-endpoints [options] [command]`

No command-specific options. **Example:** `ideactl utils vpc-endpoints -h`

## `utils vpc-endpoints service-info`

Print whether each IDEA gateway and interface endpoint is available in the region.

**Usage:** `ideactl utils vpc-endpoints service-info [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |

**Reads:** EC2 `DescribeVpcEndpointServices`. **Changes:** nothing. **Example:** `ideactl utils vpc-endpoints service-info --aws-region us-east-2`

## `utils cluster-prefix-list`

Cluster prefix list commands. A group.

**Usage:** `ideactl utils cluster-prefix-list [options] [command]`

No command-specific options. **Example:** `ideactl utils cluster-prefix-list -h`

## `utils cluster-prefix-list show`

Print CIDR entries from the cluster managed prefix list.

**Usage:** `ideactl utils cluster-prefix-list show [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |

**Reads:** DynamoDB `cluster.network.cluster_prefix_list_id`, then EC2 prefix-list entries. **Changes:** nothing. **Example:** `ideactl utils cluster-prefix-list show --cluster-name sample-cluster --aws-region us-east-2`

## `utils cluster-prefix-list add-entry`

Add a CIDR to the cluster prefix list.

**Usage:** `ideactl utils cluster-prefix-list add-entry [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--cidr <cidr>` | yes | none | yes |
| `--description <description>` | yes | none | yes |

**Reads:** the prefix list id and current version. **Changes:** EC2 managed prefix list. Refuses a CIDR that is already present. **Example:** `ideactl utils cluster-prefix-list add-entry --cluster-name sample-cluster --aws-region us-east-2 --cidr 192.0.2.0/24 --description office`

## `utils cluster-prefix-list remove-entry`

Remove a CIDR from the cluster prefix list.

**Usage:** `ideactl utils cluster-prefix-list remove-entry [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--cidr <cidr>` | yes | none | yes |

**Reads:** the prefix list id and current version. **Changes:** EC2 managed prefix list. **Example:** `ideactl utils cluster-prefix-list remove-entry --cluster-name sample-cluster --aws-region us-east-2 --cidr 192.0.2.0/24`

## `backup-update-global-settings`

Copy local `config/` to `config.golden.<MMDDYYYY_HHMMSS>/`, regenerate from `values.yml`, and replace only `global-settings` keys in DynamoDB.

**Usage:** `ideactl backup-update-global-settings [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--force` | no | none | no |
| `--module-set <module-set>` | yes | none (help text says default; the parser has no default, ClusterConfig still uses `default`) | no |

**Reads:** cluster config export, `values.yml`. **Changes:** local `config/`, golden backup directory, DynamoDB `global-settings.*`. Without `--force`, a confirmation prompt can abort with a message and exit 0. **Example:** `ideactl backup-update-global-settings --cluster-name sample-cluster --aws-region us-east-2 --force`

Help has no command description.

## `support`

Support options. A group.

**Usage:** `ideactl support [options] [command]`

No command-specific options. **Example:** `ideactl support -h`

## `support deployment`

Build a deployment debug archive.

**Usage:** `ideactl support deployment [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--module-set <module-set>` | yes | `"default"` | no |

**Reads:** local `logs/`, `values.yml`, `config/`, and a DynamoDB dump when those package contents are selected. The default contents (when nothing is chosen) are deployment logs, `values.yml`, the database dump, and local config. CDK config and the deployments directory are included only if chosen. **Changes:** writes `support/idea-deployment-debug-pkg-<timestamp>/` and a `.tar.gz` next to it. **Example:** `ideactl support deployment --cluster-name sample-cluster --aws-region us-east-2`

## `run-integration-tests`

Run shipped module integration-test cases.

**Usage:** `ideactl run-integration-tests [options] <modules...>`

| Argument | Required | Help text |
| --- | --- | --- |
| `modules` | yes, variadic | (none) |

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--cluster-name <cluster-name>` | yes | none | yes |
| `--aws-region <aws-region>` | yes | none | yes |
| `--aws-profile <aws-profile>` | yes | none | no |
| `--admin-username <username>` | yes | none | yes |
| `--admin-password <password>` | yes | none | yes |
| `--test-case-id <test-case-id>` | yes | none | no |
| `--debug` | no | none | no |
| `-p, --param <key=value>` | yes, repeatable | none | no |
| `--module-set <module-set>` | yes | none (help text says default; the parser has no default, ClusterConfig still uses `default`) | no |

**Reads:** cluster modules table; each test case may call cluster APIs. **Changes:** whatever the selected tests change. Duplicate module ids are dropped. `--test-case-id` is a comma-separated list. `-p` keeps the last value for a duplicate key and ignores tokens without `=`. A module that is not deployed, or a failed case, ends the run. Failed cases throw after printing `[FAIL]`; that error is not mapped to a quiet exit, so the process prints a traceback.

**Example:** `ideactl run-integration-tests --cluster-name sample-cluster --aws-region us-east-2 --admin-username clusteradmin --admin-password "example-pass" cluster-manager`

Help has no command description.

## `help`

Commander built-in. Prints help for the program or for a named command.

**Usage:** `ideactl help [command]`

**Example:** `ideactl help deploy`

## `cost-collector`

Deploy or remove account spend collection without a cluster. A group.

**Usage:** `ideactl cost-collector [options] [command]`

No command-specific options. **Example:** `ideactl cost-collector -h`

## `cost-collector deploy`

Deploy standalone account spend collection in a commercial billing account, without cluster settings tables.

**Usage:** `ideactl cost-collector deploy [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--aws-region <region>` | yes | none | yes |
| `--aws-profile <profile>` | yes | none | no |
| `--stack-name <name>` | yes | none | yes |
| `--cluster-name <name>` | yes | none | yes |
| `--control-plane-image <image>` | yes | none | yes |
| `--agent-image <image>` | yes | none | yes |
| `--datadog-api-key-secret-arn <arn>` | yes | none | yes |
| `--subnet-ids <ids...>` | yes | none | yes |
| `--interval-hours <hours>` | yes | `6` | no |
| `--lookback-days <days>` | yes | `3` | no |
| `--module-tag <key>` | yes | `"idea:ModuleId"` | no |
| `--project-tag <key>` | yes | `"idea:Project"` | no |
| `--owner-tag <key>` | yes | `"idea:JobOwner"` | no |
| `--by-account` | no | `false` | no |
| `--allow-replacement <logical-id>` | yes, repeatable | `[]` | no |

**Reads:** caller identity, subnet route tables and CloudFormation change sets. **Changes:** one stack containing a log group, ECS cluster, Fargate service, two-container task, security group and IAM roles. Deploy prepares a change set and applies the existing guard before execution; task definition revisions are allowed. It waits for stack completion.

The agent image must be a digest-pinned private ECR reference. Both images must support Linux x86_64. The secret must contain the raw Datadog API key, reside in the deployment region, and permit the execution role to decrypt it if a custom KMS key policy restricts access. Subnets must share a VPC and be all public or all private. Public IPs are assigned when the effective route tables have an internet gateway route; private subnets need NAT access. The service stops its old task before starting a replacement to prevent duplicate collection. The cluster name is only the `idea_cluster` metric tag, not a spend filter. Historical ingestion for `idea.cost` must be enabled in Datadog. The standard agent uses US1 (`datadoghq.com`); the deployment does not configure `DD_SITE`. Follow the [image preparation and cost verification runbook](../../../../docs/first-time-users/cluster-operations/update-idea-cluster/move-to-containers.md#spend-and-storage), using the AMD64 digest from the `datadog/agent` repository.

**Example:**

```bash
ideactl cost-collector deploy --aws-profile <BILLING_PROFILE> --aws-region us-east-1 --stack-name gov-spend \
  --cluster-name gov-cluster --control-plane-image <CONTROL_PLANE_IMAGE> \
  --agent-image <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/datadog/agent@sha256:<AMD64_DIGEST> \
  --datadog-api-key-secret-arn <SECRET_ARN> --subnet-ids <SUBNET_ID>
```

## `cost-collector destroy`

Remove only the named collector stack. No image, secret or subnet flags are needed.

**Usage:** `ideactl cost-collector destroy [options]`

| Flag | Value | Default | Required |
| --- | --- | --- | --- |
| `--aws-region <region>` | yes | none | yes |
| `--aws-profile <profile>` | yes | none | no |
| `--stack-name <name>` | yes | none | yes |
| `--force` | no | none | no |

**Reads:** caller identity and the named stack. **Changes:** deletes the collector stack and its logs after confirmation; `--force` skips the prompt. The supplied secret and image repositories remain. **Example:** `ideactl cost-collector destroy --aws-region us-east-1 --stack-name gov-spend`

Existing ECS hosts created before the storage mount correction must follow [manual host replacement](ECS-HOST-REPLACEMENT.md) before application deployment. Collector retry and retention behavior is described in [collector delivery](COLLECTOR-DELIVERY.md).

The scheduler cutover gate resolves maintenance through the selected module set. The settings API requires application authentication unavailable to the upgrade identity, so the gate uses two empty PBS inventory reads at least 30 seconds apart after writing maintenance. A nonempty read resets that sequence; an unreadable inventory fails the upgrade. This propagation fallback is not an acknowledged scheduler barrier. ECS enters the module set only after the cluster-manager stack is `CREATE_COMPLETE` or `UPDATE_COMPLETE` with the target release tag and module version.
