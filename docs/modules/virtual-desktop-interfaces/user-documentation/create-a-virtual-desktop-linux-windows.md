---
description: Create a powerful Linux or Windows virtual desktop in a single click
---

# Create a virtual desktop (Linux/Windows)

To access the Virtual Desktop section, click "**Virtual Desktops**" on the left sidebar:

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-21 at 9.54.11 PM.png" alt=""><figcaption></figcaption></figure>

To launch your virtual desktop click "**Launch new Virtual Desktop"** button. You will be prompted with a new modal asking you a couple of questions:

* **Session Name**: A name for your desktop
* **Project**: The project your session will get created. Refer to "Projects Management" section under [Cluster Manager](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/GtBrWw9T1qCJK2QCOTW2/ "mention") to learn more about projects.
* **Operating System**: The operating system you want to use from:
  * **Linux**
    * Amazon Linux 2
    * CentOS 7
    * Red Hat Enterprise Linux 7
  * **Windows**:
    * Windows Server 2019
* **Software Stack**: A software stack is an EC2 AMI with pre-installed and pre-configured applications defined by your cluster administrator. Refer to [virtual-desktop-images-software-stacks.md](../admin-documentation/virtual-desktop-images-software-stacks.md "mention") to learn how to create custom software stack for your team.
* **Hibernation**: Select whether or not you want to enable hibernation for your session. You must be verify if your EC2 instance supports hibernation first
* **Virtual Desktop Size:** Instance type to provision. Select of EC2 instance types is based on the list of instances safe-listed by your cluster administrator and the project selected. You can change this value later without having to re-create a new desktop (see [modify-a-virtual-desktop.md](modify-a-virtual-desktop.md "mention"))
* **Storage Size**: Size of the main EBS partition
* **Advanced Options**: Advanced option such as enforcing a subnet ID or choose the DCV sessions type

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 10.52.44 AM.png" alt=""><figcaption></figcaption></figure>

Click "**Submit**" button to launch your virtual desktop creation. You will instantly see a new card with your desktop information. Your virtual desktop will be ready within 10-15 minutes. Startup time is based on the image selected, the operating system as well as the instance type.

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 10.53.56 AM.png" alt=""><figcaption><p>Desktop being started</p></figcaption></figure>

Wait a couple of minutes until your desktop is ready.

{% hint style="success" %}
IDEA automatically detects GPU instances and install the relevant drivers (NVIDIA GRID, NVIDIA Tesla, AMD) automatically
{% endhint %}

## How to access your Windows or Linux desktop

Once your virtual desktop is up and running, you can click the card and connect it either via web or DCV client.&#x20;

<details>

<summary>Easiest: Access your desktop from within your web browser</summary>

Click "**Connect**" button or click the thumbnail to access your Windows or Linux desktop directly via your browser.

</details>

<details>

<summary>Best Performance: Use DCV Client </summary>

Click "**DCV Session File**" button to download your `.dcv` file. To open this file, you will need to have the DCV Client installed on your system. Click the "**?**" icon to access to the download link and installation instructions.

</details>

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 1.55.56 PM.png" alt=""><figcaption><p>Virtual Desktop ready to be used</p></figcaption></figure>

## Retrieve Session Information

Click **Actions** then **Show Infos** to retrieve your session information such as instance type, subnet id, operating system etc ...

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 2.07.20 PM.png" alt=""><figcaption><p>Get detailed information about your virtual desktop</p></figcaption></figure>
