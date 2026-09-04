# Review your AWS spend

### AWS Cost Explorer <a href="#aws-cost-explorer" id="aws-cost-explorer"></a>

Any EC2 resource launched by IDEA comes with an extensive list of EC2 tags that can be used to get detailed information about your cluster usage. List includes (but not limited to):

* Project Name
* Job Owner
* Job Name
* Job Queue
* Job Id

<figure><img src="../../.gitbook/assets/bp_bugdet_extags.webp" alt=""><figcaption><p>IDEA automatically assign tags. Custom tags can be added as needed</p></figcaption></figure>

{% hint style="info" %}
All IDEA generated tags are prefixed with "**idea:**"
{% endhint %}

#### Step1: Enable Cost Allocation Tags <a href="#step1-enable-cost-allocation-tags" id="step1-enable-cost-allocation-tags"></a>

{% hint style="warning" %}
This step cannot be done from the account IDEA runs in if that account is a member of an AWS
Organization. AWS is explicit: "Only the management account in an organization and single accounts
that aren't members of an organization have access to the **cost allocation tags** manager in the
Billing console." A member account has no Cost allocation tags page, so somebody with access to the
management account has to activate the `idea:` tag keys.

IDEA cannot do this for you, and nothing fails loudly when it has not been done. Until the tag keys
are active, `idea:` tags do not appear in AWS Cost Explorer and a budget filtered on them reads as no
spend, which looks the same as a project that has not spent anything.

Tag keys can take up to 24 hours to appear on the cost allocation tags page, and up to another 24
hours to activate after that. Activation is not retroactive: costs incurred before the key was
active are not attributed to it unless the management account requests a backfill.
{% endhint %}

Click on your account name (top right on the screen) then click "**Billing Dashboard**". Once connected to your Billing dashboard, click "**Cost Allocation Tags**" on the left sidebar.

![Cost Allocation section is available via the left sidebar](https://awslabs.github.io/scale-out-computing-on-aws/imgs/budget-2.png)

Search all "**idea**" tags then click "Activate". Status of each tag should now be changed to "Active".

<figure><img src="../../.gitbook/assets/bp_bugdet_activatetags.webp" alt=""><figcaption><p>Activate all tags to be usable on Cost Explorer</p></figcaption></figure>

#### Step 2: Query Cost Explorer <a href="#step-2-enable-cost-explorer" id="step-2-enable-cost-explorer"></a>

{% hint style="info" %}
It could take up to 24 hours for the tags to be visible on Cost Explorer.
{% endhint %}

Access "**AWS Cost Explorer**" service via the EC2 console the click "**Cost Explorer**" on the left sidebar.

Open your Cost Explorer tab and specify your filters. In this example I want to get the EC2 cost (1), group by day for my queue named "cpus" (2).

![](../../.gitbook/assets/bp\_bugdet\_costex1.webp)

To get more detailed information, select 'Group By' and apply additional filters. Here is an example if I want user level information for "cpus" queue Click "Tag" section under "Group By" horizontal label (1) and select "idea:JobOwner" tag. Your graph will automatically be updated with a cost breakdown by users for "cpus" queue

![](../../.gitbook/assets/bp\_bugdet\_costex2.webp)
