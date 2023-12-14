# FAQ

## Cluster Management

<details>

<summary>How do I update Boto3</summary>

Boto3 ([https://boto3.amazonaws.com/v1/documentation/api/latest/index.html](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)) is the official AWS Python SDK. We recommend to update boto3 on a regular basis in order to stay up-to-date with the latest AWS releases (new instance types ...)

To update Boto3, run the following patch command:

```
./idea-admin.sh patch scheduler \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION> \
  --force \
  --patch-command 'sudo idea_pip install boto3 --upgrade && sudo supervisorctl restart all'
```

Refer to [patch-idea-module.md](../first-time-users/cluster-operations/update-idea-cluster/patch-idea-module.md "mention") to learn more about the patch utility

</details>

<details>

<summary>How do I safe-list a new IP to access my IDEA environment</summary>

To safelist a new IP, navigate to VPC > Managed Prefix List and add your new entry into the Prefix List created by IDEA.

Alternatively, you can run the following `idea-admin.sh` command:

```
./idea-admin.sh utils cluster-prefix-list add-entry
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION> \
  --cidr x.x.x.x/x \ 
  --description '<DESCRIPTION>'
```

</details>

<details>

<summary>I never received the welcome email after installing IDEA, how can I create an admin user?</summary>

Use `ideactl` If you cannot receive email from Cognito due to IT restriction. Login to the Cluster Manager EC2 instance and run `ideactl accounts create-user`

<pre><code><strong># Make sure to run this command as root on the CLUSTER Manager
</strong><strong># ideactl accounts create-user --email "mcrozes@myemail.com" --password "Password123@" --username "mcrozes2" --sudo --email-verified
</strong>{
  "username": "mcrozes2",
  "email": "mcrozes@myemail.com",
  "uid": 5068,
  "gid": 5077,
  "group_name": "mcrozes2-user-group",
  "login_shell": "/bin/bash",
  "home_dir": "/data/home/mcrozes2",
  "sudo": true,
  "status": "CONFIRMED",
  "enabled": true,
  "created_on": "2022-12-21T16:37:32.033000+00:00",
  "updated_on": "2022-12-21T16:37:32.033000+00:00"
}
</code></pre>

If you cannot use SSM, you can use `idea-admin.sh` . Run the following commands to create a new admin user via IDEA APIs

<pre class="language-bash"><code class="lang-bash">IDEA_ADMIN_USER="username"
IDEA_ADMIN_USER_PASSWORD="password"
IDEA_USER_EMAIL_ADDRESS="email_address"
IDEA_CLUSTER_NAME="idea-xxx"
IDEA_DEPLOYMENT_REGION="region where you deployed IDEA"

# Retrieve Client ID
CLIENT_ID_ARN=$(./idea-admin.sh config show \
--query "cluster-manager.client_id" \
--cluster-name $IDEA_CLUSTER_NAME \
--aws-region $IDEA_DEPLOYMENT_REGION \
--format raw)

CLIENT_ID=$(aws secretsmanager get-secret-value --secret-id $CLIENT_ID_ARN --query "SecretString" --output text --region $IDEA_DEPLOYMENT_REGION)# Retrieve Client Secret

# Retrieve Client secret
CLIENT_SECRET_ARN=$(./idea-admin.sh config show \
--query "cluster-manager.client_secret" \
--cluster-name $IDEA_CLUSTER_NAME \
--aws-region $IDEA_DEPLOYMENT_REGION \
--format raw)

CLIENT_SECRET=$(aws secretsmanager get-secret-value --secret-id $CLIENT_SECRET_ARN --query "SecretString" --output text --region $IDEA_DEPLOYMENT_REGION)

# Retrieve Cognito URL
COGNITO_USER_POOL=$(./idea-admin.sh config show \
--query "identity-provider.cognito.domain_url" \
--cluster-name $IDEA_CLUSTER_NAME \
--aws-region $IDEA_DEPLOYMENT_REGION \
--format raw)

# Retrieve ALB endpoint
IDEA_ALB=$(./idea-admin.sh config show \
--query "cluster.load_balancers.external_alb.load_balancer_dns_name" \
--cluster-name $IDEA_CLUSTER_NAME \
--aws-region $IDEA_DEPLOYMENT_REGION \
--format raw)

# Generate Authorization Header (remove -w 0 if using Mac)
AUTHORIZATION_HEADER=$(echo -n $CLIENT_ID:$CLIENT_SECRET | base64 -w 0)
<strong>
</strong><strong># Request Bearer
</strong>curl --silent --insecure --location --request POST "$COGNITO_USER_POOL/oauth2/token" \
--header "Authorization: Basic $AUTHORIZATION_HEADER" \
--header "Content-Type: application/x-www-form-urlencoded" \
--data-urlencode "grant_type=client_credentials" \
--data-urlencode "scope=cluster-manager/read cluster-manager/write" > .bearer

# Bearer output is stored as text file in order to use -r. File is removed shortly after
BEARER=$(cat .bearer | jq -r ".access_token")

rm -rf .bearer
<strong>
</strong><strong># Create Admin User
</strong>curl --silent --insecure --location --request POST "https://$IDEA_ALB/cluster-manager/api/v1" \
--header "Authorization: Bearer $BEARER" \
--header "Content-Type: application/json" \
--data-raw '{
"header": {
"namespace": "Accounts.CreateUser"
},
"payload": {
"user": {
"username": "'$IDEA_ADMIN_USER'",
"password": "'$IDEA_ADMIN_USER_PASSWORD'",
"email": "'$IDEA_USER_EMAIL_ADDRESS'",
"additional_groups": ["managers-cluster-group", "administrators-cluster-group]
},
"email_verified": true
}
}'
</code></pre>

</details>

<details>

<summary>How do I patch/update/change the configuration  an IDEA module</summary>

See [update-idea-cluster](../first-time-users/cluster-operations/update-idea-cluster/ "mention")

</details>

<details>

<summary>How do I uninstall IDEA?</summary>

See [uninstall-idea.md](../first-time-users/cluster-operations/uninstall-idea.md "mention")

</details>

<details>

<summary>How do I resume a failed IDEA installation</summary>

See [#how-do-i-resume-a-failed-installation](faq.md#how-do-i-resume-a-failed-installation "mention")

</details>

<details>

<summary>How to customize the logo/title or subtitle of my IDEA environment</summary>

The logo, title and subtitle of the Web Portal can be customized using configurations.

![](https://confluence.amazon.com/download/attachments/108564578/Screen%20Shot%202022-07-11%20at%207.49.14%20AM.png?version=2\&modificationDate=1657551271000\&api=v2)

### Defaults <a href="#customizelogo-titleandsubtitle-defaults" id="customizelogo-titleandsubtitle-defaults"></a>

* title - Integrated Digital Engineering on AWS (IDEA)
* logo - IDEA Default Logo
* subtitle - \<cluster-name> (\<aws-region>)

### Customization <a href="#customizelogo-titleandsubtitle-customization" id="customizelogo-titleandsubtitle-customization"></a>

#### Logo <a href="#customizelogo-titleandsubtitle-logo" id="customizelogo-titleandsubtitle-logo"></a>

Logo can be customized by uploading appropriate logo file to the cluster's S3 Bucket. Copy the S3 object key and run the below command:

```bash
./idea-admin.sh config \
set Key=cluster-manager.web_portal.logo,Type=string,Value=assets/logo.png \
--cluster-name <CLUSTER_NAME> \
--aws-region <REGION>
```

#### Title <a href="#customizelogo-titleandsubtitle-title" id="customizelogo-titleandsubtitle-title"></a>

Title can be customized by running the below command:

```bash
./idea-admin.sh config \ 
  set "Key=cluster-manager.web_portal.title,Type=string,Value=My Company" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

#### Subtitle <a href="#customizelogo-titleandsubtitle-subtitle" id="customizelogo-titleandsubtitle-subtitle"></a>

Subtitle can be customized by running the below command:

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.web_portal.subtitle,Type=string,Value=R&D Cluster" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>


```

</details>

<details>

<summary>How do I configure automatic mount for additional File-system (FSx Lustre/OnTAP/OpenZFS/Windows, EFS)</summary>

See [Shared Storage](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/5SSt4opQQGbm5tAfuEqy/ "mention") module

</details>

<details>

<summary>How do I automatically add new tags during the installation?</summary>

Update the last section of idea/idea-administrator/resources/config/templates/global-settings/settings.yml

```
# provide custom tags for all resources created by IDEA
# for eg. to add custom tags, tags as below:
# custom_tags:
#   - Key=custom:MyTagName,Value=MyTagValue
#   - Key=AnotherExampleName,Value=Another Example Value
custom_tags: []
```

</details>

<details>

<summary>How to automatically add IAM Managed Policies to existing IDEA IAM roles</summary>

Add the managed policy ARN in cluster settings: source/idea-administrator/resources/config/templates/cluster/settings.yml&#x20;

All roles will contain the policy(ies) you have added to the list.

</details>

<details>

<summary><strong>I am using an existing VPC and scheduler module is not working (not able to query the internal DNS)</strong></summary>

IDEA create a route53 private hosted zone.&#x20;

If you try to curl any DNS from this Route53 Zone,and get no result, even though the Private Zone is assigned to the VPC

```
# nslookup 
internal-alb.idea-demo.us
-east-2.local
Server: 10.110.0.2
Address: 10.110.0.2#53
** server can't find 
internal-alb.idea-demo.us
-east-2.local: NXDOMAIN
```

To fix this, enable DNS hostname/resolution on your VPC

</details>

## IDEA Logs

<details>

<summary>Where are the application logs stored?</summary>

IDEA modules such as cluster-manager, virtual-desktop-controller and scheduler run a python based application server.

The application server logs are available under: **/opt/idea/app/logs**

All logs will be available in **application.log**. In rare occasions, few logs may be available under **stdout.log**.

Logging can configured per application server using IDEA Cluster Configuration. Below is the logging configuration for cluster-manager:

```
./idea-admin.sh config show \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION> \
  --query "cluster-manager.logging.*"
+-----------------------------------------------+--------------------+---------+
| Key                                           | Value              | Version |
+-----------------------------------------------+--------------------+---------+
| cluster-manager.logging.default_log_file_name | application.log    |    1    |
| cluster-manager.logging.logs_directory        | /opt/idea/app/logs |    1    |
| cluster-manager.logging.profile               | production         |    1    |
+-----------------------------------------------+--------------------+---------+
```

</details>

<details>

<summary>How to debug a module not starting correctly</summary>

1 - Log in to the EC2 machine and check the logs under **/root/boostrap/logs.**

Try to find some potential issue(s) by looking for keywords like: &#x20;

* error
* fatal
* denied
* permission

All infrastructure nodes such as directoryservice (openldap-server), scheduler, bastion-host, virtual-desktop controller use a standard directory structure during bootstrap.

2 - Check if supervisord is running correctly (/opt/idea/python/latest/bin/supervisorctl status), if not check /var/log/supervisord.log

3 - Depending your module, you can also check the app log via /opt/idea/app/logs

Make sure to run supervisorctl restart all after making any changes

</details>

## Scale-Out Workloads Jobs

<details>

<summary>My job is not starting, how can I check the bootstrap/setup logs</summary>

If your job is not starting, you can verify if the provisioned capacity is configured correctly by checking the bootstrap logs under `/apps/<CLUSTER>/scheduler/jobs`

Logs structure:

* jobs/
  * \<job\_id>/
    * \<job\_type> (bootstrap or compute\_node setup)
      * \<EC2 Host>

Example: `/apps/idea-demo/scheduler/jobs/98/logs/ip-10-110-4-189`

</details>

<details>

<summary>How do I add additional logic to be executed on my compute nodes?</summary>

Edit `/apps/<CLUSTER>/scheduler/compute_node/userdata_customizations.sh` if you want to add your own code to the compute node(s). Script is executed at the very end of the bootstrap sequence.

</details>
