/**
 * Two values are regenerated on every synth: the `UpdateToken` that makes the
 * private-IP custom resource re-run, and the uuid tail of the dashboard target group name, which
 * replaces that target group on every deploy so the endpoints lambda is re-pointed at a fresh one.
 *
 * `analytics.opensearch.use_existing`, the GovCloud Kinesis branches, and the service-linked-role
 * branch require their respective configuration conditions.
 */

import { randomUUID } from 'node:crypto';

import {
  CfnDeletionPolicy,
  CfnOutput,
  CustomResource as CdkCustomResource,
  Fn,
  RemovalPolicy,
} from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { KinesisEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as opensearch from 'aws-cdk-lib/aws-opensearchservice';

import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { IdeaCodeAsset } from '../code-asset.ts';
import { OpenSearch } from '../constructs/analytics.ts';
import { kmsKeyArn } from '../constructs/base.ts';
import {
  CustomResourceProvider,
  KinesisStream,
  LambdaFunction,
  Policy,
  Role,
} from '../constructs/common.ts';
import { ExistingSocaCluster, lookupExistingOpensearch } from '../constructs/existing-resources.ts';
import { OpenSearchSecurityGroup } from '../constructs/network.ts';

export const MODULE_ANALYTICS = 'analytics';

/** `constants.CAVEATS['KINESIS_STREAMS_CLOUDFORMATION_UNSUPPORTED_STREAMMODEDETAILS_REGION_LIST']`. */
export const KINESIS_STREAM_MODE_UNSUPPORTED_REGIONS = ['us-gov-east-1', 'us-gov-west-1'];

/** `RemovalPolicy` lookup uses member names, not enum values. */
function removalPolicyByName(name: string): RemovalPolicy {
  if (!Object.prototype.hasOwnProperty.call(RemovalPolicy, name)) {
    throw new Error(`'${name}' is not a valid RemovalPolicy`);
  }
  return RemovalPolicy[name as keyof typeof RemovalPolicy];
}

export class AnalyticsStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  securityGroup: ec2.ISecurityGroup | undefined;
  opensearch!: opensearch.IDomain;
  kinesisStream!: KinesisStream;

  /**
   * `serviceLinkedRoleExists` is the answer to the `iam:ListRoles` probe. `buildStack` resolves
   * it before constructing the stack.
   */
  constructor(
    props: StackBuildProps,
    serviceLinkedRoleExists: boolean,
    existingDomainDataNodes?: number,
  ) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    this.cluster = new ExistingSocaCluster(this.context, this.stack);

    this.buildSecurityGroup();

    if (this.context.config.getBool('analytics.opensearch.use_existing', false)) {
      this.opensearch = lookupExistingOpensearch(this.context, this.stack);
      this.buildDashboardEndpoints(existingDomainDataNodes);
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-KDS3',
            reason: 'Kinesis Data Stream is encrypted with customer-managed KMS key',
          },
        ],
        this.stack,
      );
    } else {
      this.buildOpenSearch(!serviceLinkedRoleExists);
      this.buildDashboardEndpoints();
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-IAM5',
            reason: 'CDK L2 construct does not support custom LogGroup permissions',
          },
          { rule_id: 'AwsSolutions-IAM4', reason: 'Usage is required for Service Linked Role' },
          {
            rule_id: 'AwsSolutions-L1',
            reason: 'CDK L2 construct does not offer options to customize the Lambda runtime',
          },
          {
            rule_id: 'AwsSolutions-KDS3',
            reason: 'Kinesis Data Stream is encrypted with customer-managed KMS key',
          },
        ],
        this.stack,
      );
      if (this.dataNodes() === 1) {
        this.addNagSuppression(
          [
            {
              rule_id: 'AwsSolutions-OS7',
              reason: 'OpenSearch domain has 1 data node disabling Zone Awareness',
            },
          ],
          this.stack,
        );
      }
    }

    this.buildAnalyticsInputStream();
    this.buildClusterSettings();
  }

  dataNodes(): number {
    return this.context.config.getInt('analytics.opensearch.data_nodes', 0, { required: true });
  }

  buildSecurityGroup(): void {
    this.securityGroup = new OpenSearchSecurityGroup(
      this.context,
      `${this.moduleId}-opensearch-security-group`,
      this.stack,
      this.cluster.vpc,
    );
  }

  buildOpenSearch(createServiceLinkedRole: boolean): void {
    const config = this.context.config;
    const dataNodes = this.dataNodes();
    const dataNodeInstanceType = config.getString('analytics.opensearch.data_node_instance_type', '', {
      required: true,
    });
    const ebsVolumeSize = config.getInt('analytics.opensearch.ebs_volume_size', 0, { required: true });
    const nodeToNodeEncryption = config.getBool('analytics.opensearch.node_to_node_encryption', false, {
      required: true,
    });
    const removalPolicy = config.getString('analytics.opensearch.removal_policy', '', { required: true });
    const appLogRemovalPolicy = config.getString('analytics.opensearch.logging.app_log_removal_policy', 'DESTROY');
    const searchLogRemovalPolicy = config.getString(
      'analytics.opensearch.logging.search_log_removal_policy',
      'DESTROY',
    );
    const slowIndexLogRemovalPolicy = config.getString(
      'analytics.opensearch.logging.slow_index_log_removal_policy',
      'DESTROY',
    );

    const kmsKeyId = config.getString('analytics.opensearch.kms_key_id');
    const encryptionKey =
      kmsKeyId === undefined
        ? undefined
        : kms.Key.fromKeyArn(this.stack, 'opensearch-kms-key', kmsKeyArn(this.context, kmsKeyId));

    // The logging options construct log groups before the domain.
    const logging: opensearch.LoggingOptions = {
      slowSearchLogEnabled: config.getBool('analytics.opensearch.logging.slow_search_log_enabled', false, {
        required: true,
      }),
      slowSearchLogGroup: new logs.LogGroup(this.stack, 'analytics-search-log-group', {
        logGroupName: `/${this.clusterName}/${this.moduleId}/search-log`,
        removalPolicy: removalPolicyByName(searchLogRemovalPolicy),
      }),
      appLogEnabled: config.getBool('analytics.opensearch.logging.app_log_enabled', false, { required: true }),
      appLogGroup: new logs.LogGroup(this.stack, 'analytics-app-log-group', {
        logGroupName: `/${this.clusterName}/${this.moduleId}/app-log`,
        removalPolicy: removalPolicyByName(appLogRemovalPolicy),
      }),
      slowIndexLogEnabled: config.getBool('analytics.opensearch.logging.slow_index_log_enabled', false, {
        required: true,
      }),
      slowIndexLogGroup: new logs.LogGroup(this.stack, 'analytics-slow-index-log-group', {
        logGroupName: `/${this.clusterName}/${this.moduleId}/slow-index-log`,
        removalPolicy: removalPolicyByName(slowIndexLogRemovalPolicy),
      }),
      // Audit logs need fine-grained access control; provision the domain manually and import it
      // through the use-existing flow to turn them on.
      auditLogEnabled: false,
    };

    // The construct id is the literal `analytics`, not the module id: a cluster with a custom
    // analytics module id still names this construct (and therefore its logical id) `analytics`.
    this.opensearch = new OpenSearch(this.context, MODULE_ANALYTICS, this.stack, {
      cluster: this.cluster,
      securityGroups: [this.securityGroup as ec2.ISecurityGroup],
      dataNodes,
      dataNodeInstanceType,
      ebsVolumeSize,
      removalPolicy: removalPolicyByName(removalPolicy),
      nodeToNodeEncryption,
      kmsKeyArn: encryptionKey,
      createServiceLinkedRole,
      logging,
    });
  }

  buildDashboardEndpoints(existingDomainDataNodes?: number): void {
    const config = this.context.config;
    const clusterEndpointsLambdaArn = config.getString('cluster.cluster_endpoints_lambda_arn', '', {
      required: true,
    });
    const externalHttpsListenerArn = config.getString(
      'cluster.load_balancers.external_alb.https_listener_arn',
      '',
      { required: true },
    );
    const pathPatterns = config.getList<string>('analytics.opensearch.endpoints.external.path_patterns', [], {
      required: true,
    });
    const priority = config.getInt('analytics.opensearch.endpoints.external.priority', 0, { required: true });

    let domainName = this.opensearch.domainName;
    let dataNodes: number;
    if (config.getBool('analytics.opensearch.use_existing', false)) {
      // The import reads the name off the endpoint, which carries a `vpc-` prefix that
      // `describe_domain` rejects. The stripped name is what the custom resource gets too.
      if (domainName.startsWith('vpc-')) domainName = domainName.replace('vpc-', '');
      if (existingDomainDataNodes === undefined) {
        throw new Error(`no opensearch:DescribeDomain answer for ${domainName}`);
      }
      dataNodes = existingDomainDataNodes;
    } else {
      dataNodes = this.dataNodes();
    }

    const opensearchPrivateIps = new CustomResourceProvider(
      this.context,
      'opensearch-private-ips',
      this.stack,
      {
        ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_opensearch_private_ips'),
        lambdaTimeoutSeconds: 180,
        policyTemplateName: 'custom-resource-opensearch-private-ips.yml',
        resourceType: 'OpenSearchPrivateIPAddresses',
      },
    ).invoke('opensearch-private-ips', {
      DomainName: domainName,
      // Regenerated on every synth so the custom resource re-reads the domain's ENIs on deploy.
      UpdateToken: randomUUID(),
    });

    const ipAddresses = opensearchPrivateIps.getAttString('IpAddresses');
    const targets: elbv2.CfnTargetGroup.TargetDescriptionProperty[] = [];
    for (let i = 0; i < dataNodes; i += 1) {
      targets.push({ id: Fn.select(i, Fn.split(',', ipAddresses)) });
    }

    // The uuid tail replaces the target group on every deploy and updates the endpoint rule.
    const deploymentId = randomUUID();
    const dashboardTargetGroup = new elbv2.CfnTargetGroup(
      this.stack,
      `${this.clusterName}-dashboard-target-group`,
      {
        port: 443,
        protocol: 'HTTPS',
        targetType: 'ip',
        vpcId: this.cluster.vpc.vpcId,
        name: `${this.getTargetGroupName('dashboard')}-${deploymentId}`.slice(0, 32),
        targets,
        healthCheckPath: '/',
      },
    );
    dashboardTargetGroup.node.addDependency(opensearchPrivateIps);

    new CfnOutput(this.stack, 'IPAddresses', { value: ipAddresses });
    new CfnOutput(this.stack, 'NumberOfTargets', { value: String(targets.length) });

    new CdkCustomResource(this.stack, 'dashboard-endpoint', {
      serviceToken: clusterEndpointsLambdaArn,
      properties: {
        endpoint_name: `${this.moduleId}-dashboard-endpoint`,
        listener_arn: externalHttpsListenerArn,
        priority,
        target_group_arn: dashboardTargetGroup.ref,
        conditions: [{ Field: 'path-pattern', Values: pathPatterns }],
        actions: [{ Type: 'forward', TargetGroupArn: dashboardTargetGroup.ref }],
        tags: {
          'idea:ClusterName': this.clusterName,
          'idea:ModuleId': this.moduleId,
          'idea:ModuleName': MODULE_ANALYTICS,
        },
      },
      resourceType: 'Custom::DashboardEndpointExternal',
    });
  }

  buildAnalyticsInputStream(): void {
    const config = this.context.config;
    const streamConfig = config.getString('analytics.kinesis.stream_mode', '', { required: true });
    if (streamConfig !== 'PROVISIONED' && streamConfig !== 'ON_DEMAND') {
      throw new Error('analytics.kinesis.stream_mode needs to be one of PROVISIONED or ON_DEMAND only');
    }
    const streamMode =
      streamConfig === 'PROVISIONED' ? kinesis.StreamMode.PROVISIONED : kinesis.StreamMode.ON_DEMAND;
    const shardCount =
      streamConfig === 'PROVISIONED'
        ? config.getInt('analytics.kinesis.shard_count', 0, { required: true })
        : undefined;

    this.kinesisStream = new KinesisStream(this.context, `${this.moduleId}-kinesis-stream`, this.stack, {
      streamName: `${this.moduleId}-kinesis-stream`,
      streamMode,
      shardCount,
      removalPolicy: removalPolicyByName(config.getString('analytics.kinesis.removal_policy', 'DESTROY')),
    });
    if (KINESIS_STREAM_MODE_UNSUPPORTED_REGIONS.includes(this.awsRegion)) {
      (this.kinesisStream.node.defaultChild as kinesis.CfnStream).addPropertyDeletionOverride(
        'StreamModeDetails',
      );
    }

    const lambdaName = `${this.moduleId}-sink-lambda`;
    const streamProcessingLambdaRole = new Role(this.context, `${lambdaName}-role`, this.stack, {
      description: `Role for ${lambdaName} function for Cluster: ${this.clusterName}`,
      assumedBy: ['lambda'],
    });
    streamProcessingLambdaRole.attachInlinePolicy(
      new Policy(this.context, `${lambdaName}-policy`, this.stack, {
        policyTemplateName: 'analytics-sink-lambda.yml',
      }),
    );

    const streamProcessingLambda = new LambdaFunction(this.context, lambdaName, this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_analytics_sink'),
      description: 'Lambda to process analytics-kinesis-stream data',
      timeoutSeconds: 900,
      securityGroups: [this.securityGroup as ec2.ISecurityGroup],
      role: streamProcessingLambdaRole,
      environment: { opensearch_endpoint: this.opensearch.domainEndpoint },
      vpc: this.cluster.vpc,
      vpcSubnets: { subnets: this.cluster.privateSubnets },
    });
    this.addNagSuppression(
      [{ rule_id: 'AwsSolutions-L1', reason: 'Python Runtime is selected for stability.' }],
      streamProcessingLambda,
    );

    if (this.awsRegion.startsWith('us-gov-')) {
      // GovCloud rejects the tags the L2 event source puts on the mapping, so it is built by hand.
      // The L2 is skipped entirely, which also means the role's `DefaultPolicy` never exists there
      // and the function runs on the inline `analytics-sink-lambda.yml` policy alone.
      const eventSourceMapping = new lambda.CfnEventSourceMapping(this.stack, `${lambdaName}-event-source`, {
        functionName: streamProcessingLambda.functionName,
        eventSourceArn: this.kinesisStream.streamArn,
        startingPosition: 'LATEST',
        batchSize: 100,
        tags: [],
      });
      eventSourceMapping.cfnOptions.deletionPolicy = CfnDeletionPolicy.DELETE;
      eventSourceMapping.cfnOptions.updateReplacePolicy = CfnDeletionPolicy.DELETE;
      eventSourceMapping.addDependency(streamProcessingLambda.node.defaultChild as lambda.CfnFunction);
    } else {
      streamProcessingLambda.addEventSource(
        new KinesisEventSource(this.kinesisStream, {
          batchSize: 100,
          startingPosition: lambda.StartingPosition.LATEST,
        }),
      );
    }
  }

  buildClusterSettings(): void {
    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      'opensearch.domain_name': this.opensearch.domainName,
      'opensearch.domain_arn': this.opensearch.domainArn,
      'opensearch.domain_endpoint': this.opensearch.domainEndpoint,
      'opensearch.dashboard_endpoint': `${this.opensearch.domainEndpoint}/_dashboards`,
      'kinesis.stream_name': this.kinesisStream.streamName,
      'kinesis.stream_arn': this.kinesisStream.streamArn,
    };
    if (this.securityGroup !== undefined) {
      clusterSettings['opensearch.security_group_id'] = this.securityGroup.securityGroupId;
    }
    this.updateClusterSettings(clusterSettings);
  }
}

