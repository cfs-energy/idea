# IDEA CLI utility

`idea-admin.sh` utility is designed to simplify your interaction with your IDEA environment. With this simple tool, you can install a new environment, delete an existing cluster, update the configuration of a live environment or even retrieve the connection endpoints of your deployment via simple 1-liner commands. See some examples below:

* Install a new cluster: `quick-setup` ( [install-idea](../first-time-users/install-idea/ "mention"))
* Generate an empty `values_files.yml` that can be used as base skeleton for your upcoming installation: `quick-setup-help`
* Delete an existing environment: `delete`
* Retrieve connection endpoints: `show-connection-info`
* Run integration test for all modules: `run-integration-tests`
* Update your cluster configuration via `config`  ( [update-idea-configuration.md](../first-time-users/cluster-operations/update-idea-cluster/update-idea-configuration.md "mention"))
* Patch an existing cluster via `patch` ( [patch-idea-module.md](../first-time-users/cluster-operations/update-idea-cluster/patch-idea-module.md "mention"))
* Upgrade backend component via `deploy` ( [update-idea-backend-resource.md](../first-time-users/cluster-operations/update-idea-cluster/update-idea-backend-resource.md "mention"))
* Manage additional FileSystems easily via `shared-storage` ( [Storage Management](https://app.gitbook.com/s/5SSt4opQQGbm5tAfuEqy/storage-management "mention"))

```bash
  about                    print IDEA release version info
  bootstrap                bootstrap cluster
  build-bootstrap-package  build bootstrap package for a module
  cdk                      cdk app
  check-cluster-status     check status for all applicable cluster endpoints
  config                   configuration management options
  delete-cluster           delete cluster
  deploy                   deploy modules
  directoryservice         directory service utilities
  list-modules             list all modules for a cluster
  patch                    patch application module with the current release
  quick-setup              Install a new cluster
  quick-setup-help         display quick-setup help
  run-integration-tests    run integration tests for a module
  shared-storage           shared-storage options
  show-connection-info     print cluster connection information
  sso                      single sign-on configuration options
  support                  support options
  upload-packages          upload applicable packages for a module
  utils                    cluster configuration utilities

```

{% hint style="info" %}
Always use -h at the end of your command to list all available options
{% endhint %}
