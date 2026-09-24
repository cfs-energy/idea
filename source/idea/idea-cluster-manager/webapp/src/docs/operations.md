## Operations

Health shows cluster modules and infrastructure hosts. On container clusters, an empty Infrastructure hosts section is hidden.

On container clusters, Desktop services and Job service show all control-plane services in one fixed-width live table, with desktop roles or the scheduler first for the selected page. Role, image and task popovers retain full names and details; rollout indicators show state and relative deployment time.

One image tag is shown when tasks agree, while multiple tags are counted and listed in a popover; digest-qualified images use **digest…** and expose the full digest. Task details include start time and health, and Refresh reads the current state again while preserving partial error messages.

Clusters without the container module retain module and deployment settings. Use the Operations runbooks for configuration changes, deployment, restarts and recovery.

[Runbooks](https://docs.idea-hpc.com/first-time-users/cluster-operations)
