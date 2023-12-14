---
description: >-
  Manage your virtual desktop lifecycle easily via Virtual Desktop State
  section.
---

# Stop/Delete/Hibernate a virtual desktop

To access the Virtual Desktop lifecycle section, click "**Actions**" button associated to the virtual desktop:

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 2.00.17 PM.png" alt=""><figcaption><p>Control your virtual desktop lifecycle</p></figcaption></figure>

### Stop

Click "**Action**" > "**Virtual Desktop State**" > "**Stop**" to stop your current virtual desktop session. Stopped session will not suffer any data loss and you can restart a stopped session at any moment.

### Terminate

Click "**Action**" > "**Virtual Desktop State**" > "**Terminate**" to permanently terminate a virtual desktop  session.&#x20;

{% hint style="warning" %}
Terminating a session may cause data loss if you are using ephemeral storage, so make sure to have uploaded all your data back to IDEA filesystem first.
{% endhint %}

### Hibernate

{% hint style="info" %}
When you hibernate an instance, your desktop state is saved in memory. When you restart it, all your applications will automatically resume. On the other hand, stopping a virtual desktop is the same as powering off your laptop. Please not all EC2 instances support hibernation, verify if your instance supports it by checking [https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/hibernating-prerequisites.html](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/hibernating-prerequisites.html)
{% endhint %}

Click "**Action**" > "**Virtual Desktop State**" > "**Hibernate**" to hibernate your current virtual desktop session. Hibernated session will not suffer any data loss and you can restart the session at any moment.&#x20;

##
