# Control-plane E2E harness

These tools exercise an already deployed control plane. The API and load tools do not make AWS SDK calls: provide the relevant load-balancer hostname, a user name, and the path to a file containing that user's password. No hostname, credential path, or cluster identifier is embedded in the tools.

The API tools cache an access token in `~/.ideactl/e2e/tokens/`, using a separate URL-escaped filename for each user. Pass `--token-dir` to use a different cache location. TLS certificates are verified by default; use `--insecure` only when intentionally testing an endpoint with a certificate the test host does not trust.

## Direct API request

```sh
node tools/e2e/api.ts \
  --alb-host control-plane.example.net \
  --username test-user \
  --password-file /secure/path/password.txt \
  --namespace Projects.ListProjects \
  --payload '{}'
```

`api.ts` routes namespaces beginning with `Scheduler` to the scheduler API and namespaces beginning with `VirtualDesktop` to the VDC API. All other namespaces use the cluster-manager API. It refreshes the cached token and retries once if an API response reports an expired or unauthorized token.

## API load

```sh
node tools/e2e/load-api.ts \
  --alb-host control-plane.example.net \
  --username test-user \
  --password-file /secure/path/password.txt \
  --rps 50 \
  --seconds 300 \
  --workers 64
```

The request mix mirrors a portal session: cluster settings and account reads, project listing, VDC session and software-stack lists, and scheduler job and queue lists. The initial project listing supplies the `project_id` for `VirtualDesktop.ListSoftwareStacks`. It reports 30-second windows plus final request rate, p50/p95/p99, maximum latency, and error counts.

## Gateway TLS load

```sh
node tools/e2e/load-gateway.ts \
  --host gateway.example.net \
  --port 443 \
  --connections 5000 \
  --ramp 60 \
  --hold 120
```

This opens TLS connections over the ramp period, sends a keep-alive HTTP request after each handshake, holds successful connections, and reports open/failure counts with handshake timing statistics.

## Job burst

```sh
node tools/e2e/load-jobs.ts \
  --alb-host control-plane.example.net \
  --username test-user \
  --password-file /secure/path/password.txt \
  --count 12
```

This submits short PBS jobs through `Scheduler.SubmitJob`, polls `Scheduler.ListActiveJobs`, and prints matching completed-job records from `Scheduler.ListCompletedJobs`. Use `--poll-seconds` and `--max-polls` to bound polling.

Run any tool with `--help` for its accepted flags. Required connection and credential flags intentionally have no defaults.

## ECS cutover proof matrix

`proof-matrix.ts` runs the required ECS cutover checks and exits nonzero when any check fails.
It prints the action, observations, and a `PASS` or `FAIL` line for each selected check. It attempts desktop cleanup after reaching `READY`, including after later gateway or replacement
failures. A desktop that fails to reach `READY` can remain, and deletion itself can fail: after a
failed run, list your sessions, identify the proof desktop by name and creation time, delete it in
the portal, and verify that both its session and instance are removed before retrying.

The matrix uses AWS SDK credentials from the standard credential chain for ECS and ELB checks;
select the target account with `AWS_PROFILE` and pass `--region`. Task-replacement checks call
`StopTask`, and `scheduler-image-upgrade` executes the supplied upgrade command; these checks
mutate the selected deployment.

Job checks require access to the `normal` queue. The shared submission helper selects project
`default`, so the test user must also have access to that project and permission to submit jobs.

Discover desktop inputs using the same user that will run the proof (`jq` is required for the last command). Inspect `/tmp/proof-stacks.json` and select the stack ID before running the instance-type query; it passes the full stack object so architecture and instance restrictions apply:

```sh
node tools/e2e/api.ts --alb-host control-plane.example.invalid --username test-user \
  --password-file /secure/path/password.txt --namespace Projects.ListProjects --payload '{}'
node tools/e2e/api.ts --alb-host control-plane.example.invalid --username test-user \
  --password-file /secure/path/password.txt --namespace VirtualDesktop.ListSoftwareStacks \
  --payload '{"project_id":"<PROJECT_ID>"}' > /tmp/proof-stacks.json
node tools/e2e/api.ts --alb-host control-plane.example.invalid --username test-user \
  --password-file /secure/path/password.txt --namespace VirtualDesktopUtils.ListAllowedInstanceTypes \
  --payload "$(jq -c --arg id '<STACK_ID>' \
    '{hibernation_support:false,software_stack:(.payload.listing[] | select(.stack_id==$id))}' /tmp/proof-stacks.json)"
```

Choose an accessible project and an enabled software stack assigned to it; take `stack_id`, `base_os`
and `min_storage` from the stack response. Choose an allowed instance type, a compatible session
type (`VIRTUAL` for ARM64 Linux, `CONSOLE` for Windows), and a root volume in GiB at least the stack's `min_storage` (convert its unit if needed)
and within the cluster limit. The example below uses hibernation disabled and a 40 GiB volume;
adjust that size to the selected stack, and replace every placeholder before running. If enabling
hibernation, rediscover instance types with `hibernation_support:true` and add instance RAM to the
minimum root volume. An administrator can read the maximum from the repository root with
`./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> --query vdc.dcv_session.max_root_volume_memory`.

