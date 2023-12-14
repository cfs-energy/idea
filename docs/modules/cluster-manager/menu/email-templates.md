# Email Templates

IDEA lets you easily configure and manage automated email notifications such as when a virtual desktop is launched or when one simulation job has completed

<figure><img src="../.gitbook/assets/Screen Shot 2022-10-24 at 2.13.02 PM.png" alt=""><figcaption><p>Email Templates interface</p></figcaption></figure>

### Create a new email template

* Click "**Create Email Template**"
* Choose a friendly name
* Specify the template name. You will use this name every time you want to invoke the template
* Finally, enter the subject a the body (HTML supported) of the email

{% hint style="info" %}
Refer to [#jinja2-variables](email-templates.md#jinja2-variables "mention") if you need to customize the email body with pre-defined IDEA variables
{% endhint %}



### Edit email template

* Select an email template
* Click "**Actions**" then "**Edit Email Template**"

{% hint style="info" %}
Refer to [#jinja2-variables](email-templates.md#jinja2-variables "mention") if you need to customize the email body with pre-defined IDEA variables
{% endhint %}

### Jinja2 Variables

{% tabs %}
{% tab title="Virtual Desktops Interface" %}
| Variable Name | Description                          |
| ------------- | ------------------------------------ |
| cluster\_name | Name of your IDEA cluster            |
| session.owner | Owner of the virtual desktop session |
| session.name  | Name of the virtual desktop session  |
{% endtab %}

{% tab title="Scale-Out Workloads" %}
| Variable Name   | Description                               |
| --------------- | ----------------------------------------- |
| job.name        | Name of the simulation                    |
| job.owner       | Owner of the simulation                   |
| job.job\_id     | ID of the simulation                      |
| job.queue\_type | Queue name where the job was submitted to |
{% endtab %}
{% endtabs %}
