# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Development setup

Install [mise](https://mise.jdx.dev/getting-started.html) and activate it in your shell
(for example, `eval "$(mise activate zsh)"` for zsh). From the checkout:

```bash
mise trust
mise install
hk install
hk check --all
```

`mise.toml` pins the developer and CI tools. `hk install` installs the Git commit hook.
It applies available fixes, stops the commit, and leaves those fixes for you to review and
stage. Use `hk fix --all` to apply fixes manually, and `hk check --all` (or `mise run lint`)
for a read-only check. To run without shell activation, use `mise exec -- hk check --all`.
The branch guard rejects local commits to main and master; CI skips only that guard.

Library dependencies still come from `requirements/*.txt`, `package-lock.json`, and
`yarn.lock`. Install them as described in the
[developer onboarding guide](docs/developer-portal/developer-onboarding.md).
Use `mise run test:sdk` (or `test:cluster-manager`, `test:scheduler`,
`test:virtual-desktop-controller`, `test:web-portal`) after installing the development
requirements. The legacy module suites use Corretto 17: run `MISE_ENV=legacy mise install`
and `MISE_ENV=legacy mise run test:sdk`. ideactl uses `mise run ideactl:build` and
`mise run ideactl:checks`; install its dependencies with `npm ci` first.
Keep the development venv's `bin` directory first on `PATH` inside the ideactl test
process too: its Python oracles import Jinja2 and PyYAML from the dev requirements.

The web portal uses Yarn: run `yarn install --frozen-lockfile` in
`source/idea/idea-cluster-manager/webapp` before its tests. `npm ci` requires an npm
lockfile and cannot install the portal's dependencies.

On macOS, the Python bootstrap tests execute Linux shell fragments and need GNU sed
and GNU date on `PATH`. With Homebrew's `gnu-sed` and `coreutils` installed:

```bash
export PATH="$(brew --prefix gnu-sed)/libexec/gnubin:$(brew --prefix coreutils)/libexec/gnubin:$PATH"
```

Run the Python suites with the mise Java environment active and local socket access:
cluster-manager uses DynamoDB Local, and SDK and terminal tests start local listeners.
Cluster-manager fails if DynamoDB Local cannot start, so its dependent tests cannot
silently disappear from a passing run. Inspect pytest's skip summary (`-ra`) for
platform prerequisites such as root access or PowerShell.

Tool updates belong in mise configuration; Python and Node dependency updates belong in
the existing manifests and lockfiles. `software_versions.yml` still selects deployed
runtimes. Keep its Node and Python versions aligned with mise when changing those runtimes.

Build and maintenance commands are listed in the
[development command table](docs/developer-portal/developer-onboarding.md#development-commands).
Use `mise run clean`, `mise run build`, and `mise run package` in that order.
Refresh requirements with `mise run req:update`; verify a no-op compile with
`mise run req:update --check`. Activate the development venv first so service tasks
use its interpreter through `VIRTUAL_ENV`.

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check [existing open](https://github.com/cfs-energy/idea/issues), or [recently closed](https://github.com/cfs-energy/idea/issues?utf8=%E2%9C%93&q=is%3Aissue%20is%3Aclosed%20), issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels ((enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any ['help wanted'](https://github.com/cfs-energy/idea/labels/help%20wanted) issues is a great place to start.


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project, report it privately through the repository's [Security tab](https://github.com/cfs-energy/idea/security/advisories/new). Please do **not** create a public GitHub issue.


## Licensing

See the [LICENSE](https://github.com/cfs-energy/idea/blob/main/LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.

We may ask you to sign a [Contributor License Agreement (CLA)](http://en.wikipedia.org/wiki/Contributor_License_Agreement) for larger changes.
