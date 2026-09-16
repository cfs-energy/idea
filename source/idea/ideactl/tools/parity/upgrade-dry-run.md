# Historical upgrade replay

Run from the package directory:

```sh
node tools/parity/upgrade-dry-run.ts --capture <directory> --values <values.yml> --templates <directory> --inventory <inventory.json>
```

A 25.11 capture needs `cluster-settings.json` and `modules.json` in DynamoDB scan format,
including every page. Each deployed module needs a JSON template body named
`<stack-name>.json`. Values must describe the captured cluster and its desired deployment.
The command copies values into a temporary directory and changes only replay state.
It accepts the reported configuration drift for the simulated run.

`--inventory` supplies a JSON object with these fields. Table rows use document JSON,
not DynamoDB attribute wrappers. Explicit empty arrays distinguish empty inventories
from missing captures, which must refuse historical planning.

| Field | Input |
| --- | --- |
| `tables` | Map of full table names to all queue-profile, software-stack and user-session rows, including all pages |
| `images` | EC2 image metadata (`ImageId`, `Name`, `CreationDate`) for stored AMIs and the target release AMI |
| `stacks` | Map of stack names to their EC2 instance IDs, including empty arrays |
| `protection` | Map of instance IDs to termination-protection booleans |
| `protectionTags` | IDs carrying the saved protection marker |
| `iam` | Map of DCV host role names to `attached` managed-policy names, `inline` names, `collision` boolean and `available` policy slots |
| `deployedIam` | Expected IAM state after simulated deployment, in the same shape |
| `deploymentSettings` | Expected published `{key, value}` rows after simulated deployment, including each DCV host policy ARN |
| `instanceTypes` | Offered EC2 instance types |
| `openSearchTypes` | Supported OpenSearch instance types for the captured engine |
| `jobs` | Map of scheduler host IDs to `queued`, `running` and `other` job counts |
| `trunking` | Effective ECS trunking setting as a boolean |

The IAM collision input is true when the target managed-policy name already exists outside
its owning deployed stack. Available slots must account for both account policy capacity and
role attachment capacity. `deployedIam` and `deploymentSettings` are expected outcomes, not
observations of a deployment. The report explicitly labels deployment as simulated.

Module registration, settings sync, EOL deletion/disabling, protection markers and simulated
publication update replay state. A missing historical inventory refuses before writes.
The synthetic 25.11 rehearsal runs without a private capture and covers direct completion,
EOL deletion, built-image preservation and protection restoration.

Only a real 25.11 cluster can establish IAM propagation, quota enforcement, stack transition
and rollback, old-runtime coexistence with rewritten globals, image boot and package
compatibility, desktop reconnects, running-job behavior, broker billing and index reconciliation.
