# Raise a bug

Bugs and feature requests for IDEA are tracked on GitHub, and anyone can open one.

* **Report a bug:** [open a new issue](https://github.com/cfs-energy/idea/issues/new/choose) and choose **Bug report**.
* **Request a feature:** the same page, choose **Feature request**.
* **Ask a question:** [GitHub Discussions](https://github.com/cfs-energy/idea/discussions).

Search the [open issues](https://github.com/cfs-energy/idea/issues) first; the problem may already be known.

## What to include

* **The IDEA version:** the contents of `IDEA_VERSION.txt` in the release you deployed, or the `idea-administrator` image tag you ran (for example `26.09.0`).
* **Where it happened:** the web portal, the cluster manager, the scheduler, virtual desktops, the installer or an upgrade; the base OS of the host or desktop involved; the AWS region and partition.
* **Steps to reproduce**, what you expected, and what happened instead.
* **The exact error text:** the message on screen for the portal, the full command output for `idea-admin.sh`.
* **Logs from the module host** for the minutes around the failure: `/opt/idea/app/logs/application.log`. For an installer or upgrade failure, add the CloudFormation stack status and the failed resource's status reason.
* **Screenshots** for anything visual.
* Whether you changed IDEA from the published release, and if so what.

## What not to include

Issues are public. Remove credentials, tokens, account ids, private hostnames and IP addresses before pasting logs or screenshots.

## Security issues

Do not put exploit details in a public issue. Open an issue that says only that you have a security finding and how a maintainer can reach you, and a maintainer will follow up privately.
