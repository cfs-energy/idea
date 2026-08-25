---
description: Which base_os values work with which regions, hardware features and bootstrap steps
---

# Supported OS & hardware matrix

`base_os` selects a compute node's bootstrap path: package manager, EFA install, GPU driver, extra repos. Not every value supports every step, and not every value has an AMI in every region.

## Base operating systems

| `base_os` value   | Distribution                | Status                               |
| ----------------- | --------------------------- | ------------------------------------ |
| `amazonlinux2023` | Amazon Linux 2023           | Supported. Default for new installs. |
| `rhel8`           | Red Hat Enterprise Linux 8  | Supported.                           |
| `rhel9`           | Red Hat Enterprise Linux 9  | Supported.                           |
| `rhel10`          | Red Hat Enterprise Linux 10 | Supported. Compute nodes only.       |
| `rocky8`          | Rocky Linux 8               | Supported.                           |
| `rocky9`          | Rocky Linux 9               | Supported.                           |
| `rocky10`         | Rocky Linux 10              | Supported. Compute nodes only.       |
| `ubuntu2204`      | Ubuntu 22.04                | Accepted; no AMIs published.         |
| `ubuntu2404`      | Ubuntu 24.04                | Accepted; no AMIs published.         |

`amazonlinux2` is not an accepted value (EOL 2026-06-30). Existing virtual desktop software stacks and sessions on it stay readable and deletable; nothing new can be created on it.

Windows values (`windows2019`, `windows2022`, `windows2025`) are used only by virtual desktops and skip the bootstrap steps below.

### EL 10

`rhel10` and `rocky10` run compute nodes only. They are absent from the virtual desktop software stack config, so they do not appear in the virtual desktop OS list and no stack can be created on them.

The reason is Amazon DCV: it publishes no EL 10 build. `nice-dcv-el9-x86_64.tgz` and its aarch64 counterpart exist, `nice-dcv-el10-*` return 404 at every architecture. So the desktop bootstrap has no EL 10 branch to write, and a desktop on either would boot without DCV and never become usable. Compute nodes are unaffected, because they do not install DCV.

They also **require an EL 10.1 or 10.2 AMI**. See the FSx for Lustre note below.

### Ubuntu

`ubuntu2204` and `ubuntu2404` pass submission validation, but `region_ami_config.yml` carries no Ubuntu AMIs in any region, so a job using either must pass its own `instance_ami`. Their bootstrap path is complete, and EFA installs on both.

Ubuntu 26.04 is **not accepted**: `ubuntu2604` is absent from the values the scheduler allows, so a job requesting it is rejected at submission. Three dependencies publish nothing for it:

* **Amazon DCV**: no Ubuntu 26.04 package, so no virtual desktops.
* **FSx for Lustre**: no client; the repo publishes no `resolute` suite and no modules for the 7.0 kernel.
* **EFA**: the pinned installer (1.44.0) ships no `DEBS/UBUNTU2604`.

### Architecture

The AMI configuration accepts two shapes per region: a flat `<region>: <base_os>: <ami-id>` map, which is read as x86\_64 only, and a nested `<region>: <architecture>: <base_os>: <ami-id>` map covering `x86_64` and `arm64`. Mixing the two shapes inside one region is a configuration error.

The AMIs IDEA ships are entirely flat, so every published AMI is x86\_64. An arm64 or Graviton compute node needs a custom `instance_ami`, or an AMI configuration extended with the nested shape.

## Region AMI coverage

`region_ami_config.yml` maps each region to one AMI per `base_os` (ids resolved 2026-08-18). A region without an entry cannot launch that `base_os` unless the job passes a custom `instance_ami`.

* `amazonlinux2023`, `rhel8`, `rhel9`: all 29 cataloged regions.
* `rocky8`, `rocky9`: all except `us-gov-west-1`.
* `rhel10`, `rocky10`: 27 of 29. None in `me-south-1` or `us-gov-west-1`.
* `ubuntu2204`, `ubuntu2404`: **no AMIs in any region.** Both require a custom `instance_ami`.
* The GovCloud row, `us-gov-west-1`, is maintained separately and carries only `amazonlinux2023`, `rhel8` and `rhel9`.

{% hint style="warning" %}
Check `region_ami_config.yml` before depending on a `base_os` in a region. This page is a summary, not a live snapshot.
{% endhint %}

