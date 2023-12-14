# Set up budget per project



On this page, we will demonstrate how to configure a budget for a given project and reject jobs if you exceed the allocated budget

## Create a new AWS Budget <a href="#submit-a-job-when-budget-is-valid" id="submit-a-job-when-budget-is-valid"></a>

Go to AWS Billing, click "**Budgets**" on the left sidebar and create a new budget

![](https://awslabs.github.io/scale-out-computing-on-aws/imgs/budget-7.png)

Click "**Create Budget"** and choose **"Customize (Advanced)"** to create a **"Cost Budget"** and **c**onfigure the Period/Budget Scope based on your requirements.&#x20;

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-10 at 2.59.18 PM.png" alt=""><figcaption><p>Example of a project with a monthly budget of $3500</p></figcaption></figure>

{% hint style="info" %}
We recommend you to set up a email notification when your budget exceed 80%
{% endhint %}

## Map your AWS Budget to an IDEA project&#x20;

You now need to map the AWS Budget to an IDEA project ([click here to learn more about project management on IDEA](https://docs.ide-on-aws.com/cluster-manager/menu/projects-management))

On your IDEA web interface, click "**Projects**" and "**Create a New Project**". Fill out the form and make sure to "**Enable Budget for this Project**". Enter the AWS budget name for your project (must match the name of your budget configured on AWS Budget)

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-10 at 3.04.24 PM.png" alt=""><figcaption><p>Make sure the name match your AWS Budget</p></figcaption></figure>

You should now see the budget on your IDEA project. Make sure to select your project and click "**Actions**" > "**Enable Project**"

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-10 at 3.06.00 PM.png" alt=""><figcaption><p>Validate your AWS Budget directly within IDEA</p></figcaption></figure>

## Map the project to a queue

Now that you have your IDEA project linked to AWS Budget created, you need to specify which queue(s) you want this configuration to apply. Navigate to [IDEA Queue Profile](https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/queue-profiles)

Select the queue profile and click "**Actions**" > "**Edit Queue Profile**" then map your project to the profile.

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-10 at 3.08.59 PM.png" alt=""><figcaption><p>Your IDEA project is now applicable to all queues configured to "compute" profile</p></figcaption></figure>

## Test the integration

### Valid Budget

With a valid budget, job(s) will be submitted successfully

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-10 at 3.11.26 PM.png" alt=""><figcaption></figcaption></figure>

### Invalid Budget

Let's now pretend we ran out of money for a given budget.

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-10 at 3.14.37 PM.png" alt=""><figcaption><p>No money left for the project assigne to the queue</p></figcaption></figure>

Job submission will then be impossible on IDEA

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-10 at 3.22.39 PM.png" alt=""><figcaption><p>Unable to submit job because of AWS Budget</p></figcaption></figure>

{% hint style="info" %}
Allow 15 minutes for IDEA to be fully in sync with AWS Budget
{% endhint %}

## Integration with [Virtual Desktop Interface (VDI)](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/QthiamUzKn8KJLl0hYBf/ "mention") <a href="#submit-a-job-when-budget-is-invalid" id="submit-a-job-when-budget-is-invalid"></a>

IDEA projects can be consumed by multiple modules. In a similar way where [Scale-Out Workloads](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/LGamNPuOYtjAP3GFfRJO/ "mention") will be rejected if a budget has expired, IDEA users won't be able to provision their [Virtual Desktop Interface (VDI)](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/QthiamUzKn8KJLl0hYBf/ "mention") until additional budget is available to them.
