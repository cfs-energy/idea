## Projects

Manage IDEA projects. You can use projects to control ACLs, queue or budget.

### Create Project

Select your project and click **Action** > **Create Project** to create your project.

Choose a project name, description and select the IDEA queues applicable. Jobs using your project but submitted to another queue will be rejected.
You can control who can use this project by applying a LDAP group. If no groups are specified then everyone can submit a job with this project.

### Associate an AWS Budget

You can link your project to an existing AWS budget if needed. Jobs will be automatically rejected if the budget allocated via AWS Budget exceed its threshold.

### Update Tags

Select your project and click **Action** > **Update Tags** to update the AWS tags associated to your project. IDEA will automatically add these tags to all resources created to run jobs associated to this project.

### Edit Project

Select your project and click **Action** > **Edit Project** to update your project.

### Delete Project

Select your project and click **Action** > **Delete Project** to delete your project.
