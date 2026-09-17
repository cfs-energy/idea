/**
 * Three of the four providers build nothing: `prometheus` only asserts two config keys exist,
 * `dogstatsd` does not even do that (the agent ships with the modules), and an unknown provider
 * raises at synth. Only `cloudwatch` and `amazon_managed_prometheus` emit a resource, and the
 * CloudWatch dashboard is deliberately empty, so `DashboardBody` is `{"widgets":[]}`.
 */

import * as aps from 'aws-cdk-lib/aws-aps';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';

import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { ExistingSocaCluster } from '../constructs/existing-resources.ts';

export const METRICS_PROVIDER_CLOUDWATCH = 'cloudwatch';
export const METRICS_PROVIDER_AMAZON_MANAGED_PROMETHEUS = 'amazon_managed_prometheus';
export const METRICS_PROVIDER_PROMETHEUS = 'prometheus';
export const METRICS_PROVIDER_DOGSTATSD = 'dogstatsd';

export class MetricsStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  cloudwatchDashboard: cloudwatch.Dashboard | undefined;
  amazonPrometheusWorkspace: aps.CfnWorkspace | undefined;

  constructor(props: StackBuildProps) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    this.cluster = new ExistingSocaCluster(this.context, this.stack);

    const provider = this.metricsProvider();
    if (provider === METRICS_PROVIDER_CLOUDWATCH) {
      this.buildCloudWatch();
    } else if (provider === METRICS_PROVIDER_AMAZON_MANAGED_PROMETHEUS) {
      this.buildAmazonManagedPrometheus();
    } else if (provider === METRICS_PROVIDER_PROMETHEUS) {
      this.buildPrometheus();
    } else if (provider === METRICS_PROVIDER_DOGSTATSD) {
      // The metrics agent is deployed with the modules, not by this stack.
    } else {
      throw new Error(`metrics provider: ${provider} not supported`);
    }

    this.buildClusterSettings();
  }

  metricsProvider(): string {
    return this.context.config.getString('metrics.provider', undefined, { required: true }) as string;
  }

  buildCloudWatch(): void {
    const dashboardName = this.context.config.getString('metrics.cloudwatch.dashboard_name', undefined, {
      required: true,
    }) as string;
    // The dashboard body is empty.
    this.cloudwatchDashboard = new cloudwatch.Dashboard(this.stack, 'cloudwatch-dashboard', {
      dashboardName,
    });
  }

  buildAmazonManagedPrometheus(): void {
    const workspaceName = this.context.config.getString(
      'metrics.amazon_managed_prometheus.workspace_name',
      undefined,
      { required: true },
    ) as string;
    this.amazonPrometheusWorkspace = new aps.CfnWorkspace(this.stack, 'prometheus-workspace', {
      alias: workspaceName,
    });
    this.addCommonTags(this.amazonPrometheusWorkspace);
  }

  /** Validate and do nothing: `prometheus` provisions no resources. */
  buildPrometheus(): void {
    this.context.config.getString('metrics.prometheus.remote_write.url', undefined, { required: true });
    this.context.config.getString('metrics.prometheus.query.url', undefined, { required: true });
  }

  buildClusterSettings(): void {
    const clusterSettings: Record<string, unknown> = { deployment_id: this.deploymentId };
    const provider = this.metricsProvider();
    if (provider === METRICS_PROVIDER_CLOUDWATCH) {
      clusterSettings['cloudwatch.dashboard_arn'] = (this.cloudwatchDashboard as cloudwatch.Dashboard)
        .dashboardArn;
    } else if (provider === METRICS_PROVIDER_AMAZON_MANAGED_PROMETHEUS) {
      const workspace = this.amazonPrometheusWorkspace as aps.CfnWorkspace;
      clusterSettings['amazon_managed_prometheus.workspace_id'] = workspace.attrWorkspaceId;
      clusterSettings['amazon_managed_prometheus.workspace_arn'] = workspace.attrArn;
      clusterSettings['prometheus.remote_write.url'] = `${workspace.attrPrometheusEndpoint}api/v1/remote_write`;
      clusterSettings['prometheus.remote_read.url'] = `${workspace.attrPrometheusEndpoint}api/v1/query`;
    }
    this.updateClusterSettings(clusterSettings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new MetricsStack(props);
}
