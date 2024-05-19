## Users

Create and manage LDAP users of your IDEA cluster.

### Create a new user

Click **Create User** button to create a new LDAP user.

### Disable user

Select your user and click **Actions** > **Disable User** to disable the user account. This action prevent the user to access IDEA cluster.

### Grant Admin Permissions

Select a user and click **Actions** > **Set as Admin User** to grant IDEA Admin permissions. Admins have sudo permissions on all systems and full privileges on the web interface. SUDO permissions will take a couple of minutes to synchronize depending the size of your IDEA cluster.

>
**Note**: IDEA Admins cannot access the AWS account where IDEA is installed.

### Revoke Admin Permissions

Select a user with Admin permissions and click **Actions** > **Remove as Admin User** to revoke IDEA Admin permissions.

### Add user from group

Select your user and click **Actions** > **Add User to Group** to add user to the LDAP group.

### Remove user from group

Select your user and click **Actions** > **Remove User from Group** to remove group membership for this user.