```sh
node tools/e2e/proof-matrix.ts \
  --check desktop-end-to-end \
  --alb-host control-plane.example.invalid \
  --username test-user \
  --password-file /secure/path/password.txt \
  --gateway-host gateway.example.invalid \
  --desktop-request '{"session":{"name":"proof-desktop","project":{"project_id":"<PROJECT_ID>"},"software_stack":{"stack_id":"<STACK_ID>","base_os":"<BASE_OS>"},"type":"VIRTUAL","hibernation_enabled":false,"server":{"instance_type":"<INSTANCE_TYPE>","root_volume_size":40}}}'
```

For recovery, call `VirtualDesktop.ListSessions` with `{}` using `api.ts`, then
`VirtualDesktop.DeleteSession` with `{"session":{"idea_session_id":"<SESSION_ID>"}}` for the
leftover proof session. Poll the session list and check the instance termination in EC2; if deletion
fails, retain the response and ask the cluster administrator to recover that session.

The available checks are:

- `desktop-end-to-end`, create a desktop, wait for `READY`, verify a gateway connection, and delete it.
- `desktop-stream`, create a desktop and open a DCV session to it through the gateway exactly as the
  web client does (the `/ws` WebSocket with the `dcv` subprotocol and the connection request); the
  desktop's DCV server must answer with a confirm. An abort names the gateway's reason, such as
  `SERVER_UNREACHABLE`. This is the only check that exercises the gateway-to-desktop leg.
- `desktop-ssh`, create a desktop and SSH into it the way a user does: the key the portal issues
  (`Auth.GetUserPrivateKey`), through the bastion (`--bastion-host`), to the desktop's private
  address. The key lives only in a temporary directory for the check's duration.
- `gateway-task-kill`, replace one gateway task while a desktop connection is open. A flow through
  the network load balancer is pinned to one task, so the connection either survives (it was on the
  other task) or closes and a new one must open once the service has recovered; both pass, and the
  observation says which.
- `broker-task-kill`, replace one broker task while a desktop connection is open; the session's
  connection info must still resolve afterwards, and the connection is judged as above (the check's
  idle flow was closed within the minute the broker took to recover, so it usually reopens).
- `scheduler-replacement`, replace a scheduler task while a witnessed job runs, and prove the run
  was carried across rather than requeued. The job script exits 17 on its own second execution, so
  a requeued job cannot reach the expected exit status, and the run's start time is compared across
  the replacement wherever the job API reports it. It needs the job to outlive the replacement, so
  `--job-sleep-seconds` defaults to 1800 for this check.
- `scheduler-image-upgrade`, run the operator's own upgrade command (`--upgrade-command`, through
  `sh -c`) while a witnessed job runs, then apply the scheduler-replacement proof to it. This is
  the check for the routine upgrade path: a new image tag rolled through `upgrade-cluster` must not
  requeue a running job.
- `job-burst`, submit concurrent short jobs and verify their exit statuses.
- `api-load`, run `load-api.ts` and enforce its p95 and error thresholds.
- `gateway-load`, run `load-gateway.ts` and enforce its handshake and failure thresholds.
- `metrics-sink`, query Datadog for `idea.api_invocations` filtered by `idea_cluster:<cluster>`
  over the last 15 minutes. At least one non-null point passes; the observations include the
  point count and newest timestamp in UTC. No points or a query error fails the check.

For `metrics-sink`, provide `--cluster`, `--datadog-api-key <key>` and `--datadog-app-key <key>`.
`--datadog-site` defaults to `datadoghq.com` and selects only the query endpoint. The supplied daemon and cost-only deployments omit `DD_SITE` and use US1 with the standard agent image; another query site does not redirect ingestion.
These accept `IDEA_E2E_CLUSTER`, `IDEA_E2E_DATADOG_API_KEY`, `IDEA_E2E_DATADOG_APP_KEY`
and `IDEA_E2E_DATADOG_SITE` as environment alternatives. Without both keys the check prints
`NOT RUN` and does not fail the matrix. This check needs no portal credentials.

```sh
node tools/e2e/proof-matrix.ts --check metrics-sink --cluster sample-cluster \
  --datadog-api-key "$IDEA_E2E_DATADOG_API_KEY" \
  --datadog-app-key "$IDEA_E2E_DATADOG_APP_KEY"
```

Pass `--check` more than once or provide a comma-separated list. With no `--check`, the matrix
runs every check. Connection, service, task, target-group, and threshold values may instead be
provided through the matching `IDEA_E2E_*` environment variable. Run
`node tools/e2e/proof-matrix.ts --help` for every flag and the required flags for each check.

Task-replacement checks require permissions to stop a task, describe the selected service, and
describe target health. The selected task must belong to the selected service. The matrix waits for
the service's desired running count and, where applicable, the target group's healthy count.
