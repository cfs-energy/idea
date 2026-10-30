# ECS upgrade replay

Run from the package directory:

```sh
node tools/parity/upgrade-dry-run.ts --capture <directory> --values <values.yml> --templates <directory> --inventory <inventory.json>
```

A 26.10.x ECS capture needs `cluster-settings.json` and `modules.json` in DynamoDB scan format,
including every page. Each deployed module needs a JSON template body named
`<stack-name>.json`. Values must describe the captured cluster and its desired deployment.
The command copies values into a temporary directory and changes only replay state.
It accepts the reported configuration drift for the simulated run.

`--inventory` supplies a JSON object with these fields. Table rows use document JSON,
not DynamoDB attribute wrappers. Explicit empty arrays distinguish empty inventories
from missing captures.

| Field | Input |
| --- | --- |
| `tables` | Map of full table names to all queue-profile, software-stack and user-session rows, including all pages |
| `images` | EC2 image metadata (`ImageId`, `Name`, `CreationDate`) for stored AMIs and the target release AMI |
| `stacks` | Map of stack names to their EC2 instance IDs, including empty arrays |
| `protection` | Map of instance IDs to termination-protection booleans |
| `protectionTags` | IDs carrying the saved protection marker |
| `deploymentSettings` | Expected published `{key, value}` rows after simulated deployment, including each DCV host policy ARN |
| `instanceTypes` | Offered EC2 instance types |
| `openSearchTypes` | Supported OpenSearch instance types for the captured engine |
| `trunking` | Effective ECS trunking setting as a boolean |

`deploymentSettings` contains expected outcomes, not observations of a deployment. The report labels deployment as simulated. Module registration, settings sync, EOL deletion/disabling, protection markers, and simulated publication update replay state.

Below-floor captures refuse before any writes. This replay cannot establish IAM propagation, actual stack transitions, image boot, desktop reconnects, running-job behavior, broker billing, or index reconciliation.
