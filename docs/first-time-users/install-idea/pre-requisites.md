# Pre-Requisites

Installation of IDEA is automated via Docker. Make sure you have the following tools installed/configured on your system prior to launching the installation:

* [x] [#docker](pre-requisites.md#docker "mention")
* [x] [#aws-cliv2](pre-requisites.md#aws-cliv2 "mention")
* [x] [#create-the-iam-policies](pre-requisites.md#create-the-iam-policies "mention")
* [x] [#create-iam-user](pre-requisites.md#create-iam-user "mention") or [#create-iam-role](pre-requisites.md#create-iam-role "mention")
* [x] [#create-the-ssh-keypair](pre-requisites.md#create-the-ssh-keypair "mention")

## Docker

You must have Docker installed on your environment if you want to proceed to a standard 1-click installation. If needed, you can download and install Docker from the [official Docker website](https://docs.docker.com/get-docker/).

{% hint style="warning" %}
[Refer to this guide to avoid permissions issues](https://docs.docker.com/engine/install/linux-postinstall/) while running Docker as a non-root user.
{% endhint %}

{% hint style="info" %}
**Docker License:** Please ensure you or your organization adheres to the [Docker Subscription Service Agreement](https://www.docker.com/legal/docker-subscription-service-agreement/). Otherwise you must proceed to a manual installation.
{% endhint %}

### AWS CLIv2

If needed, install `awscli` v2 on your local environment by following the instructions from this link [https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)

### Create the IAM policies

These IAM policies contains all the permissions required to install/uninstall IDEA. This policy and be enabled/disabled at the user level if needed.

{% hint style="info" %}
Policy to uninstall IDEA is optional. If needed you can terminate your cluster from the AWS console using your own IAM user. The uninstall policy is only needed if you are planning to automatize cluster creation/termination
{% endhint %}

To create a policy:

1. Navigate to the IAM console page: [https://console.aws.amazon.com/iamv2/](https://console.aws.amazon.com/iamv2/)
2. Click **"Policies"** on the left sidebar
3. Click **"Create Policy"**
4. Select the JSON tab and copy/paste the content of [https://github.com/awslabs/scale-out-computing-on-aws/blob/main/installer/SOCAInstallerIamPolicy.json](https://github.com/awslabs/scale-out-computing-on-aws/blob/main/installer/SOCAInstallerIamPolicy.json). This file contains all the required permissions to install/uninstall IDEA.&#x20;
5. Click **"Next: Tags"** and add optional tags as needed
6. Click **"Next: Review"**, chose a name and a description
7. Click **"Create Policy"**
8. Repeat the steps3 to 7, but this time copy/paste the content of JSONUNINSTALL during step4

<figure><img src="../../.gitbook/assets/Screen Shot 2022-06-04 at 1.04.00 PM.png" alt=""><figcaption><p>Example of the two IAM policies (one for installation, one for termination)</p></figcaption></figure>

**Create your IAM user or role**

Should I configure an IAM user or role?

| Install from local laptop/workstation | Install from EC2                                                                                                                                                       |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| <ul><li>Use IAM user</li></ul>        | <ul><li>Use IAM role if you have the permission to create and attach it.</li><li>Use IAM user if you do not have the permission to create/attach an IAM role</li></ul> |

### Create IAM User

IAM user must have the permissions required to install IDEA.&#x20;

1. Navigate to the IAM console page: [https://console.aws.amazon.com/iamv2/](https://console.aws.amazon.com/iamv2/)
2. Click "**Users**" on the left sidebar
3. Click "**Add User**"
   1. Choose a username
   2. Under AWS Access Type check "**Access key - Programmatic access"**
4. Click **"Next: Add Permissions"**
5. Click **"Attach existing policies directly"** button and choose the IAM policy you just created on the previous step
6. Click **"Next: Add Tags"**.  Add any optional tags as needed
7. Click "**Next: Review"**
8. Click **"Create User"**
9. You will be prompted with your **AWS Access Key ID** and **AWS Secret Access Key.** Save them in a secure location as we will need them later. In case you lost them, you can generate a new pair them via IAM User > Security Credentials
10. Click **"Close"**

<figure><img src="../../.gitbook/assets/Screen Shot 2022-06-04 at 1.06.37 PM.png" alt=""><figcaption><p>Example of IAM user configured with the IAM policy</p></figcaption></figure>

**Configure your local IAM user**

Now you have created your IAM user, you must configure your local environment.&#x20;

{% hint style="warning" %}
Before configuring it, you must have AWSCLIv2 installed. To install it, refer to this guide: [https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
{% endhint %}

Once installed, run`aws configure` command and follow the prompts. Make sure to use the access/secret key created previously. If you do not have access to them, login to IAM, choose your IAM user, navigate to Security Credentials command and re-generate a new access/secret security pair

```bash
$ aws configure
AWS Access Key ID [None]: AKI<ACCESS_KEY_OF_YOUR_IAM>USER>
AWS Secret Access Key [None]: pd6<SECRET_KEY_OF_YOUR_IAM_USER>
Default region name [None]: us-west-2
Default output format [None]:
```

### Create IAM Role

* Navigate to the IAM console page: [https://console.aws.amazon.com/iamv2/](https://console.aws.amazon.com/iamv2/)
* Click "**Roles**" on the left sidebar
* Select "**AWS Service**" as Trusted Entity and "**EC2**" as Use Case
* Select the "**Permission Policies**" you created previously
* Review the "**Role details**" then click "**Create Role**"

Now your IAM role is created, go back to the EC2 console, select the EC2 instance you want to trigger the installation from and attach the newly created IAM role to it

### Create the SSH Keypair

This SSH key will be used to connect to the IDEA hosts as admin user. Keep it secure!

To create your SSH key:

1. Navigate to the EC2 console page: [https://console.aws.amazon.com/ec2/v2/](https://console.aws.amazon.com/ec2/v2/). Make sure to select the AWS region you want to use.
2. Click "**Key Pairs**" on the left sidebar under "Network & Security" section
3. Click "**Create Key pair**"
4. Pick a name, select RSA format and download it either as `.pem` if you are using Unix or `.ppk` via PuTTY on Windows. (note: you can always transform .pem to .ppk and vice-versa)
5. Click "**Create key pair**"

This will download the private key on your local system.  To be able to use the key, you must apply correct permissions by running `chmod 600 /path/to/your_pem_key.`
