# Configure your OpenSearch

## Access your OpenSearch endpoint

Run `idea-admin.sh show-connection-info --aws-region <REGION> --cluster-name <NAME>` command to retrieve the "**Analytic Dashboard**" URL.

```bash
$ ./idea-admin.sh show-connection-info --aws-region us-east-2 --cluster-name idea-mcdev
Web Portal: xx
Bastion Host (SSH Access): xx
Bastion Host (Session Manager URL): xx
Analytics Dashboard: https://idea-mcdev-external-alb-391482262.us-east-2.elb.amazonaws.com/_dashboards

```

Alternatively, you can retrieve the OpenSearch dashboard endpoint via your IDEA Web Interface > "**Cluster** **Management**" > "**Settings**" then "**Analytics**".

## Initialize OpenSearch indexes (first time accessing OpenSearch)

Click the hamburger menu on the left to open the sidebar, then click "**Stack Managemen**t" under "**Management**" section.&#x20;

Click "**Index Patterns**" on the left sidebar then "**Create Index Pattern**". Refer to the tabs below to create indexes based on your own interest.

{% tabs %}
{% tab title="Virtual Desktop Index" %}
## **User Sessions**

**As Index Pattern Name**, specify your cluster name followed by "**\_vdc\_user\_sessions\***". For example, if your cluster name is "idea-test" then search for "idea-test\__vdc\__user\_sessions\*". Once done, click "**Next Step**".

Use "**created\_on**" as Time Field then click "**Create Index Pattern"**
{% endtab %}

{% tab title="Scale-Out Workloads Index" %}
## **Compute Nodes**

**As Index Pattern Name**, specify your cluster name followed by "**\_scheduler\_nodes\***". For example, if your cluster name is "idea-test" then search for "idea-test\_scheduler\_nodes\*". Once done, click "**Next Step**".

Use "**provisioning\_time**" as Time Field then click "**Create Index Pattern"**

##

## Job Information

**As Index Pattern Name**, specify your cluster name followed by "**\_scheduler\_jobs\***". For example, if your cluster name is "idea-test" then search for "_idea-test\_scheduler\_jobs\*"_. Once done, click "**Next Step**".

Use "**queue\_time**" as Time Field then click "**Create Index Pattern"**
{% endtab %}
{% endtabs %}

## Validate the index

Once your index is created, click the hamburger menu to open the sidebar and click "**Discover**" within the "**OpenSearch Dashboards**" section.

Select the Index pattern you have created previously using the dropdown section on the right

<figure><img src="../../../.gitbook/assets/Screen Shot 2022-11-09 at 9.14.00 PM.png" alt=""><figcaption><p>Scheduler Index will store Scale-Out Workload jobs. VDC pattern is for Virtual Desktops</p></figcaption></figure>

All your IDEA data should now be visible.&#x20;

{% hint style="warning" %}
**IMPORTANT**

OpenSearch display the events created within the last 15 minutes by default, make sure to update the timeframe accordingly.
{% endhint %}

<figure><img src="../../../.gitbook/assets/Screen Shot 2022-11-09 at 9.24.09 PM.png" alt=""><figcaption><p>Make sure to change the Time selection as needed.</p></figcaption></figure>

OpenSearch will display the various entries ingested by IDEA as well as all the relevant metadata. You can use these metadata as filters to narrow your research to specific key elements.

<figure><img src="../../../.gitbook/assets/Screen Shot 2022-11-09 at 9.22.47 PM.png" alt=""><figcaption><p>Example of IDEA data being ingested on OpenSearch</p></figcaption></figure>

It's now time to [create-your-own-analytics-visualizations.md](create-your-own-analytics-visualizations.md "mention").

## Troubleshooting access permission[¶](https://awslabs.github.io/scale-out-computing-on-aws/analytics/monitor-cluster-activity/#troubleshooting-access-permission) <a href="#troubleshooting-access-permission" id="troubleshooting-access-permission"></a>

Access to OpenSearch is restricted to the IP you have specified during the installation. If your IP change for any reason, you won't be able to access the analytics dashboard and will get the following error message:

```
{"Message":"User: anonymous is not authorized to perform: es:ESHttpGet"}
```

To solve this issue, log in to AWS Console and go to OpenSearch Service dashboard. Select your OpenSearch  cluster and click "Modify Access Policy"

![](https://awslabs.github.io/scale-out-computing-on-aws/imgs/kibana-8.png)

Finally, simply add your new IP under the "Condition" block, then click Submit

![](https://awslabs.github.io/scale-out-computing-on-aws/imgs/kibana-9.png)

Please note it may take up to 5 minutes for your IP to be validated
