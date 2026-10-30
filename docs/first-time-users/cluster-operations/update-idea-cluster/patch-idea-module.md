# Update a module's code (idea-admin.sh deploy)

The control plane runs only on ECS. Direct upgrades require 26.10.0 or newer on every deployed module. Use the 26.10.9 administrator to complete any older upgrade and ECS migration first. See the [recovery command](upgrade-cluster.md#recovery-with-the-26109-administrator).

{% hint style="info" %}
Use this when you have changed the code of one module (a new API, a fixed function) and want it running on your cluster without a full upgrade. Configuration-only changes go through [update-idea-configuration.md](update-idea-configuration.md "mention").
{% endhint %}

The `patch` command of earlier releases, which copied a build onto a running host over Systems Manager, no longer exists. Control-plane code is shipped in the ECS image.

Deployment arguments are module IDs, not module names. Run
`./idea-admin.sh list-modules --cluster-name <CLUSTER_NAME> --aws-region <REGION>` and use its
**Module ID** column. Generated IDs are `cluster-manager`, `scheduler`, `vdc` for the module named
`virtual-desktop-controller`, and `bastion-host` for the bastion.

## Modules that run as containers

`cluster-manager`, `scheduler` and `virtual-desktop-controller` (the controller, the DCV broker and the DCV connection gateway) run as tasks from one control plane image on all supported clusters. To run changed code:

1. Follow the complete [image build-and-push workflow](../../../../.github/workflows/build_push.yaml), including its linked development setup and module-build actions. Dispatch from your patch branch with a separate `control_plane_image_name` input, the target public `ecr_repository`, and the repository's ECR push role configured. It builds the release bundles, stages the DCV signing key, broker RPM, both gateway RPMs and DCV archives with checksums, builds OpenPBS in a cached stage of the control-plane image with the `dcv-packages` context, then smoke-tests the image. A private build writes no release tags; the run's summary names the image digest. The image is built for `linux/arm64`; use a registry reachable from your cluster and verify the pushed image manifest supports its hosts. For private ECR, adapt the workflow's login/push steps to the target account and region as in [ECS preparation](ecs-control-plane.md#once-per-cluster).
2. Point the cluster at it: `./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> --force "Key=ecs.image,Type=str,Value=<repository>@<digest>"`
3. Deploy one module: `./idea-admin.sh deploy --upgrade <MODULE_ID> --cluster-name <CLUSTER_NAME> --aws-region <REGION>`, or every module: `./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION>`

For the desktop module, use `./idea-admin.sh deploy --upgrade vdc --cluster-name <CLUSTER_NAME> --aws-region <REGION>`; the module name `virtual-desktop-controller` is not its generated deployment ID. Use a new tag or digest for changed code so the task definition changes.

Most services roll behind their load balancer. The scheduler stops its old task before starting its replacement, briefly interrupting submissions and its API; running jobs are designed to survive using persistent PBS state. Desktop connections may need to reconnect through the gateway.
