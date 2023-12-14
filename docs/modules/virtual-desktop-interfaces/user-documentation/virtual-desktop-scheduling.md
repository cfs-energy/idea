---
description: How to change the schedule of your Windows or Linux desktop
---

# Virtual desktop scheduling



By default, your virtual desktop come with no schedule, which means your desktop will stay up until you stop/terminate it, and will stay stopped until you turn it back on. You can change this behavior by configuring your own scheduling, and IDEA will ensure your desktop will automatically start/stop based on your own requirements.

{% hint style="info" %}
Virtual Desktop will only be stopped if idle (e.g: no active session connected and CPU usage below 15% for at least 15 minutes). This is meant to prevent accidental stop and ensure you won't have to worry if you have a simulation running on your desktop overnight but have configured auto-stop after 8PM
{% endhint %}

You can at any moment review whether or not you have a schedule configured for the current day on your virtual desktop by checking the settings bar of your session (note: schedule are unique to each desktop)

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 3.25.15 PM.png" alt=""><figcaption><p>Verify if a schedule is applicable by checking the settings bar</p></figcaption></figure>

To create/edit a schedule, click "**Actions**" > **** "**Schedule**". **** This will open a new modal where you will be able to choose the schedule for any given day:

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-25 at 2.31.55 PM.png" alt=""><figcaption><p>Set a custom schedule per day</p></figcaption></figure>

Simply click the dropdown menu to chose your schedule for that day using the different presets below:

| Mode            | Running Desktop                        | Stopped Desktop                                |
| --------------- | -------------------------------------- | ---------------------------------------------- |
| No Schedule     | Stay running until you stop/terminate  | Stay stopped until you manually restart it     |
| Stop All Day    | Will be stopped if idle after 00H      | Will stay stopped                              |
| Started All Day | Will stay running                      | Will be automatically started after 00H        |
| Working Hours   | Will be started at 9 AM                | Will be stopped if idle after 5 PM             |
| Custom Schedule | Will be started based on your own time | Will be stopped if idle based on your own time |

{% hint style="info" %}
Schedule is re-evaluated every 30 minutes
{% endhint %}
