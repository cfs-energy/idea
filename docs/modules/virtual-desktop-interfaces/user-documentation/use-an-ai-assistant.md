---
description: Call the models your project allows from a Linux or Windows virtual desktop
---

# Use an AI assistant

Your virtual desktop can call Amazon Bedrock models when two things are true: the cluster administrator has enabled Bedrock, and your project has been granted at least one model. Both are visible under **Account Settings** > **My Projects**. The **AI Models** column lists the models your project can invoke; if the column is absent, the feature is off for this cluster.

## Credentials

There are no API keys to request, copy, or store.

The desktop runs under an EC2 instance role that belongs to your project. The AWS CLI and the AWS SDKs read credentials from the instance metadata service automatically, so `aws` commands and SDK clients work with no credential configuration. The credentials rotate on their own and are never written to your home directory.

What the role allows is decided on the server:

* only the models listed on your project, and
* only through your project's inference profile, which is what attributes the usage to your project.

A model that is not on your project's list fails with `AccessDeniedException` no matter which client you use. The check is in IAM, not in the web UI, so nothing you set locally changes it.

{% hint style="info" %}
Sessions are one instance per desktop, so the role identifies you and your project. Usage is attributable per user.
{% endhint %}

## Region

Requests go to the Bedrock endpoint in the desktop's own region, in the same AWS partition as the cluster. The CLI and the SDKs resolve that region from the instance, so a call that does not name a region still lands in the cluster's region.

## Invoke a model

Native Bedrock runtime call with the AWS CLI:

```bash
aws bedrock-runtime converse \
  --model-id "<model-id>" \
  --messages '[{"role":"user","content":[{"text":"Summarize the file I just wrote."}]}]'
```

The `--model-id` value is your project's **application inference profile ARN**, not the model id. Both are listed under **Account Settings** > **My Projects** > **AI Models**: the model id on the first line, the profile ARN under it. The policy requires the call to go through the profile, so a bare foundation model id is denied.

A model showing `Not provisioned yet` has been granted but its profile has not been created; it becomes usable once the cluster reconciles the project.

## Assistant CLIs

Command line assistants that support Bedrock as a backend need three things: the setting that selects the Bedrock backend, the region, and the model id. They obtain credentials from the instance role like any other AWS client, so there is no key or profile to configure. Check the tool's own documentation for the variable names.

## Known limits

* Application inference profiles are accepted by the native runtime APIs (`InvokeModel`, `InvokeModelWithResponseStream`, `Converse`, `ConverseStream`). The OpenAI-compatible endpoints reject them, so a client pinned to those endpoints cannot use a project profile.
* A project role is granted `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`, and only when the call goes through one of the project's own application inference profiles. It also gets `bedrock:GetInferenceProfile` on those profiles and `bedrock:ListInferenceProfiles`, so a client can find them. No other Bedrock action is granted, and no model outside the project's profiles can be invoked.
* Apart from Bedrock, a project role carries the same host permissions as any other cluster host: the DCV host policy, AWS Systems Manager, the CloudWatch agent, and whatever managed policies the administrator applies to every IDEA instance role. It replaces the shared host role rather than adding to it.
* Embedding models are not available through project inference profiles.

## Troubleshooting

<table><thead><tr><th width="290">Error</th><th>What it means</th></tr></thead><tbody><tr><td><code>AccessDeniedException</code> on the model or profile resource</td><td>The model is not on your project's list, or the request did not go through your project's inference profile.</td></tr><tr><td><code>ValidationException</code> about on-demand throughput</td><td>The model can only be reached through an inference profile. Pass the profile id, not the bare model id.</td></tr><tr><td><code>ResourceNotFoundException</code> naming use case details</td><td>The model vendor requires a one-time use case form for this AWS account. An administrator submits it once in the AWS console; nothing on the desktop works around it.</td></tr><tr><td><code>AccessDeniedException</code> in AWS GovCloud on a model that works elsewhere</td><td>Model access in GovCloud is enabled per account by an administrator. See <a href="choose-an-ai-model.md">Choose an AI model</a>.</td></tr></tbody></table>

## Cost

Bedrock usage is tagged with your project and lands in that project's AWS budget next to desktop and job spend. Token prices differ sharply between models: see [Choose an AI model](choose-an-ai-model.md).

Your administrator can have the cluster stop granting model access to a project that is over its budget. A desktop started while that is the case still starts, and still works for everything else, but its calls to Bedrock fail with `AccessDeniedException` until the budget allows them again. A desktop that was already running keeps the access it launched with. If model access has gone away and nothing about your project changed, ask your administrator whether the project budget is spent.
