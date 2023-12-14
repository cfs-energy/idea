---
description: >-
  Update the hardware of your Windows or Linux virtual desktop on the fly or
  simply assign a new session name
---

# Modify a virtual desktop

First, you must stop your current desktop before being able to upgrade/downgrade the instance type associated to your desktop. To do so, click **** "**Actions**" **** > **** "**Virtual Desktop State**" > "**Stop**" **** your virtual desktop.

{% hint style="info" %}
Stopping a desktop will not cause any data loss.
{% endhint %}

Wait until the state of your desktop is **Stopped**. You can verify that by checking the virtual desktop settings bar:

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 2.12.25 PM.png" alt=""><figcaption><p>Verify the virtual desktop state by checking the settings bar</p></figcaption></figure>

Once you confirmed your virtual desktop is stopped, click **** "**Actions**" **** > **** "**Update Session**"

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 2.14.15 PM.png" alt=""><figcaption><p>Update Session will let you change the EC2 hardware and session name</p></figcaption></figure>

You will have the possibility to update the session name or the virtual desktop size. To upgrade/downgrade, simply select the new instance type you want to use (in this example, we will upgrade from g4dn.xlarge to g4dn.2xlarge)

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 2.15.21 PM.png" alt=""><figcaption><p>You can change the size of your virtual desktop or its session name</p></figcaption></figure>

{% hint style="info" %}
&#x20;You can verify what instance type is used by your virtual desktop by checking the desktop setting bar

![](<../.gitbook/assets/Screen Shot 2022-10-25 at 2.17.32 PM (2).png>)
{% endhint %}

Once your instance is updated, restart your desktop by clicking "**Actions**" > **** "**Virtual Desktop State**" **** > "**Start**"**.** You virtual desktop will now use the updated EC2 hardware.

##