/** `/aws-service-role/es.<suffix>` then `/aws-service-role/opensearchservice.<suffix>`. */
export function serviceLinkedRolePathPrefixes(dnsSuffix: string): string[] {
  return [`/aws-service-role/es.${dnsSuffix}`, `/aws-service-role/opensearchservice.${dnsSuffix}`];
}

/** Matches the OpenSearch L2 endpoint parser before the domain read occurs. */
export function domainNameFromEndpoint(endpoint: string): string {
  const hostname = new URL(`https://${endpoint}`).hostname;
  const domain = hostname.split(".")[0];
  const components = domain.split("-");
  const suffix = `-${components[components.length - 1]}`;
  return domain.split(suffix)[0];
}

/** Reads the deployed service-linked role or existing domain before building the construct tree. */
export async function buildStack(props: StackBuildProps): Promise<void> {
  const dnsSuffix = props.ctx.config.getString('cluster.aws.dns_suffix', '', { required: true });
  const pathPrefixes = serviceLinkedRolePathPrefixes(dnsSuffix);
  const useExisting = props.ctx.config.getBool('analytics.opensearch.use_existing', false);

  if (useExisting) {
    const endpoint = props.ctx.config.getString('analytics.opensearch.domain_vpc_endpoint_url', '', {
      required: true,
    });
    let domainName = domainNameFromEndpoint(endpoint);
    if (domainName.startsWith('vpc-')) domainName = domainName.replace('vpc-', '');
    const domain = await props.ctx.synthReads.describeDomain(domainName);
    const instanceCount = domain.ClusterConfig?.InstanceCount;
    if (instanceCount === undefined) {
      throw new Error(`no opensearch:DescribeDomain answer for ${domainName}`);
    }
    new AnalyticsStack(props, false, instanceCount);
    return;
  }

  let count = 0;
  for (const pathPrefix of pathPrefixes) {
    count += (await props.ctx.synthReads.listServiceLinkedRoles(pathPrefix)).length;
  }
  new AnalyticsStack(props, count > 0);
}
