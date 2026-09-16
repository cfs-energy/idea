# Developer Onboarding

Download the matching archive from the [GitHub release](https://github.com/cfs-energy/idea/releases) (replace `<VERSION>` with the release version):

| Operator platform | Release file |
| --- | --- |
| macOS Apple silicon | `ideactl-v<VERSION>-darwin-arm64.tar.gz` |
| Linux ARM64 | `ideactl-v<VERSION>-linux-arm64.tar.gz` |
| Linux x64 | `ideactl-v<VERSION>-linux-amd64.tar.gz` |
| Windows x64 | `ideactl-v<VERSION>-windows-amd64.zip` |

Each archive has a `.sha256` sidecar and is included in `SHA256SUMS`. Extract it to get one `ideactl` file (`ideactl.exe` on Windows). The Node runtime and deployment CLI are embedded; no Node or npm installation is required. Run `./ideactl about` on macOS/Linux or `.\ideactl.exe about` in PowerShell.

Windows releases have no code signing. SmartScreen may warn: after verifying the archive with `Get-FileHash -Algorithm SHA256` against the release checksum, choose **More info > Run anyway**, or run `Unblock-File .\ideactl.exe` in PowerShell before launching it. Organization policy may prevent this override.


## Pre-Requisites

* [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
* [Python3](https://www.python.org/downloads/) + [PyEnv](https://github.com/pyenv/pyenv)
* [nvm](https://github.com/nvm-sh/nvm)
* [yarn](https://yarnpkg.com/)
* [Git](https://git-scm.com/downloads)

{% hint style="warning" %}
#### Versions

Replace the variables in the code snippets below with the values in `software_versions.yml`
at the repository root (`python_version`, `node_version`). The CDK CLI needs no version of its
own: the deploy tool pins it.
{% endhint %}

## Prepare environment

### Set Environment Variables for Versions

```
PYTHON_VERSION=<see above>
NODEJS_VERSION=<see above>
```

### Install pyenv and nvm

{% tabs %}
{% tab title="Mac" %}
Using Homebrew:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install pyenv
brew install nvm
```
{% endtab %}

{% tab title="Windows" %}
Using Powershell:

<pre class="language-powershell"><code class="lang-powershell"><strong>Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1" -OutFile "./install-pyenv-win.ps1"; &#x26;"./install-pyenv-win.ps1"
</strong></code></pre>

Install NVM latest release from: [https://github.com/coreybutler/nvm-windows/releases](https://github.com/coreybutler/nvm-windows/releases)
{% endtab %}
{% endtabs %}

### Python \<PYTHON\_VERSION>

```bash
pyenv install --skip-existing $PYTHON_VERSION
```

### **NodeJS \<NODEJS\_VERSION>**

```bash
nvm install $NODEJS_VERSION
nvm use $NODEJS_VERSION
```

#### AWS CDK

The CDK CLI is a pinned dependency of the deploy tool under `source/idea/ideactl`; `npm ci`
there installs the version the release was tested with. Do not install it globally.

#### **Docker Desktop (Optional)**

Follow instructions on the below link to install Docker Desktop. (Required if you are working with creating Docker Images)

[https://docs.docker.com/desktop/mac/install/](https://docs.docker.com/desktop/mac/install/)

## Clone Git Repo

All PRs will be accepted only against the main branch.

```bash
git clone https://github.com/cfs-energy/idea.git
cd idea
// make your changes
```

## Virtual Environment

Activate your python virtual environment via:

```bash
PYENV_VERSION=$PYTHON_VERSION python -m venv venv
source venv/bin/activate
```

If your PYENV\_VERSION command is not working for any reason, you can create venv using below command:

```bash
$HOME/.pyenv/versions/$PYTHON_VERSION/bin/python3 -m venv venv
```

## Install Dev Requirements

```bash
pip install -r requirements/dev.txt
```

<details>

<summary><em><strong>Note for MacOS users</strong></em></summary>

_**BigSur Note:**_ cryptography and orjson library requirements fail to install on MacOS BigSur.

To fix **cryptography**, follow the instructions mentioned here:\
[https://stackoverflow.com/questions/64919326/pip-error-installing-cryptography-on-big-sur](https://stackoverflow.com/questions/64919326/pip-error-installing-cryptography-on-big-sur)

```
env LDFLAGS="-L$(brew --prefix openssl@1.1)/lib" CFLAGS="-I$(brew --prefix openssl@1.1)/include" pip install cryptography==36.0.1
```

To fix **orjson**, run:

```
brew install rust
# Upgrade your pip
python3 -m pip install --upgrade pip
```

</details>

## Verify Dev Setup

Run below command to check if development environment is working as expected, run:

```bash
invoke -l
```

Running this command should print output like below:

```
Available tasks:

  apispec.all (apispec)                build OpenAPI 3.0 spec for all modules
  apispec.cluster-manager              cluster-manager api spec
  apispec.scheduler                    scheduler api spec
  apispec.virtual-desktop-controller   virtual desktop controller api spec
  build.all (build)                    build all
  build.cluster-manager                build cluster manager
  build.data-model                     build data-model
  build.scheduler                      build scheduler
  build.sdk                            build sdk
  build.virtual-desktop-controller     build virtual desktop controller
  clean.all (clean)                    clean all components
  clean.cluster-manager                clean cluster manager
  clean.data-model                     clean data-model
  clean.scheduler                      clean scheduler
  clean.sdk                            clean sdk
  clean.virtual-desktop-controller     clean virtual desktop controller
  cli.cluster-manager                  invoke cluster-manager cli
  cli.scheduler                        invoke scheduler cli
  cli.virtual-desktop-controller       invoke virtual desktop controller cli
  devtool.build                        wrapper utility for invoke clean.<module> build.<module> package.<module>
  devtool.configure                    configure devtool
  devtool.ssh                          ssh into the workstation
  devtool.sync                         rsync local sources with remote development server
  devtool.upload-packages              upload packages
  package.all (package)                package all components
  package.cluster-manager              package cluster manager
  package.make-all-archive             build an all archive containing all package archived
  package.scheduler                    package scheduler
  package.virtual-desktop-controller   package virtual desktop controller
  release.build-opensource-dist        build open source package for Github
  release.build-s3-dist                build s3 distribution package for global assets
  release.update-version               update idea release version in all applicable places
  req.install                          Install python requirements
  req.update                           Update python requirements using pip-compile.
  tests.all (tests)                    run unit tests for all components
  tests.cluster-manager                run cluster-manager unit tests
  tests.scheduler                      run scheduler unit tests
  tests.sdk                            run sdk unit tests
  tests.virtual-desktop-controller     run virtual desktop controller unit tests
  tests.web-portal                     run cluster-manager web-portal (webapp) tests via vitest
  web-portal.serve                     serve web-portal frontend app in web-browser
  web-portal.typings                   convert idea python models to typescript
```

Clean, build and package the Python modules:

```bash
invoke clean build package
```

## Build the deploy tool

`idea-admin.sh` runs `ideactl`, a TypeScript program under `source/idea/ideactl`. It
needs Node.js 22 or newer; `software_versions.yml` names the version the images are
built with.

```bash
cd source/idea/ideactl
npm ci
npm run build
```

## Run idea-admin.sh in Developer Mode

`IDEA_DEV_MODE` selects where `idea-admin.sh` gets the deploy tool from.

If `IDEA_DEV_MODE=true`, the wrapper rebuilds `source/idea/ideactl` and runs it from
your checkout. If `IDEA_DEV_MODE=false` (the default), it pulls the control-plane
image for the release named in `IDEA_VERSION.txt` and runs `ideactl` in a container.

Export it before running the wrapper from the project root. It applies to that
terminal session only.

```bash
export IDEA_DEV_MODE=true
./idea-admin.sh about
```

`npm ci` has to have been run at least once; developer mode fails with an explicit
message if `node_modules` is missing.

## Publishing the container images

The Build and Push workflow in `.github/workflows/build_push.yaml` publishes the
images, and the release executables for the deploy tool.

### Normal path

Merging to `main` runs the workflow. It builds every Python module, assumes the OIDC
role held in the `ECR_ROLE` repository secret, then builds and pushes
`idea-scheduler-pbs` followed by `idea-control-plane` to `public.ecr.aws/s5o2b4m0`.
The control-plane image gets three tags: the contents of `IDEA_VERSION.txt`, the same
value prefixed with `v`, and `latest`. The scheduler image gets the `v` tag only,
because the control-plane build consumes it by that exact reference.

### Rerun path

If that run fails after the merge, dispatch the same workflow again rather than
publishing by hand:

```bash
gh workflow run build_push.yaml --ref main
```

Three inputs change the target. `ecr_repository` selects the registry, and
`control_plane_image_name` and `scheduler_image_name` select the repositories within
it. From a ref other than `main` the workflow stops immediately unless both image
names are set, so a branch dispatch cannot overwrite the released images:

```bash
gh workflow run build_push.yaml --ref release-26.09.0 \
  -f control_plane_image_name=idea-control-plane-ci-test \
  -f scheduler_image_name=idea-scheduler-pbs-ci-test
```

The named repositories have to exist already, because ECR Public does not create one
on push. Delete a throwaway repository once the check is finished.

### Emergency path

There is none. The control-plane image is built from a private package context that
only the workflow assembles, so CI is the only supported publisher. If the workflow
itself cannot run, fix the workflow.

### The publishing role

The role named by `ECR_ROLE` trusts any ref of this repository, so the image-name
guard above is the only control that stops a branch dispatch from replacing a
released image. Narrowing the role trust condition to `main` would remove the need
for that guard.
