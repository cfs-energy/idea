# Users Management

To manage IDEA users, navigate to "**Administration**" > "**People and access**" > "**Users**"

<figure><img src="../../.gitbook/assets/mods_cm_users_portal.webp" alt=""><figcaption><p>Users portal</p></figcaption></figure>

### Create a new user

1. Click "**Create User**"
2. Specify the username
3. Specify the email address
4. Select whether or not the email is verified.
   1. **NO** (default & recommended): A temporary password will be send to the email and user will need to change it at first login
   2. **YES**: You will be asked to manually set up a password for the user
5. Choose the permissions applicable for the user. To learn more about the different permissions, refer to [groups-management.md](groups-management.md "mention")
6. Choose the login shell

If "**Email is Verified**" is not checked during account creation, the user will receive a welcome email message with a temporary password. User will be required to change this password after first successful login.

<figure><img src="../../.gitbook/assets/mods_cm_users_invite.webp" alt=""><figcaption><p>Example of invitation email</p></figcaption></figure>

### Understanding Confirmation Status

Confirmed: User can log in to IDEA

Force Change Password: User will be required to change his/her password after the next successful login

### Enable a user

* Select a user with Status set to "Disabled"
* Click "**Actions**" then "**Enable User**"

### Disable a user

* Select a user with Status set to "Enabled"
* Click "**Actions**" then "**Disable User**"

Disabling a user renames `~/.ssh/authorized_keys` to `authorized_keys.disabled`, so key sign-in to the bastion stops even where the directory keeps the account enabled. Every key in the file is set aside, including keys added by hand. Enabling the user renames the file back. If the user was given keys while disabled, those stay in `authorized_keys` and the old file stays aside. A key file that is a link or has a second name is renamed aside the same way, without following it, and an empty read-only file takes its place. A `.ssh` folder that is a link is set aside as a whole. `authorized_keys2` is handled like `authorized_keys`. At every start, the cluster manager also sets aside the keys of each disabled user, in the background.

Signing a user out everywhere, resetting their password or disabling them also ends any browser terminal opened with an earlier sign-in, within 15 seconds, and no new terminal opens from it. API tokens created before a user's sessions were ended stop working.

### Delete a user

Deleting a user removes the account from IDEA and the user pool. On a directory IDEA writes, it also removes the directory account, and the user's home folder is renamed aside so a later account with the same name starts with a new home and key:

* `/data/home/<username>` is renamed to `/data/home/<username>_<timestamp>`, owned by root with mode 700.
* A home that is a link, or that is its own mount, is left where it is and the cluster manager logs a warning.
* On a read-only directory the account stays in the directory, and its home is left in place.

To give the files back, an administrator renames the folder and sets its owner on a host that mounts `/data`, for example `sudo mv /data/home/<username>_<timestamp> /data/home/<username>` and `sudo chown -R <username>: /data/home/<username>` once the account exists again. To remove them, delete the renamed folder.

### Give user Cluster Admin privileges

* Select a standard user
* Click "**Actions**" then "**Set as Admin user**"

### Remove Cluster Admin privileges for a user

* Select a user with admin privileges
* Click "**Actions**" then "**Remove as Admin user**"

### Add user to group

* Select the user
* Click "**Actions**" then "**Add User to Group**"
*   Choose the group from the list

    <figure><img src="../../.gitbook/assets/mods_cm_users_groupsel.webp" alt=""><figcaption><p>Example of group selection</p></figcaption></figure>

### Remove user from group

* Select the user
* Click "**Actions**" then "**Remove User From Group**"
* Select the group(s) you want your user to be removed from

### Reset password for a user

* Select the user
* Click "**Actions**" then "**Reset Password**"

{% hint style="info" %}
You cannot reset the password of a user if his/her confirmation status is not "Confirmed"
{% endhint %}

### Review background task failures

The **Status** column shows an error indicator when a terminal background task fails for a user. Read the task name, failure message, and time, correct the cause, then repeat the action. The indicator clears when the same task succeeds.

### Grant personal desktop instance types

Select a user, then choose **Actions > Set virtual desktop instance types**. Select the extra instance types this user may launch without changing the global or software-stack allow list. The global deny list still takes precedence. Save an empty selection to remove all personal exceptions.

### Add a batch of users

Login to the cluster manager and run `ideactl accounts batch-create-users` to create multiple users in a single command. Users information (username/email ...) must be specified via a .csv file

### ideactl accounts

IDEA provides `ideactl` utility in case you cannot access the web interface but needs to interact with users. To get started, log in to the **Cluster Manager** EC2 machine and run `ideactl accounts`

```
ideactl accounts
Usage: ideactl accounts [OPTIONS] COMMAND [ARGS]...

  account management options

Options:
  --help  Show this message and exit.

Commands:
  batch-create-users  creates users from csv file
  create-user         create new user account
  delete-user         delete user
  disable-user        disable user
  enable-user         enable user
  get-user            get user
  list-users          list existing users
  modify-user         update an existing user account
```

For example, here is how to create a new user, setting a temp password and giving this user admin permission

```bash
# ideactl accounts create-user --email "sampleuser@example.com" --password "Password123@" --username "sampleuser2" --sudo --email-verified
{
  "username": "sampleuser2",
  "email": "sampleuser@example.com",
  "uid": 5068,
  "gid": 5077,
  "group_name": "sampleuser2-user-group",
  "login_shell": "/bin/bash",
  "home_dir": "/data/home/sampleuser2",
  "sudo": true,
  "status": "CONFIRMED",
  "enabled": true,
  "created_on": "2022-12-21T16:37:32.033000+00:00",
  "updated_on": "2022-12-21T16:37:32.033000+00:00"
}
```
