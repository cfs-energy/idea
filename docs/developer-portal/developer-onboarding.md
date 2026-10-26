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


## Prerequisites

Install [Git](https://git-scm.com/downloads) and
[mise](https://mise.jdx.dev/getting-started.html). Deployment also needs the
[AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
Activate mise in your shell, for example `eval "$(mise activate zsh)"` for zsh.

`mise.toml` owns exact developer and CI tool versions, including Node, Python, the lock
compiler, and the hook linters. Application libraries retain their existing lockfiles.
`software_versions.yml` selects the deployed runtimes; keep its Node and Python versions
aligned with mise when changing those runtimes.

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
mise trust
mise install
hk install
hk check --all
```

## Virtual Environment

Create a virtual environment with the pinned Python:

```bash
mise exec -- python -m venv venv
source venv/bin/activate
```

## Install Dev Requirements

```bash
pip install -r requirements/dev.txt
```

## Run checks

Run `hk check --all` or `mise run lint` before submitting a change. Use `hk fix --all`
to apply available fixes, then review and stage them. The installed commit hook also
applies fixes and stops for review when a file changes.

After installing development requirements, use `mise run test:data-model`, `test:sdk`,
`test:cluster-manager`, `test:scheduler`, `test:virtual-desktop-controller`, or
`test:web-portal`. These legacy suites use Corretto 17; select it with
`MISE_ENV=legacy mise install` and `MISE_ENV=legacy mise run test:sdk`.
For ideactl, run `npm ci` in `source/idea/ideactl`, then use `mise run ideactl:build`
and `mise run ideactl:checks` from the repository root.

## Development commands

Run `mise tasks ls` to list commands. Activate the development virtual environment
before invoking service tasks; `VIRTUAL_ENV` selects its Python interpreter.

| Command | Purpose |
| --- | --- |
| `mise run clean [module]` | Remove generated service artifacts |
| `mise run build [module]` | Build service source archives, resources and portal |
| `mise run package [module]` | Package built services and dependencies |
| `mise run apispec [module]` | Export OpenAPI specs; `all --target-dir PATH` writes all three |
| `mise run web-portal:typings` | Generate portal interfaces from service models |
| `mise run web-portal:serve` | Start the portal development server |
| `mise run req:update` | Recompile Linux requirements locks |
| `mise run test:data-model` | Test the data models |
| `mise run test:sdk` | Test the SDK |
| `mise run test:cluster-manager` | Test cluster-manager |
| `mise run test:scheduler` | Test the scheduler |
| `mise run test:virtual-desktop-controller` | Test virtual desktops |
| `mise run test:web-portal` | Install portal dependencies and run Vitest |
| `mise run test:all` | Run all six suites, reporting any failure |
| `mise run ideactl:build` | Build ideactl |
| `mise run ideactl:checks` | Run ideactl checks |
| `mise run lint` | Run hk checks |
| `mise run fix` | Apply hk fixes for review |

Module names are `data-model`, `sdk`, `scheduler`, `cluster-manager`, and
`virtual-desktop-controller`; omit the name for all modules. Packaging applies to
the three services; `mise run package make-all-archive` gathers their archives.
The virtual-desktop-controller build also includes the connection gateway.

```bash
mise run clean
mise run web-portal:typings
mise run build
mise run package
```

Service tests retain `--keywords`, repeated `--params key=value`, `--capture-output`,
and `--cov-report`. Pass additional pytest arguments after `--`.
The portal task accepts Yarn test arguments.

Use `mise run req:update --name dev` for one lock, `--upgrade` for all dependencies,
or `--package-name NAME` for one dependency. `--check` recompiles in a temporary
directory and fails on any byte difference or orphan lock. The compiler remains
mise-pinned pip-tools to preserve the existing lock format; macOS uses the pinned
Python Linux container, so Docker must be running. `--container` forces the same
path on Linux. Renovate uses this task too. Install dependencies directly with
`uv pip install -r requirements/dev.txt` in the active virtual environment.

Invoke's unused remote workstation helpers, release-export wrappers and encoded
service CLI wrappers were removed. Use SSH/rsync directly for remote work and
`python -m <service>.cli.cli_main` for a service CLI. Administrator commands use
ideactl. The standalone AMI refresh scripts had no build, CI or documented command
callers and were removed; the shipped AMI maps and runtime refresh code remain.

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
