# Groups management

To manage IDEA groups, navigate to the **"Cluster Management**" section on the left sidebar of IDEA menu and click "**Groups**"

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-23 at 9.46.37 AM.png" alt=""><figcaption><p>Groups management on IDEA</p></figcaption></figure>

### Group scopes

Groups can have multiple scopes:

* **User**: LDAP group associated to a given user. User has full privileges and can add/remove other users on his/her group. He/she can then use this LDAP group to manage folders permissions on their filesystem
* **Module**: LDAP group managing permissions for a given module. For example, users belonging to "vdc-administrators-module-group" have administrative privileges on [Virtual Desktop Interface (VDI)](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/QthiamUzKn8KJLl0hYBf/ "mention") module
* **Cluster**: Same as Module except this one apply to all modules installed
* **Project**: Group(s) associated to a given Project&#x20;

### Create new group&#x20;

1. Click "**Create Group**"
2. Enter a friendly name for this group
3. Specify the group name. This will the name you will reference on your filesystems/IDEA app.
4. Choose the group type (scope)

### Disable a group

1. Select the group you want to disable
2. Click "**Actions**" > "**Disable Group**"
