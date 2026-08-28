---
description: Read the Amazon Bedrock invocation logging that AI usage reporting is built on
---

# AI Usage Tracking

The **AI Usage** column on the **Projects** page reports month to date tokens and requests per project, broken down per model, alongside the project's month to date Bedrock spend. Tokens are aggregated from Amazon Bedrock **model invocation logging**, not from billing data, so they are visible within the hour rather than a day later. If invocation logging is not delivering to the cluster log group, nothing is collected and every project reads as unused.

The spend figure comes from Cost Explorer, filtered to the project's `idea:Project` cost allocation tag and summed over the Bedrock services, so it trails the recorded tokens by about a day. It is available in the commercial partition only; where Cost Explorer cannot answer the column reads "cost unavailable" rather than zero. Per user attribution is not in the column: read it through the usage API and the downstream tooling built on it.

## Model invocation logging

Model invocation logging is a single configuration per AWS account and region, and it captures every Bedrock caller in that account, not only IDEA hosts. IDEA manages it by default: `invocation_logging.manage_configuration` ships as `true`.

Deploying the cluster-manager module with `bedrock.enabled` true creates the destination log group and its delivery role, and records them as `cluster-manager.bedrock.invocation_log_group_name` and `cluster-manager.bedrock.invocation_log_role_arn`. IDEA then points the account and region configuration at that log group, but only when nothing else has. It never overwrites another owner's configuration, and never deletes one.

If another owner already configured invocation logging for this account and region, IDEA leaves it alone and usage stays empty until that configuration delivers to the IDEA log group. The cluster-manager log records this as a stand down, naming the destination it found.

## Opt out of managing the account configuration

Setting `manage_configuration` to false leaves the account and region configuration untouched. Nothing is collected for any project unless invocation logging is configured outside IDEA to deliver to the cluster log group:

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.bedrock.invocation_logging.manage_configuration,Type=bool,Value=false" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

The setting is read on the next aggregation cycle, which runs hourly by default.

To configure logging yourself, in the Bedrock console under **Settings** > **Model invocation logging**, enable logging to CloudWatch Logs with the log group named in `cluster-manager.bedrock.invocation_log_group_name` and the role in `cluster-manager.bedrock.invocation_log_role_arn`. Usage is attributed only for records delivered to that log group.

## What the Projects page shows

| Cell | Meaning |
| --- | --- |
| `--` | Bedrock is not enabled for the project. |
| Tokens and requests | Usage recorded for the current month. |
| `No usage recorded` | Logging is being managed and no invocation was recorded for the project this month. |
| `Not collected` | `manage_configuration` is false, so IDEA is not setting the account configuration. Unless logging was configured outside IDEA, no usage is collected for any project. |
| `Usage unavailable` | The usage read failed. Check the cluster-manager logs. |

## Enforce a project budget on model spend

Off by default. Model charges are already in the project's AWS budget: the application inference profiles carry the project cost allocation tag, so Bedrock spend reaches the budget's own actual spend figure, about a day after the invocation. Turning this on publishes the verdict on that budget and acts on it. Nothing is valued ahead of cost allocation, and recorded tokens are reporting data that feeds no limit.

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.bedrock.budgets.enabled,Type=bool,Value=true" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

What an exhausted budget does, with `action` at its default of `block`:

| Where | Effect |
| --- | --- |
| New virtual desktop | The desktop launches, without model access. It runs under the shared DCV host profile instead of the project role. |
| Desktop already running | Nothing. Model access is not withdrawn from a running session. |
| New HPC job | Not provisioned. What holds the job is the project's AWS Budgets check, which compares the budget's actual spend to its limit, the same as any other overspent project. The Bedrock verdict decides nothing here: it only adds the Bedrock share of spend to the rejection reason. |

Set `action` to `warn` to publish the verdict without withholding anything, which is the way to see what enforcement would do before it does it.

A project with no budget has nothing to exceed and is unaffected. A budget that cannot be read withholds model access rather than allowing it, and never blocks a job, so one failed read cannot stop the cluster's work.

A warning or an exhausted verdict also reports how much of the project's spend was Bedrock, read from Cost Explorer grouped by service and filtered to the project's `idea:Project` cost allocation tag. It appears in the reason a held job is given, and in the controller log when a desktop launches without model access. The tag must be activated in **Billing** > **Cost allocation tags** for anything to be attributed to the project. The figure is reporting only: where Cost Explorer cannot answer, including any partition other than the commercial one, which has no endpoint for it, the share is absent and nothing else changes. Each request is billed at the Cost Explorer rate, so it is read only for a project at or over its warning threshold and the answer is held for six hours.

Enforcement is only as current as the budget it reads. The AWS Budgets figure is cached for `cluster-manager.cache.short_term.ttl_seconds`, ten minutes by default, and other modules hold a project for a further 30 seconds. Raising a limit or turning enforcement off therefore takes effect in about ten minutes. Cost allocation is roughly a day behind the invocation, so spend from the last day is not in the figure yet.

## Notes

* Prompts and completions are not delivered unless `bedrock.invocation_logging.include_request_response_data` is set to true. Leaving it false keeps the log group to metadata and token counts.
* An invocation is attributed to the user who owns the instance that made the call. Calls from hosts that are not IDEA sessions or jobs are counted against no project, and calls from a project role with no owning instance land in an unattributed bucket.
* `bedrock.usage.lookback_days` is the trailing window recomputed on every run. Keep it at or below `bedrock.invocation_logging.log_retention_in_days`: nothing enforces the relationship, and a window longer than retention simply reads days the log group no longer holds. Usage from before logging was enabled is not backfilled.
* Usage is not spend. The priced equivalent reaches Cost Explorer and the project budget about a day later, and is not backfilled for activity recorded before cost allocation tags were activated. Recorded tokens are never valued as money and feed no limit.
