# Move the control plane to containers

From this release the cluster manager, the scheduler and the virtual desktop controller with its DCV broker and connection gateway can run as container tasks on a small pool of hosts, instead of one host each. A cluster moves over once, with `upgrade-cluster`; every later upgrade is then a rolling image change that keeps jobs running. This page is the runbook for that one move.

## Before you start

* The cluster must be on the previous published release. `upgrade-cluster` refuses an older one; upgrade it one release at a time first.
* The account must have the `awsvpcTrunking` ECS setting enabled in the cluster's region. The upgrade checks and refuses otherwise.
* The container hosts are Graviton (`ecs.hosts.instance_type`, default `m7g.large`). Check the type is offered in the cluster's subnets' availability zones.
* The control plane image must be reachable from the cluster: `ecs.image` defaults to the release image in the public repository for the commercial partition. A partition with no repository entry (GovCloud) needs the image pushed to a repository in that account and `ecs.image` set to it, or the stack refuses.
* Job submission will close for the length of the run, and users see a maintenance message when they try to submit. Announce a window.

## Run it

1. Add `enable_ecs: true` to the cluster's `values.yml` (the local copy under `~/.idea/clusters/<CLUSTER_NAME>/<REGION>/`, or the copy the upgrade restores from the cluster bucket).
2. Run the upgrade with the drain:

```
./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION> --drain
```

3. Read the configuration preview it prints and confirm. Rows you changed by hand are preserved; the run says which.

## What happens, in order

* The scheduler's DNS record is retained on its stack with a policy-only update, so the container scheduler can take the name over without CloudFormation deleting it.
* Job submission closes and the run waits until the host scheduler holds no job. Nothing is written before the inventory is empty.
* Settings are written: the global rows are rewritten, the container module's rows are added, and the module's registration in the module set is held until the last stack has deployed, so the running portal keeps working through the upgrade.
* Stacks deploy in dependency order. A new `ecs` stack creates the host pool. The cluster-manager stack cuts its load balancer rules over to the container service; the portal is unavailable for a few minutes until that stack completes. The desktop stack moves the controller, broker and gateway to services. The scheduler stack starts the scheduler task on an empty job database and retires the host. The bastion host is recreated once.
* Submission reopens with the maintenance flag restored to what it was.

## After

* Job ids start again from zero, once. From now on the scheduler's job database lives on a file system that outlives every task, and later upgrades carry running jobs across.
* The bastion has a new public address. Anything that pinned the old one needs the new one.
* `./idea-admin.sh check-cluster-status --cluster-name <CLUSTER_NAME> --aws-region <REGION>` should report every module healthy.

## Routine upgrades from here

Point `ecs.image` at the new release's image and run `upgrade-cluster` without `--drain`. Each service rolls to a new task definition revision behind its load balancer; a job running through the scheduler roll finishes normally. The upgrade refuses any change that would replace or remove a stateful resource and names it, and accepts a task definition revision by name, since the previous revision is kept.