## EFA support

* `amazonlinux2023`, `rhel8`, `rhel9`, `rocky8`, `rocky9`: supported (RPM installer path).
* `rhel10`, `rocky10`: **not supported.** The pinned installer (1.44.0) recognizes no EL 10 family and exits, so a job requesting `efa_support=true` on either is rejected at submission rather than running over TCP on an EFA-billed instance.
* `ubuntu2204`, `ubuntu2404`: supported. The pinned installer ships their packages and the bootstrap has a deb install path.

EFA also requires an EFA-capable instance type ([AWS list](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html#efa-instance-types)). `base_os` support is necessary, not sufficient.

## FSx Lustre client support

The client install is gated on `base_os` and the running kernel series, because AWS publishes packages per point release:

* `amazonlinux2023`: from the distribution repos, with no kernel gate.
* `rhel8`/`rocky8`, `rhel9`/`rocky9`: from the AWS FSx client repo matching the running kernel. An unrecognized kernel series logs an error and skips the install.
* `rhel10`/`rocky10`: **require an EL 10.1 or 10.2 AMI.** EL 10.0 has no client; the bootstrap logs an error and Lustre is not mounted. The AMI refresh tooling pins EL10 lookups to 10.1 and 10.2 for this reason.
* `ubuntu2204`, `ubuntu2404`: install normally from the AWS FSx client repo.

An EL 10 client cannot mount a Lustre 2.10 or 2.12 filesystem. New scratch filesystems default to 2.15, but an imported pre-2.15 filesystem does not mount from these nodes.

A node that fails the client install still joins the scheduler as healthy, and the failure is visible only in the bootstrap log. Check here before pointing `fsx_lustre`-consuming queues at a new `base_os`.

## GPU driver support

Driver selection keys off the EC2 instance family, not `base_os`, with one AMD exception.

* **NVIDIA** (`p2`, `p3`, `p4d`, `p4de`, `p5`, `p5e`, `p5en`, `p6-b200`, `p6e-gb200`, `g2`, `g3`, `g3s`, `g4dn`, `g5`, `g5g`, `g6`, `g6e`, `g6f`, `gr6`): supported on every accepted `base_os`.
* **AMD** (`g4ad`): `rocky8` and `rocky9` only. Any other `base_os` fails the install rather than skipping silently.

A family outside both lists is not recognized as a GPU instance. The bootstrap logs a warning and skips the driver install regardless of `base_os`. If the node has GPU hardware anyway, `global-settings.gpu_settings.fail_on_missing_driver` decides whether the bootstrap aborts or continues.

## EPEL and extra-repo bootstrap

EPEL is installed only when `/etc/yum.repos.d/epel.repo` is absent. The CodeReady Builder, PowerTools and CRB repos hold the build dependencies several EPEL packages need, and are enabled with `dnf config-manager` after `dnf-plugins-core` is installed.

| `base_os`                  | EPEL                                     | Builder repo                                                     |
| -------------------------- | ---------------------------------------- | ---------------------------------------------------------------- |
| `amazonlinux2023`          | Not applicable                           | Not applicable                                                   |
| `rhel8`                    | `epel-release-latest-8` RPM from Fedora  | `dnf config-manager --set-enabled codeready-builder-for-rhel-8-rhui-rpms` |
| `rhel9`                    | `epel-release-latest-9` RPM from Fedora  | `dnf config-manager --set-enabled codeready-builder-for-rhel-9-rhui-rpms` |
| `rhel10`                   | `epel-release-latest-10` RPM from Fedora | `dnf config-manager --set-enabled codeready-builder-for-rhel-10-rhui-rpms` |
| `rocky8`                   | `epel-release` from Rocky's own repos    | `dnf config-manager --set-enabled powertools`                    |
| `rocky9`                   | `epel-release` from Rocky's own repos    | `dnf config-manager --set-enabled crb`                           |
| `rocky10`                  | `epel-release` from Rocky's own repos    | `dnf config-manager --set-enabled crb`                           |
| `ubuntu2204`, `ubuntu2404` | Not applicable                           | Not applicable. Ubuntu enables `universe` and `multiverse` instead. |

{% hint style="info" %}
Appearing in the base operating system table is necessary, not sufficient. A value also needs an AMI in your region and a bootstrap branch for the hardware your job asks for.
{% endhint %}
