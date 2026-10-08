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

## Dependency updates

Renovate runs once a week, early Monday, from `.github/renovate.json`:

* Minor, patch, pin and digest updates arrive together in one pull request. Majors arrive one per dependency so each is judged on its own.
* Platform pins move by hand in their own change: the Node line (`software_versions.yml` and the image's `NODE_VERSION`, kept in step), the Python line, the Amazon Linux base image, the CI runner images and the JDK. Patch releases within a pinned line still flow.
* The Datadog agent default in `src/config/datadog-agent.ts` moves with its digest.
* Vulnerability alerts open immediately, outside the schedule.
* OpenPBS is pinned with a checksum in the control-plane Dockerfile and moves by hand.

Renovate's hosted app cannot regenerate this repository's lock files in its sandbox, so a Renovate pull request arrives with its manifests updated and its lock files stale until they are regenerated on the branch.

## Publishing the container image

The Build and Push workflow in `.github/workflows/build_push.yaml` publishes the
control-plane image, and the release executables for the deploy tool.

### Release candidates

Every release is published twice: first as a release candidate that is proven on a
cluster, then unchanged as the release.

1. On the release branch, with the version already bumped, dispatch the workflow as
   candidate 1:

   ```bash
   gh workflow run build_push.yaml --ref <release-branch> -f release_candidate=1
   ```

   It runs the checks and tests, builds and smoke-tests the ideactl executables for
   every platform (including a synthesis of every module with the packaged executable
   that must match a build from source), and builds and smoke-tests the control-plane
   image. It then publishes the GitHub prerelease `v<version>-rc.1` with the
   executables, their checksums and `candidate.json`, and tags the image digest
   `<version>-rc.1`. A candidate never receives a release tag.
2. Prove the candidate on a development cluster with only its files: download the
   prerelease's ideactl, set `ecs.image` to `<repository>:<version>-rc.1`, and upgrade.
3. If anything needs fixing, fix it on the branch and dispatch the next number
   (`release_candidate=2`). A number is never reused.
4. Merge the release pull request. On `main` the workflow builds nothing: it finds the
   highest candidate for this version whose `candidate.json` records the exact source
   tree being merged, tags that candidate's image digest `<version>`, `v<version>` and
   `latest`, and publishes the release with the candidate's files. If no candidate
   matches the merged tree, nothing is released; cut a candidate from that source.

A squash merge keeps the source tree identical to the branch, so the candidate built
from the branch head matches. A merge that changes the tree, such as one picking up a
newer `main`, needs a new candidate from the updated branch.

An upgrade moves `ecs.image` to the release image only after the registry confirms that
image exists; if it does not exist or the registry cannot be reached, the upgrade stops
before writing anything. A cluster on a candidate of the release being installed keeps
its candidate image, since that is the build being proven, and a candidate of an earlier
release moves to the release.

### Rerun path

If the promotion fails after the merge, rerun the failed run from the Actions page.
It refuses to overwrite a release that already exists.

### Private builds

A dispatch with `control_plane_image_name` builds and smoke-tests a private image in
that repository and writes no release or candidate tags:

```bash
gh workflow run build_push.yaml --ref <branch> \
  -f control_plane_image_name=idea-control-plane-ci-test
```

`ecr_repository` selects the registry. A dispatch without either input stops
immediately. The named repository has to exist already, because ECR Public does not
create one on push. Delete a throwaway repository once the check is finished.

### Emergency path

There is none. The control-plane image is built from a private package context that
only the workflow assembles, so CI is the only supported publisher. If the workflow
itself cannot run, fix the workflow.

### The publishing role

The role named by `ECR_ROLE` trusts any ref of this repository, because a release
candidate is pushed from its branch. The dispatch guards are the only control that
stops a branch from writing release tags: a dispatch writes only a candidate tag or a
private repository, and release tags are written only by the promotion on `main`.
