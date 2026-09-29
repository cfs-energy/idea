## Images and applications

### Desktop images

Manage your Windows and Linux software stack. A stack is an EC2 AMI with pre-configured applications.

### Create a new software stack

Click **Create Software Stack** to create a new software stack for your virtual desktops users.

>
**Important**: Creating a software stack will create an Amazon Machine Image of a selected EC2 instance ID. Prior to do that, verify you do not have any personal information stored on the machine.

The page lists every supported operating-system and architecture combination even if its base stack is missing or disabled. Controller startup restores missing base records.

The default desktop family list includes `g7`; both `g7` and `g7e` have NVIDIA driver mappings. `g7e` still needs an allow-list entry or user exception, and the deny list always wins.

Red Hat Enterprise Linux and Rocky Linux image builds boot the kernel installed during bootstrap. If a desktop returns on the old kernel, bootstrap records the mismatch and exits immediately without another retry. The timeout sweep later marks the session **ERROR** and terminates the host.
