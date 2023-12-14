# Create Web Based Job Submission Worfklows

On this page, you will learn how to create an application profile and give your users the ability to submit HPC jobs via a simple web interface.

## No coding experience = No Problem <a href="#no-coding-experience-no-problem" id="no-coding-experience-no-problem"></a>

IDEA features a complete visual form builder experience with simple drag & drop capabilities. HPC admins can build their own forms without any coding/HTML experience via an intuitive wysiwyg (What You See Is What You Get) solution.

## Build the job script <a href="#build-the-job-script" id="build-the-job-script"></a>

The first step is to identify the variables you want your users to configure. Let's take this simple job file for reference:

```bash
#PBS -N MyJobName
#PBS -q queue_1
#PBS -l instance_type=t3.xlarge

# CD into current working directory
cd $PBS_O_WORKDIR 

# Prepare the job environment, edit the current PATH, License Server etc
export PATH=/apps/softwarename/v2023/
export LICENSE_SERVER=1234@licenseserver.internal

# Run the solver
/apps/softwarename/v2023/bin/solver --cpus 36 \
     --input-file myfile.input \
     --parameter1 value1

# Once job is complete, archive output to S3
BACKUP="true"
if [[ "$BACKUP" == "true" ]]; 
  then
     aws s3 sync . s3://mybucketname/
fi
```

Replace the values/parameters you want your users to configure with  `{{``VARIABLE_NAME }}` such as:

