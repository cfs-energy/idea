# IDEA CLI utility

The control plane runs only on ECS. Direct upgrades require 26.10.0 or newer on every deployed module. Use the 26.10.9 administrator to complete any older upgrade and ECS migration first. See the [recovery command](../first-time-users/cluster-operations/update-idea-cluster/upgrade-cluster.md#recovery-with-the-26109-administrator).

Download the matching archive from the [GitHub release](https://github.com/cfs-energy/idea/releases) (replace `<VERSION>` with the release version):

| Operator platform | Release file |
| --- | --- |
| macOS Apple silicon | `ideactl-v<VERSION>-darwin-arm64.tar.gz` |
| Linux ARM64 | `ideactl-v<VERSION>-linux-arm64.tar.gz` |
| Linux x64 | `ideactl-v<VERSION>-linux-amd64.tar.gz` |
| Windows x64 | `ideactl-v<VERSION>-windows-amd64.zip` |

Each archive has a `.sha256` sidecar and is included in `SHA256SUMS`. Extract it to get one `ideactl` file (`ideactl.exe` on Windows). The Node runtime and deployment CLI are embedded; no Node or npm installation is required. Run `./ideactl about` on macOS/Linux or `.\ideactl.exe about` in PowerShell.

Windows releases have no code signing. SmartScreen may warn: after verifying the archive with `Get-FileHash -Algorithm SHA256` against the release checksum, choose **More info > Run anyway**, or run `Unblock-File .\ideactl.exe` in PowerShell before launching it. Organization policy may prevent this override.

On Windows, certificate generation still needs `openssl` on PATH and diagnostic archive collection needs `tar`. The release smoke covers configuration generation and an offline identity-provider synthesis, not a live cluster deployment. Session Manager links open the AWS console; the executable does not bundle or invoke the Session Manager plugin.


`idea-admin.sh` utility is designed to simplify your interaction with your IDEA environment. With this simple tool, you can install a new environment, delete an existing cluster, update the configuration of a live environment or even retrieve the connection endpoints of your deployment via simple 1-liner commands. See some examples below:

* Install a new cluster: `quick-setup` ([install-idea](../first-time-users/install-idea/ "mention"))
* Generate an empty `values.yml` that can be used as base skeleton for your upcoming installation: `quick-setup-help`
* Delete an existing environment: `delete-cluster`
* Retrieve connection endpoints: `show-connection-info`
* Run integration test for all modules: `run-integration-tests`
* Update your cluster configuration via `config` ( [update-idea-configuration.md](../first-time-users/cluster-operations/update-idea-cluster/update-idea-configuration.md "mention"))
* Update an application module via `deploy --upgrade` ([patch-idea-module.md](../first-time-users/cluster-operations/update-idea-cluster/patch-idea-module.md "mention"))
* Upgrade backend component via `deploy` ( [update-idea-backend-resource.md](../first-time-users/cluster-operations/update-idea-cluster/update-idea-backend-resource.md "mention"))
* Manage additional FileSystems easily via `shared-storage` ([storage-management.md](../modules/storage/storage-management.md "mention"))

```text
Usage: ideactl [options] [command]

IDEA cluster administration

Options:
  -V, --version                                 output the version number
  -h, --help                                    display help for command

Commands:
  about [options]                               print the release version
  quick-setup-help                              display quick-setup help
  quick-setup [options]                         Install a new cluster
  config                                        configuration options
  cdk                                           cdk options
  bootstrap [options]                           bootstrap cluster
  deploy [options] <modules...>                 deploy modules. Use `all` as the module id to deploy all modules.
  cost-collector                                Deploy or remove account spend collection without a cluster
  replace [options] <component>                 replace one stateful component, deliberately. Upgrade never does this.
  check-cluster-status [options]                check status for all applicable cluster endpoints
  list-modules [options]                        list all modules for a cluster
  show-connection-info [options]                print cluster connection information
  upgrade-cluster [options] [modules...]        upgrade an existing cluster
  return-hosts [options]                        drain and remove a container host the host group holds beyond its minimum
  delete-cluster [options]                      delete cluster
  delete-backups [options]                      delete all recovery points in the cluster's backup vault
  sso                                           single sign-on configuration
  directoryservice                              directory service commands
  shared-storage                                shared storage commands
  utils                                         utility commands
  backup-update-global-settings [options]
  support                                       support options
  run-integration-tests [options] <modules...>
  help [command]                                display help for command
```

{% hint style="info" %}
Always use -h at the end of your command to list all available options
{% endhint %}
