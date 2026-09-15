# Update a module's code (idea-admin.sh deploy)

{% hint style="info" %}
Use this when you have changed the code of one module (a new API, a fixed function) and want it running on your cluster without a full upgrade. Configuration-only changes go through [update-idea-configuration.md](update-idea-configuration.md "mention").
{% endhint %}

The `patch` command of earlier releases, which copied a build onto a running host over Systems Manager, no longer exists. A module's code now travels one of two ways, depending on where the module runs.

## Modules that run as containers

`cluster-manager`, `scheduler` and `virtual-desktop-controller` (the controller, the DCV broker and the DCV connection gateway) run as tasks from one control plane image on clusters with `enable_ecs: true`. To run changed code:

1. Build the control plane image from your checkout and push it to a repository your cluster can pull from (your own ECR repository, or the release repository for a published release).
2. Point the cluster at it: `./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> --force "Key=ecs.image,Type=str,Value=<repository>:<tag>"`
3. Roll one module: `./idea-admin.sh deploy <MODULE> --cluster-name <CLUSTER_NAME> --aws-region <REGION>`, or every module: `./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION>`

The service rolls to a new task definition revision behind its load balancer. A job running through a scheduler roll finishes normally, and a desktop connection reconnects through the gateway.

## Modules that run on hosts

The bastion host, and every module on a cluster that has not moved to containers, installs its release archive at boot from the cluster bucket. The deploy uploads that archive from `~/.idea/downloads/`; the control plane image ships the release's archives there, and a build of your own image carries yours. In developer mode (`IDEA_DEV_MODE=true`, running from source) build the archives with `invoke clean build package` and copy them from `dist/` into `~/.idea/downloads/` yourself. Then deploy the module:

```
./idea-admin.sh deploy <MODULE> --cluster-name <CLUSTER_NAME> --aws-region <REGION>
```

The deploy uploads the release archive and the rendered bootstrap package, and the host is replaced with one that installs them. A host whose bootstrap package did not change is left alone; the archive name is derived from its rendered content, so an unchanged package keeps its location.
