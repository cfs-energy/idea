# Update a module's code (idea-admin.sh deploy)

{% hint style="info" %}
Use this when you have changed the code of one module (a new API, a fixed function) and want it running on your cluster without a full upgrade. Configuration-only changes go through [update-idea-configuration.md](update-idea-configuration.md "mention").
{% endhint %}

The `patch` command of earlier releases, which copied a build onto a running host over Systems Manager, no longer exists. A module's code now travels one of two ways, depending on where the module runs.

Deployment arguments are module IDs, not module names. Run
`./idea-admin.sh list-modules --cluster-name <CLUSTER_NAME> --aws-region <REGION>` and use its
**Module ID** column. Generated IDs are `cluster-manager`, `scheduler`, `vdc` for the module named
`virtual-desktop-controller`, and `bastion-host` for the bastion.

## Modules that run as containers

`cluster-manager`, `scheduler` and `virtual-desktop-controller` (the controller, the DCV broker and the DCV connection gateway) run as tasks from one control plane image on clusters with `enable_ecs: true`. To run changed code:

1. Follow the complete [image build-and-push workflow](../../../../.github/workflows/build_push.yaml), including its linked development setup and module-build actions. Dispatch from your patch branch with a separate `control_plane_image_name` input, the target public `ecr_repository`, and the repository's ECR push role configured. It builds the release bundles, stages the DCV signing key, broker RPM, both gateway RPMs and DCV archives with checksums, builds OpenPBS in a cached stage of the control-plane image with the `dcv-packages` context, then smoke-tests and promotes that image by digest. The image is built for `linux/arm64`; use a registry reachable from your cluster and verify the pushed image manifest supports its hosts. For private ECR, adapt the workflow's login/push steps to the target account and region as in [the migration preparation](move-to-containers.md#once-per-cluster).
2. Point the cluster at it: `./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> --force "Key=ecs.image,Type=str,Value=<repository>:<tag>"`
3. Deploy one module: `./idea-admin.sh deploy --upgrade <MODULE_ID> --cluster-name <CLUSTER_NAME> --aws-region <REGION>`, or every module: `./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION>`

For the desktop module, use `./idea-admin.sh deploy --upgrade vdc --cluster-name <CLUSTER_NAME> --aws-region <REGION>`; the module name `virtual-desktop-controller` is not its generated deployment ID. Use a new tag or digest for changed code so the task definition changes.

Most services roll behind their load balancer. The scheduler stops its old task before starting its replacement, briefly interrupting submissions and its API; running jobs are designed to survive using persistent PBS state. Desktop connections may need to reconnect through the gateway.

## Modules that run on hosts

The bastion host, and every module on a cluster that has not moved to containers, installs its release archive at boot from the cluster bucket. The deploy uploads that archive from `~/.idea/downloads/`; the control plane image ships the release's archives there, and a build of your own image carries yours. In developer mode (`IDEA_DEV_MODE=true`, running from source) build the archives with `invoke clean build package` and copy them from `dist/` into `~/.idea/downloads/` yourself. For application patches, build a distinct release version, not another archive with the same filename: keep `IDEA_VERSION.txt`, the administrator package/lockfile versions and the wrapper revision consistent, rebuild the administrator and bundles, and run that version's administrator. The version changes the release URI embedded in bootstrap, which makes the host replacement visible to CloudFormation. Then deploy the module:

```
./idea-admin.sh deploy --upgrade <MODULE_ID> --cluster-name <CLUSTER_NAME> --aws-region <REGION>
```

The deploy uploads release archives and a bootstrap archive named by its rendered content. Rebuilding application code under the same release filename overwrites S3 without necessarily changing bootstrap or replacing the host; `--force-build-bootstrap` only rerenders it and does not guarantee replacement. Use the distinct-version procedure above and check the proposed host replacement before proceeding.

If the change-set guard refuses the replacement, review the named logical ID, drain a host scheduler and arrange the module's outage before explicitly accepting it with `deploy --upgrade <MODULE_ID> --allow-replacement <LOGICAL_ID>` and the same cluster/region flags. The flag permits the proposed replacement; it does not force an unchanged host to be replaced.
