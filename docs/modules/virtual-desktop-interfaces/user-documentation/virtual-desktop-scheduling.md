---
description: How to change the schedule of your Windows or Linux desktop
---

# Virtual desktop scheduling

By default, your virtual desktop comes with the Stop On Idle schedule. This will stop/hibernate your virtual desktop only when the CPU use is below the cluster set utilization threshold. Default is 30%. AND when no one has logged in for at least `virtual-desktop-controller.dcv_session.idle_autostop_delay`. Default is 60 minutes.

{% hint style="info" %}
Virtual Desktop will only be stopped if idle (e.g: no active session connected within the Idle Timeout period and CPU usage below 30%). This is meant to prevent accidental stop and ensure you won't have to worry if you have a simulation running on your desktop overnight but have configured auto-stop after 8PM
{% endhint %}

You can change this behavior by configuring your own scheduling, and IDEA will ensure your desktop will automatically start/stop based on your own requirements.

There are a number of schedules to choose from. Please review the table below.

<table><thead><tr><th width="186">Mode</th><th width="222">Running Desktop</th><th>Stopped Desktop</th></tr></thead><tbody><tr><td>No Schedule</td><td>Stay running until you stop/terminate</td><td>Stay stopped until you manually restart it</td></tr><tr><td>Stop On Idle</td><td>Will be stopped if idle after AutoStop Idle timeout &#x26; CPU use is less than Utilization Threshold</td><td>Will stay stopped</td></tr><tr><td>Started All Day</td><td>Will stay running</td><td>Will be automatically started after 00H</td></tr><tr><td>Working Hours</td><td>Will be started at 9 AM</td><td>Will be stopped if idle after 5 PM</td></tr><tr><td>Custom Schedule</td><td>Will be started based on your own time</td><td>Will be stopped if idle based on your own time</td></tr></tbody></table>

Simply click the dropdown menu to chose your schedule for that day using the different presets below:

You can at any moment review whether or not you have a schedule configured for the current day on your virtual desktop by checking the settings bar of your session (note: schedule are unique to each desktop)

<figure><img src="../../../.gitbook/assets/mods_vdi_user_schedule_verify.webp" alt=""><figcaption><p>Verify if a schedule is applicable by checking the settings bar</p></figcaption></figure>

To create/edit a schedule, click "**Actions**" > "**Schedule**". This will open a new modal where you will be able to choose the schedule for any given day:

<figure><img src="../../../.gitbook/assets/mods_vdi_user_schedule_custom.webp" alt=""><figcaption><p>Set a custom schedule per day</p></figcaption></figure>

{% hint style="info" %}
Schedule is re-evaluated every 30 minutes
{% endhint %}

If your administrator allows it, the same modal also shows a **Stop On Idle after (minutes)** field above the weekly schedule. This overrides `virtual-desktop-controller.dcv_session.idle_autostop_delay` for this desktop only; set it to `0` to fall back to that cluster default. The value you can request is capped by `virtual-desktop-controller.dcv_session.idle_autostop_delay_max`, 240 minutes out of the box, and lowering that cap later also clamps any override you already saved. A cap of `0` turns per-session overrides off, and the field does not appear.
