# Application logs

IDEA applications default to `INFO` on both hosts and containers. Use these settings to change the level:

| Setting | Default | Effect |
| --- | --- | --- |
| `cluster.logging.default_log_level` | `INFO` | Cluster-wide application level, read at startup |
| `<module_id>.logging.default_log_level` | Inherit the cluster level | Override for that module, read at startup |
| `ecs.datadog.logs_enabled` | `true` | Collect labeled IDEA container logs when `ecs.datadog.enabled` is true |

The default module IDs are `cluster-manager`, `scheduler`, `vdc`, and `bastion-host`. The Python application overrides are `cluster-manager.logging.default_log_level`, `scheduler.logging.default_log_level`, and `vdc.logging.default_log_level`. Module-name keys such as `virtual-desktop-controller.logging.default_log_level` also work; the ID key wins when both are set. The old cluster-wide `logging.default_log_level` remains a fallback for one release; `cluster.logging.default_log_level` takes precedence and appears in the Settings page under Cluster → Logs.

Levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`. Invalid values fall back to `INFO` with one warning at startup. Restart the affected application processes after changing a level, or replace their ECS tasks. Handler profiles still choose console or file output; their application level no longer overrides these settings. Existing `logging.enable_debug_logging` lists under each module still enable selected logger prefixes.

The existing `cluster-manager.web_portal.default_log_level` takes precedence for the `web-portal` logger. Its numeric format stays available: `0` off, `1` error, `2` warning, `3` info, `4` debug, `5` trace (mapped to Python debug). The browser continues to use the same setting.

For a temporary controller investigation, set `vdc.logging.default_log_level` to `DEBUG`, restart the controller, and remove the override afterward to inherit the cluster level again. Deleted sessions' stale schedules are removed when their next scheduled event runs, after a strongly consistent read confirms absence and the schedule is at least 10 minutes old. A failed session read skips that schedule and allows the remaining hourly work to continue. Missing permissions and normal SSM `InProgress` notifications are logged at debug.

## Datadog container logs

When `ecs.datadog.enabled` is true, log collection is enabled by default. Set `ecs.datadog.logs_enabled` to `false` to keep the agent's metrics collection while disabling the new log configuration. Deploy the ECS stack and the application service stacks after changing this setting, since the agent environment and container labels are part of their task definitions.

Only labeled IDEA service containers and their file-tail sidecars are collected. Sources are `python` for cluster-manager, scheduler and VDC, `sshd` for bastion, `java` for the broker, and `nginx` for the gateway. The OpenPBS file-tail sidecar remains collected with source `openpbs`. The log `service` matches the container's `com.datadoghq.tags.service` value (for example, `cluster-manager` or `vdc`), with tags `idea_cluster:<cluster>` and `idea_module:<module>`. Python log events start at `^\[\d{4}-\d{2}-\d{2} ` so tracebacks remain in the same event. The Datadog agent excludes its own logs through `DD_CONTAINER_EXCLUDE_LOGS`, preserving its metrics. Every container keeps its `awslogs` driver, and CloudWatch remains the system of record.

The agent keeps its Docker socket mount and adds a read-only `/var/lib/docker/containers` mount. Collection uses the Docker socket explicitly because `awslogs` does not produce Docker JSON log files. This requires Docker's default dual logging cache to remain enabled. See [Datadog container collection troubleshooting](https://docs.datadoghq.com/containers/troubleshooting/log-collection/) and [Docker dual logging](https://docs.docker.com/engine/logging/dual-logging/).

After rollout on collab, confirm a new application message reaches both CloudWatch and Datadog with the expected service and tags. Check a production `application.log` message through its file-tail sidecar as well. Confirm agent and unrelated container logs are absent from Datadog, metrics still arrive, and setting `logs_enabled=false` stops log shipping after replacing the relevant tasks. Local synthesis tests cannot confirm agent discovery, host Docker cache availability, intake connectivity, or the pipeline end to end.
