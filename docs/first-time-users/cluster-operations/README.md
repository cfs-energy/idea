# Cluster operations

* [Backfill metrics history](metrics-history.md)

Use [Settings](settings.md) for the searchable administrative page, its groups and save controls.

Health shows cluster modules and infrastructure hosts. On container clusters, an empty Infrastructure hosts section is hidden.

On container clusters, Desktop services and Job service show all control-plane services in one live table: cluster manager, scheduler, desktop controller, DCV broker, connection gateway, SSH bastion, and any additional services. Desktop services appear first on the desktop tab; the scheduler appears first on the job tab. The table shows desired, running and pending task counts, image tags from the active task definition, the primary deployment rollout state and last update time, and each running task's start time and health. Times use your browser's timezone. Refresh reads the current state again. Read failures are shown alongside available results; use Refresh to retry. Unavailable rollout or health information is identified explicitly.

Clusters without the container module retain module and deployment settings. Use the Operations runbooks for configuration changes, deployment, restarts and recovery.
