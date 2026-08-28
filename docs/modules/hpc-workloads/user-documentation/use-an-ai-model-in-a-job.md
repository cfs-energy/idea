---
description: Call the models your project allows from a compute node
---

# Use an AI model in a job

A job can call Amazon Bedrock models when three things are true: the cluster administrator has enabled Bedrock, enabled it for jobs, and your project has been granted at least one model. The models your project allows are listed under **Account Settings** > **My Projects**; if the **AI Models** column is absent, the feature is off for this cluster.

## Credentials

There are no API keys to request, copy, or store.

A job submitted to a project that has Bedrock enabled runs its compute nodes under that project's EC2 instance role, in place of the cluster's default compute node role. The AWS CLI and the AWS SDKs read credentials from the instance metadata service, so `aws` commands and SDK clients inside the job script work with no credential configuration.

What the role allows is decided on the server:

* only the models listed on your project, and
* only through your project's inference profile, which is what attributes the usage to your project.

A model that is not on your project's list fails with `AccessDeniedException`. The check is in IAM, so nothing set in the job script changes it.

## Invoke a model

Pass your project's **application inference profile ARN** as the model id, not the model id itself. Both are listed under **Account Settings** > **My Projects** > **AI Models**: the model id on the first line, the profile ARN under it.

```bash
#!/bin/bash
#PBS -N summarize
#PBS -P my-project

aws bedrock-runtime converse \
  --model-id "<application-inference-profile-arn>" \
  --messages '[{"role":"user","content":[{"text":"Summarize results.csv"}]}]'
```

Requests go to the Bedrock endpoint in the compute node's own region, in the same AWS partition as the cluster.

## The job's project decides

Model access follows the project named at submission (`-P`, or the project selected in the web form), not the submitting user. A job submitted to a project without Bedrock runs under the default compute node role and cannot reach any model.

A job in a project that has Bedrock enabled runs under the project's instance profile, which is applied for you. A compute node holds one instance role, so submitting a different `instance_profile` would replace the project role and remove the job's model access: such a job is rejected at submission. Submitting the project's own instance profile is accepted, and submitting none is the normal case.

## A model list that changes while the job runs

The allowlist is enforced by the project role's IAM policy, which the cluster reconciles as the project is edited. A running job follows those edits: a model added to the project becomes usable without resubmitting, and a model removed from it starts failing with `AccessDeniedException` in the same job. Jobs are never stopped or requeued because of a project edit, so a long job that must not lose a model should not have that model revoked underneath it.

A job that has not started yet is unaffected: its compute nodes are launched with whatever the project allows at that moment.

## A job that waits for model access

A job whose project has Bedrock enabled is not started until the project's access is usable. While the cluster is still reconciling a newly granted model, the job stays queued and its error message names the reason. This is deliberate: the alternative is a job that consumes compute and then fails its first model call.

If the reason names a configuration change - a redeploy, or an instance profile a queue does not authorize - the job will not start on its own. See [My job is not starting](troubleshooting/my-job-is-not-starting.md).

## Known limits

* Application inference profiles are accepted by the native runtime APIs (`InvokeModel`, `InvokeModelWithResponseStream`, `Converse`, `ConverseStream`). The OpenAI-compatible endpoints reject them.
* A project role is granted `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`, and only when the call goes through one of the project's own application inference profiles. It also gets `bedrock:GetInferenceProfile` on those profiles and `bedrock:ListInferenceProfiles`, so a client can find them. No other Bedrock action is granted, and no model outside the project's profiles can be invoked.
* Apart from Bedrock, a project role carries the same host permissions as any other cluster host: the DCV host policy, AWS Systems Manager, the CloudWatch agent, and whatever managed policies the administrator applies to every IDEA instance role. It replaces the shared host role rather than adding to it.
* A job cannot request a subset of its project's models. The allowlist is per project.

## Cost

Bedrock usage is tagged with the job's project and lands in that project's AWS budget next to its compute spend. Token prices differ sharply between models: see [Choose an AI model](../../virtual-desktop-interfaces/user-documentation/choose-an-ai-model.md).
