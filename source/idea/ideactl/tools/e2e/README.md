# Control-plane E2E harness

These tools exercise an already deployed control plane. They do not make AWS SDK calls: provide the relevant load-balancer hostname, a user name, and the path to a file containing that user's password. No hostname, credential path, or cluster identifier is embedded in the tools.

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
It prints the action, observations, and a `PASS` or `FAIL` line for each selected check. It deletes
every desktop it creates, including after a failed gateway or task-replacement check.

```sh
node tools/e2e/proof-matrix.ts \
  --check desktop-end-to-end \
  --alb-host control-plane.example.invalid \
  --username test-user \
  --password-file /secure/path/password.txt \
  --gateway-host gateway.example.invalid \
  --desktop-request '{"session":{"name":"proof-desktop"}}'
```

The available checks are:

- `desktop-end-to-end`, create a desktop, wait for `READY`, verify a gateway connection, and delete it.
- `gateway-task-kill`, preserve a desktop connection while a gateway task is replaced.
- `broker-task-kill`, preserve a desktop connection while a broker task is replaced.
- `scheduler-replacement`, replace a scheduler task while a witnessed job runs, and prove the run
  was carried across rather than requeued. The job script exits 17 on its own second execution, so
  a requeued job cannot reach the expected exit status, and the run's start time is compared across
  the replacement wherever the job API reports it. It needs the job to outlive the replacement, so
  `--job-sleep-seconds` defaults to 1800 for this check.
- `job-burst`, submit concurrent short jobs and verify their exit statuses.
- `api-load`, run `load-api.ts` and enforce its p95 and error thresholds.
- `gateway-load`, run `load-gateway.ts` and enforce its handshake and failure thresholds.

Pass `--check` more than once or provide a comma-separated list. With no `--check`, the matrix
runs every check. Connection, service, task, target-group, and threshold values may instead be
provided through the matching `IDEA_E2E_*` environment variable. Run
`node tools/e2e/proof-matrix.ts --help` for every flag and the required flags for each check.

Task-replacement checks require permissions to stop a task, describe the selected service, and
describe target health. The selected task must belong to the selected service. The matrix waits for
the service's desired running count and, where applicable, the target group's healthy count.
