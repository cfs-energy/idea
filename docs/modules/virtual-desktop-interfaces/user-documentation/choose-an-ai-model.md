---
description: Pick a model from the ones your project allows
---

# Choose an AI model

Cluster administrators keep a catalog of approved models, and each project is granted a subset of it. The models available to you are listed under **Account Settings** > **My Projects**.

## Invoke the profile, not the model id

Under each model, **Account Settings** > **My Projects** also shows an ARN ending in
`application-inference-profile/...`. That ARN is what you pass as the model id when you call Bedrock.

The model id itself will not work. Passing `us.anthropic.claude-...` directly is refused with
`AccessDeniedException ... not authorized to perform: bedrock:InvokeModel`, because your project is
granted its own profile rather than the shared one. That profile is also what attributes the spend to
your project, so there is no route that both works and skips the accounting.

```bash
# denied, even for a model your project allows
aws bedrock-runtime converse --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 ...

# works
aws bedrock-runtime converse \
  --model-id arn:aws:bedrock:us-east-2:111122223333:application-inference-profile/abcd1234efgh ...
```

The ARN is per project and per model, so a model granted to two projects has a different ARN in each.

## Claude Code on a virtual desktop

There is nothing to set up. A desktop in a project with model access arrives with the provider, the
region and the model already set, and the login banner names the models it got.

**Do not use the Bedrock setup wizard.** It lists the account's system inference profiles and tests
each one, and every candidate comes back `no InvokeModel permission`, correctly, because your project
is granted its own profile rather than the shared one. A wizard that goes green means something is
invoking a shared profile and the spend is not being attributed to any project. Skip it.

`/status` shows what you are on. The model reads as an `application-inference-profile` ARN, which is
opaque by design; the banner and **Account Settings** > **My Projects** both map it back to a model id.

### Defaults you can change

A desktop in a project with model access installs `/etc/idea/claude-code-defaults.json`, and every
login shell merges it into `~/.claude/settings.json`. The file holds three settings:

```json
{
  "outputStyle": "Concise",
  "permissions": {
    "defaultMode": "auto"
  },
  "autoMode": {
    "environment": [
      "$defaults",
      "Organization: the <CLUSTER_NAME> IDEA cluster, a shared HPC and virtual desktop environment on AWS",
      "Primary use of Claude Code: research and engineering work on a shared virtual desktop",
      "Cloud provider(s): AWS, region <REGION>",
      "Trusted cloud buckets: s3://<CLUSTER_BUCKET>",
      "Key internal services: the <CLUSTER_NAME> IDEA portal and its job scheduler"
    ]
  }
}
```

Concise replies, auto mode as the starting permission mode, and auto mode told which buckets and
services belong to this cluster so routine work does not prompt. On Amazon Bedrock the built-in
starting mode is Manual, so without `permissions.defaultMode` you would get Manual. `$defaults` is a
literal element meaning "keep the bundled auto mode environment, then add the lines below it";
removing it drops those bundled defaults and leaves only the cluster lines. Your administrator chooses
the permission mode with `virtual-desktop-controller.bedrock.claude_code.permission_mode`; the value
`none` seeds no `permissions` block at all.

That file is yours. The merge is per setting rather than per file: at each login it adds only the
three settings above, only the ones IDEA has never offered you before, and it records what it has
offered in `~/.claude/.idea-seeded`. A value you change is left alone, and a setting you delete stays
deleted. Home directories roam between desktops, which is why it merges on every login rather than
writing the file once. It runs from `/etc/profile.d`, so it takes effect the first time you open a
terminal, and it skips accounts below uid 1000.

To go back to the shipped defaults, clear the marker as well:

```bash
rm ~/.claude/settings.json ~/.claude/.idea-seeded   # next login offers them again
```

The model and provider are **not** in that file. They come from `/etc/environment` and apply to every
user on the desktop, so changing your settings cannot accidentally point you at a model your project
is not granted.

## Start with the smallest model

Bedrock bills per token. Input and output tokens are priced separately, output is the more expensive of the two, and the price per token between the smallest and the largest model in a vendor's line-up differs by roughly an order of magnitude.

Run the smallest model your project allows against a sample of your real prompts, score the results, and move up only where the score falls short. A model that is better on your task and ten times the price is a poor default and a reasonable exception.

## Match the model to the task

<table><thead><tr><th width="250">Task</th><th width="250">What decides the choice</th><th>Where to start</th></tr></thead><tbody><tr><td>Interactive coding, multi-step agent work</td><td>Instruction following, tool use, context length</td><td>The largest general purpose model your project allows</td></tr><tr><td>Summarizing or rewriting documents</td><td>Context window, cost per input token</td><td>A mid-size model</td></tr><tr><td>Classification, extraction, tagging at volume</td><td>Cost and latency per call</td><td>The smallest model that passes your evaluation</td></tr><tr><td>Drafting and chat</td><td>Response latency</td><td>A small or mid-size model</td></tr></tbody></table>

Before committing to a model, confirm it handles the modality you need (text, image input, image generation), that its context window fits your longest prompt plus the expected output, and that it supports streaming if your client requires it.

## Region and partition

Requests stay inside the partition your cluster runs in. Some models are reachable only through a geographic inference profile, which routes a request to one of a fixed set of regions in that partition; others are reachable in a single region. Whether cross-region routing is acceptable is a deployment decision your administrator makes, and it can differ per cluster.

## AWS GovCloud

* Bedrock pricing in GovCloud runs about 1.2x the commercial rate. A workload that is affordable in a commercial region can be materially more expensive there.
* Model access is not on by default. An administrator enables each model per account by hand, and third-party models must also be enabled in the linked commercial account. A model can be in the catalog and still be unusable until that step is done.

## Asking for a model that is not in the catalog

Administrators add models by id. Bring the model id, the task it is for, and an estimate of monthly token volume.

Approval is a commitment by the account owner rather than a UI toggle. In commercial regions model access is on by default, and the first invocation of a marketplace-listed model subscribes the AWS account to that model automatically, which is why models are added only once their terms and pricing are accepted.

## Cost attribution

Usage is tagged with the project and shows up in that project's AWS budget. Cost allocation data arrives with the usual AWS Cost Explorer delay of about a day and is not backfilled, so the first day of a new model reads low.