Job Script you will need to reuse later during [#configure-the-job-script](create-web-based-job-submission-worfklows.md#configure-the-job-script "mention"):

```
#PBS -N {{ job_name }}
#PBS -q {{ queue_name }} 
#PBS -l instance_type={{ instance_type }}

# CD into current working directory
cd $PBS_O_WORKDIR 

# Prepare the job environment, edit the current PATH, License Server etc
export PATH=/apps/softwarename/{{ version }}/
export LICENSE_SERVER=1234@licenseserver.internal

# Run the solver
/apps/softwarename/{{ version }}/bin/solver --cpus {{ cpus }} \
     --input-file {{ input_file }} \
     --parameter1 {{ parameter_1 }}

# Once job is complete, archive output to S3
BACKUP={{ backup_enabled }}
if [[ "$BACKUP" == "true" ]];
  then
     aws s3 sync . s3://{{ bucket_to_archive }}/
fi
```

In this example:

* \{{ job\_name \}} will be replaced by the actual job name specified by the user
* \{{ queue\_name \}} will be replaced by the actual queue
* \{{ instance\_type \}} will be replaced with the actual instance type
* \{{ input\_file \}} will be replaced by the simulation input
* \{{ version \}} will let the user decide what software version to use
* \{{ cpus \}} , \{{ input\_file \}}, and \{{ parameter1 \}} are application specific parameters
* \{{ backup\_enabled \}} will determine if we want to archive the job output to S3
* \{{ bucket\_to\_archive \}} will point to the user's personal S3 bucket

## Create the HTML form <a href="#create-the-html-form" id="create-the-html-form"></a>

Now that you have identified all variables, you must create their associated HTML components. As a HPC admin, navigate to "**Application**" tab in the left sidebar. Click "**Create Application**" to load the application interface. You will see three tabs:

* Preview Form: Live rendering of the form you are creating
* Form Builder: Where you build the HTML form
* Advanced Mode: Build the HTML form directly via JSON inputs

Click "**Form Builder**" then "**Reset Form**" to start from a fresh template and start to build the HTML form using the examples below.

{% hint style="info" %}
As an alternate option, you can copy/paste the content of the following section to  "**Form Builder (Advanced Mode using JSON)**" to directly import the template.&#x20;
{% endhint %}

<details>

<summary>Template (copy to Form Builder Advanced Mode)</summary>

```json
[
    {
        "name": "job_name",
        "title": "Job Name",
        "description": "Name of your simulation",
        "help_text": "",
        "param_type": "text",
        "data_type": "str",
        "multiple": false,
        "multiline": false,
        "default": "",
        "readonly": false,
        "validate": {
            "required": true
        },
        "choices": []
    },
    {
        "name": "instance_type",
        "title": "EC2 instance type to use",
        "description": "",
        "help_text": "You cannot change this value",
        "param_type": "text",
        "data_type": "str",
        "multiple": false,
        "multiline": false,
        "default": "t3.xlarge",
        "readonly": true,
        "validate": {
            "required": true
        },
        "choices": []
    },
    {
        "name": "queue_name",
        "title": "Queue To Use",
        "description": "",
        "help_text": "",
        "param_type": "text",
        "data_type": "str",
        "multiple": false,
        "multiline": false,
        "default": "normal",
        "readonly": true,
        "validate": {
            "required": true
        },
        "choices": []
    },
    {
        "name": "input_file",
        "title": "Your input file",
        "description": "",
        "help_text": "",
        "param_type": "text",
        "data_type": "str",
        "multiple": false,
        "multiline": false,
        "default": "",
        "readonly": false,
        "validate": {
            "required": true
        },
        "choices": []
    },
    {
        "name": "cpus",
        "title": "CPUs number to provision",
        "description": "How many CPUs are you planning to use for this simulation?",
        "help_text": "",
        "param_type": "text",
        "data_type": "int",
        "multiple": false,
        "multiline": false,
        "default": "",
        "readonly": false,
        "validate": {
            "min": 1,
            "max": 100,
            "required": true
        },
        "choices": []
    },
    {
        "name": "version",
        "title": "Software version",
        "description": "Choose what solver version you want to use",
        "help_text": "",
        "param_type": "select",
        "data_type": "str",
        "multiple": false,
        "multiline": false,
        "default": "",
        "readonly": false,
        "validate": {
            "required": true
        },
        "choices": [
            {
                "title": "Version 2023 (latest)",
                "value": "v2023"
            },
            {
                "title": "Version 2022",
                "value": "v2022"
            },
            {
                "title": "Version 2021 (Deprecated)",
                "value": "v2021"
            }
        ]
    },
    {
        "name": "parameter_1",
        "title": "Value for --parameter_1",
        "description": "",
        "help_text": "",
        "param_type": "text",
        "data_type": "str",
        "multiple": false,
        "multiline": false,
        "default": "",
        "readonly": false,
        "validate": {
            "required": true
        },
        "choices": []
    },
    {
        "name": "backup_enabled",
        "title": "Enable Backup to S3?",
        "description": "Choose if you want to enable backup to your S3 bucket",
        "help_text": "If enabled, you will be prompted for a bucket name",
        "param_type": "confirm",
        "data_type": "bool",
        "multiple": false,
        "multiline": false,
        "default": "",
        "readonly": false,
        "validate": {
            "required": true
        },
        "choices": []
    },
    {
        "name": "bucket_to_archive",
        "title": "Bucket Name",
        "description": "",
        "help_text": "",
        "param_type": "text",
        "data_type": "str",
        "multiple": false,
        "multiline": false,
        "default": "",
        "readonly": false,
        "validate": {
            "required": true
        },
        "choices": [],
        "when": {
            "eq": true,
            "param": "backup_enabled"
        }
    }
]
```

</details>

### Text Field

Click "**Add Form Field**" and add a new "Text" field.

Configure the widget and configure the Name settings (red) with the variable name associated (job\_name in our example) then click "**Save**"

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 10.47.59 AM.png" alt=""><figcaption><p>The value entered by the user for job_name will be sent to the job script and retrieved via {{ job_name }}</p></figcaption></figure>

Repeat the same operation for job\_project, bucket\_to\_archive, parameter and input\_file

{% hint style="info" %}
\{{ input\_file \}} will automatically be configured with the path of the input file selected by the user
{% endhint %}

### Read Only Field

In this example, e want to enforce the instance type to be c5.18xlarge.&#x20;

* Create a new Text field
* Specify the name (red) / title / description
* Under Preview, write c5.18xlarge (green)
* Enable the "is ReadOnly" button (blue)

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 10.53.28 AM.png" alt=""><figcaption><p>"Is ReadOnly" toggle button prevent users to change the value</p></figcaption></figure>

### **Number Field**

In this example, we want to create the `cpus` parameter hence recommend using the "Number"  field

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 10.56.27 AM.png" alt=""><figcaption></figcaption></figure>

To specify Min/Max values, click "**Form Builder (Advanced Mode Using Json)**" and locate the json entry associated to your field. Finally, add "**min**" and "**max**" values as shown below (line 14 and 15)

{% code lineNumbers="true" %}
```json

    {
        "name": "cpus",
        "title": "CPUs Number",
        "description": "How many CPUs are you planning to provision",
        "help_text": "",
        "readonly": false,
        "multiple": false,
        "multiline": false,
        "validate": {
            "required": true
            "max": 100,
            "min": 1
        },
        "choices": [],
        "default": "10",
        "data_type": "int",
        "param_type": "text"
    }
```
{% endcode %}

### Select Field

Assuming your application hierarchy is as follow:

```
└── /apps
    └── /softwarename
        ├── v2023
        ├── v2022
        └── v2021
```

This time, we recommend you using the "Select" component:

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 1.01.32 PM.png" alt=""><figcaption></figcaption></figure>

Similarly to the previous examples, map the "Name" to your variable name (\{{ version \}}) and add labels (green) and their associated values (blue)

### **Toggle Field & dynamic elements display/hide**

\{{ backup\_enabled \}} is a boolean which enable (1) or disable (0) archival of the job output data to S3. This time use "Toggle" form field.

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 1.05.01 PM.png" alt=""><figcaption></figcaption></figure>

Now let's create a new "Text" field called `bucket_to_archive`. However, we want to display this information only when `backup_enabled` is set to checked. To do that, click "**Advanced Mode**", locate the "bucket\_to\_archive" section and add the `when` section as shown below. This will ensure bucket\_to\_archive will only be visible to the users if bucket\_enabled is checked

```json
 {
        "description": "",
        "help_text": "",
        "multiple": false,
        "multiline": false,
        "validate": {
            "required": true
        },
        "choices": [],
        "when": {
            "param": "backup_enabled",
            "eq": true
        },
        "default": "",
        "data_type": "str",
        "param_type": "text",
        "field_type": "text",
        "name": "bucket_to_archive",
        "title": "Bucket Name",
        "readonly": false,
        "required": true
    }
```

{% tabs %}
{% tab title="backup_enabled is False" %}
![](<../.gitbook/assets/Screen Shot 2022-11-18 at 2.42.07 PM.png>)
{% endtab %}

{% tab title="backup_enabled is True" %}
![](<../.gitbook/assets/Screen Shot 2022-11-18 at 2.42.21 PM.png>)
{% endtab %}
{% endtabs %}

## Configure the job script <a href="#configure-the-job-script" id="configure-the-job-script"></a>

Once your HTML form is done, simply click "**Next**" and copy/paste [#build-the-job-script](create-web-based-job-submission-worfklows.md#build-the-job-script "mention") within the "**Job Script**" section. Select "Jinja2" as template.

{% hint style="info" %}
Job Script support Jinja2 templating. For example, **\{{ job\_name | upper \}}** will retrieve the HTML field named **job\_name** and enforce uppercase.
{% endhint %}

Since this script is expected to be triggered by OpenPBS scheduler, keep the default "PBS Job Script" as interpreter. IDEA also gives you an easy way to determine if all the variables you have configured on the HTML form are being used in your job script

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 1.09.34 PM.png" alt=""><figcaption></figcaption></figure>

**Save your profile**

Finally, click "Next" choose an name, upload a thumbnail if needed (optional) and select the IDEA project that are authorized to use this form (learn more about project: [https://docs.ide-on-aws.com/cluster-manager/menu/projects-management)](https://docs.ide-on-aws.com/cluster-manager/menu/projects-management)

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 1.12.01 PM.png" alt=""><figcaption></figcaption></figure>

## Submit a test job <a href="#submit-a-test-job" id="submit-a-test-job"></a>

To submit a job, first navigate to "File Browser" on the left sidebar ([https://docs.ide-on-aws.com/idea/first-time-users/file-browser)](https://docs.ide-on-aws.com/idea/first-time-users/file-browser).

&#x20;Choose your input file and click "**Submit Job**" icon. This will open the submit job interface.wizard

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 1.13.55 PM.png" alt=""><figcaption></figcaption></figure>

From there, click "Application" and select the application you just created

![](https://awslabs.github.io/scale-out-computing-on-aws/imgs/application-profile-9.png)

Fill out the HTML form generated during the previous step with your own inputs.

{% hint style="info" %}
IDEA also determine the number of nodes to provision automatically based on the instance type and cpus requested. For example, if the instance is c5.18xlarge (36 cores) and the number of CPUs requested by the user is 72. IDEA automatically detects these values and determine the number of instances to provision is 2
{% endhint %}

Prior to submitting a job, you can click "**Dry run**" to validate whether or not you job can run. You will also be able to check the cost estimation of your job as well as additional information specific to your simulation.

![](<../.gitbook/assets/Screen Shot 2022-11-17 at 1.16.40 PM.png>)

Once done, click "Submit Job" and you job will be submitted to the queue.

You can now verify the job script generated by IDEA correctly reports the HTML inputs you specified via the web form:

{% tabs %}
{% tab title="HTML form submitted" %}
![](<../.gitbook/assets/Screen Shot 2022-11-18 at 2.28.38 PM (2).png>)

(Click to enlarge)
{% endtab %}

{% tab title="Job Script Generated" %}
![](<../.gitbook/assets/Screen Shot 2022-11-18 at 2.31.06 PM.png>)

(Click to enlarge)
{% endtab %}
{% endtabs %}

## Work with Dynamic Form Fields <a href="#what-if-i-want-to-run-a-linux-scriptcommand" id="what-if-i-want-to-run-a-linux-scriptcommand"></a>

You can easily hide/show HTML elements based on the values of other elements via the "when" option.

For example, let's say you have a dropdown menu named "dropdown1" and you want to display it only when the user click on the toggle button "checkbox1"

```json
{
/*
 1 - Click Form Builder (Advanced Mode using JSON)
 2 - Locate the HTML element you want to edit
*/
    "param_type": "select",
    "name": "dropdown1",
    /* Add the "when" block below */
    "when": {
        "param": "checkbox1",
        "eq": true
    }
}
```

Supported operators are:

* empty
* not\_empty
* eq
* not\_eq
* in
* not\_in

Additional comparators will be added in the future.

## What if I want to run a Linux script/command <a href="#what-if-i-want-to-run-a-linux-scriptcommand" id="what-if-i-want-to-run-a-linux-scriptcommand"></a>

If you want your job script to use regular bash interpreter (and not qsub), simple select "Linux Shell Script". In other words, the output generated by your HTML world will be a simple bash script and SOCA will run `/bin/bash job_submit.sh` command.

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-17 at 1.18.38 PM.png" alt=""><figcaption></figcaption></figure>
