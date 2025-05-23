# Developer Onboarding

## Pre-Requisites

* [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
* [Python3](https://www.python.org/downloads/) + [PyEnv](https://github.com/pyenv/pyenv)
* [nvm](https://github.com/nvm-sh/nvm)
* [yarn](https://yarnpkg.com/)
* [Git](https://git-scm.com/downloads)

{% hint style="warning" %}
#### Versions

Replace the variables in the code snippets below with the following values:

* PYTHON\_VERSION: 3.9.18
* NODEJS\_VERSION: 16.20.2
* CDK\_VERSION: 2.115.0
{% endhint %}

## Prepare environment

### Set Environment Variables for Versions

```
PYTHON_VERSION=<see above>
NODEJS_VERSION=<see above>
CDK_VERSION=<see above>
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

#### **AWS CDK \<CDK\_VERSION>**

{% hint style="danger" %}
**Note:** Do **NOT** install CDK globally using `npm -g` or `yarn global add`
{% endhint %}

Follow the instructions below:

```bash
mkdir -p ~/.idea/lib/idea-cdk && pushd ~/.idea/lib/idea-cdk
npm init --force --yes
npm install aws-cdk@$CDK_VERSION --save
popd
```

{% hint style="info" %}
If you want to **upgrade** CDK version for your existing IDEA dev environment, run:

```bash
invoke devtool.upgrade-cdk
```
{% endhint %}

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

<pre><code>Available tasks:
admin.main (admin)                   call administrator app main
build.administrator                  build administrator
build.all (build)                    build all
build.cluster-manager                build cluster manager
build.data-model                     build data-model
build.dcv-connection-gateway         build dcv connection gateway
build.scheduler                      build scheduler
build.sdk                            build sdk
build.virtual-desktop-controller     build virtual desktop controller
clean.administrator                  clean administrator
clean.all (clean)                    clean all components
clean.cluster-manager                clean cluster manager
clean.data-model                     clean data-model
clean.dcv-connection-gateway         clean dcv connection gateway
clean.scheduler                      clean scheduler
clean.sdk                            clean sdk
clean.virtual-desktop-controller     clean virtual desktop controller
devtool.build                        wrapper utility for invoke clean.&#x3C;module> build.&#x3C;module> package.&#x3C;module>
devtool.configure                    configure devtool
devtool.ssh                          ssh into the workstation
devtool.sync                         rsync local sources with remote development server
devtool.upload-packages              upload packages
docker.build                         build administrator docker image
docker.prepare-artifacts             copy administrator docker image artifacts to deployment dir
docker.print-commands                print docker push commands for ECR
package.administrator                package administrator
package.all (package)                package all components
package.cluster-manager              package cluster manager
package.dcv-connection-gateway       package dcv connection gateway
package.scheduler                    package scheduler
package.virtual-desktop-controller   package virtual desktop controller
release.prepare-opensource-package
release.update-version
req.install                          Install python requirements
req.update                           Update python requirements using pip-compile.
scheduler.cli (scheduler)            call scheduler cli
tests.run-integration-tests          Run Integration Tests
tests.run-unit-tests                 Run Unit Tests
web-portal.serve                     serve web-portal frontend app in web-browser
web-portal.typings                   convert idea python models to typescript







Clean, Build and Package
invoke clean build package
Run idea-admin.sh in Developer Mode
<strong>The IDEA_DEV_MODE environment variable is used to indicate if idea-admin.sh should use the Docker Image or Run from sources.
</strong>
If IDEA_DEV_MODE=true, idea-admin.sh will execute administrator app directly using sources.
If IDEA_DEV_MODE=false (default), idea-admin.sh will attempt to download the docker image for the latest release version and execute administrator app using Docker Container.

Export IDEA_DEV_MODE=true on your terminal, before executing idea-admin.sh on from project root.
# Enable Dev Modeexport IDEA_DEV_MODE=true
<strong>You will need to run export IDEA_DEV_MODE=true, each time you open a new Terminal session.
</strong><strong>Verify if Developer Mode is enabled
</strong><strong>To verify, if Developer Mode is enabled, run below command. This should print (Developer Mode) at the end of the banner.
</strong>| ./idea-admin.sh about'####:'########::'########::::'###::::. ##:: ##.... ##: ##.....::::'## ##:::: ##:: ##:::: ##: ######:::'##:::. ##:: ##:: ##:::: ##: ##...:::: #########:'####: ########:: ########: ##:::: ##:Integrated Digital Engineering on AWSVersion 3.0.0-beta.1(Developer Mode)
</code></pre>
