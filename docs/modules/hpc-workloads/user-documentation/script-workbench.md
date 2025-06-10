# Script Workbench

{% hint style="info" %}
The Script Workbench is a web-based tool for creating, validating, and submitting PBS job scripts directly without needing to connect to a cluster node via SSH or a virtual desktop.
{% endhint %}

## Overview

The Script Workbench provides a convenient interface for working with PBS job scripts, offering features like:

* A code editor with syntax highlighting for shell scripts
* Real-time script validation through "dry run" functionality
* Cost estimation for jobs before submission
* Simplified job submission directly from the web interface
* File upload capability for existing scripts

This tool is particularly useful for users who are familiar with PBS directives and prefer to write their own job scripts rather than using form-based job submission interfaces.

## Accessing the Script Workbench

You can access the Script Workbench from the IDEA web interface:

1. Log in to your IDEA cluster web interface
2. Navigate to **IDEA** → **Home** → **Script Workbench**

## Using the Script Workbench

### Required PBS Directives

The Script Workbench interface displays which PBS directives are required / optional for your job script. As you edit your script, the UI will indicate in real time (with color coding) whether each required directive is present or missing.

{% hint style="warning" %}
If any required directive is missing, the Script Workbench will display a validation error and prevent you from submitting the job.
{% endhint %}

### Creating a Script

You can create a script in several ways:

1. **Write from scratch** - Type directly in the code editor
2. **Use a template** - Click "Insert sample PBS script" to start with a basic template
3. **Upload file** - Import an existing script from your local machine
4. **Browse files** - Open the file browser to select an existing script from your IDEA storage
5. **Right-click context menu** - In the file browser, right-click on any script file and select "Open in Script Workbench" from the context menu

The interface provides buttons for these options directly below the editor.

### Code Editor Features

The Script Workbench includes a full-featured code editor with:

* Syntax highlighting for shell scripts
* Line numbers
* Error and warning indicators
* Dark/light theme support (automatically matches your interface theme)
* Status bar showing cursor position

### Validating Your Script (Dry Run)

Before submitting your job, validate it using the Dry Run feature:

1. Enter your PBS script in the editor
2. Click the **Dry Run** button at the bottom of the editor
3. Review the validation results displayed below the editor

The Dry Run will check for:
- Missing or incorrect PBS directives
- Permission issues (project access, queue access)
- Resource availability
- Service quota limits

{% hint style="info" %}
The Dry Run also provides a cost estimate based on your script's walltime and resource requests, helping you understand potential costs before submission.
{% endhint %}

### Submitting Your Job

Once your script passes validation:

1. Click the **Submit Job** button
2. Review the job details provided after submission
3. Your job will be submitted to the scheduler and will appear in your active jobs list

After successful submission, you'll receive:
- A job ID for tracking
- Summary of job parameters
- Cost estimate for the job
- Link to view all active jobs

{% hint style="success" %}
Your submitted script is automatically saved in your home directory under `~/jobs` for future reference.
{% endhint %}

## Related Documentation

- [Submit a job](submit-a-job.md)
- [Control my jobs](control-my-jobs.md)
- [Supported EC2 parameters](supported-ec2-parameters.md)
- [Job Storage](job-storage.md)
