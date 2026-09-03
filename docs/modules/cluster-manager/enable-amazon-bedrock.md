---
description: Turn on Amazon Bedrock model access for the cluster, projects, and HPC jobs
---

# Enable Amazon Bedrock

Amazon Bedrock access in IDEA is off by default and gated at three levels: the AWS account, the cluster, and each project. All three have to be on before a user can call a model.

## 1. Request model access in the console

Before IDEA can provision anything, the account itself needs access to the foundation models you plan to use. In the Bedrock console, under **Model access**, request access to each model. This is a one-time, per-account, per-region grant. IDEA does not make this request for you, and a model missing from this list fails every invocation regardless of the configuration below.

## 2. Turn Bedrock on for the cluster

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.bedrock.enabled,Type=bool,Value=true" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

Then set the model catalog, `cluster-manager.bedrock.model_ids`. Use geographic inference profile ids, prefixed `us.`, `eu.` or `apac.`, rather than base model ids or `global.` ids. A global id is rejected at config time because it can route a call outside the account's geography; use the profile id for your own region's geography instead.

{% hint style="warning" %}
Toggling `cluster-manager.bedrock.enabled` requires a redeploy of both **cluster-manager** and **virtual-desktop-controller**. The flag gates the IAM permissions boundary and inference profile provisioning cluster-manager owns, and the invocation logging destination virtual-desktop-controller's hosts write against.
{% endhint %}

## 3. Turn it on per project

Cluster-level enablement provisions nothing by itself. Each project opts in separately, and opt-in defaults to off. See [Projects Management](projects-management.md) for enabling Bedrock on a project, picking its models, and what gets provisioned.

## 4. Turn it on for HPC jobs

Desktops pick up project-level Bedrock access on their own. Jobs do not: set `scheduler.bedrock.enabled` to `true` as well and redeploy the scheduler module. Without it, jobs keep running under the compute node role and get no model access, even on a Bedrock-enabled project.

## Usage and spend

The Projects page reports tokens and requests per model, aggregated from Bedrock invocation logging. Spend is a separate figure pulled from AWS Cost Explorer, which has no endpoint in GovCloud, so spend reads "cost unavailable" there; token counts are unaffected. See [AI Usage Tracking](ai-usage-tracking.md) for both.
