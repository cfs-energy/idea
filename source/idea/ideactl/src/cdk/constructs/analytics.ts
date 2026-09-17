/**
 * `OpenSearch` extends the L2 directly. Defaults apply only when the caller provides no value,
 * and are load-bearing for the template.
 */

import { RemovalPolicy } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import type * as kms from 'aws-cdk-lib/aws-kms';
import * as opensearch from 'aws-cdk-lib/aws-opensearchservice';

import { ArnBuilder } from '../../config/arn-builder.ts';
import { isEmpty } from '../../config/cluster-config.ts';
import type { IdeaContext } from './base.ts';
import { addCommonTags, addNagSuppression, resourceName } from './base.ts';
import type { ExistingSocaCluster } from './existing-resources.ts';

export interface OpenSearchProps {
  cluster: ExistingSocaCluster;
  securityGroups: ec2.ISecurityGroup[];
  dataNodes: number;
  dataNodeInstanceType: string;
  ebsVolumeSize: number;
  /** Defaults to `DESTROY` when omitted. */
  removalPolicy?: RemovalPolicy;
  version?: opensearch.EngineVersion;
  /** Defaults to `true`. */
  createServiceLinkedRole?: boolean;
  accessPolicies?: iam.PolicyStatement[];
  advancedOptions?: Record<string, string>;
  automatedSnapshotStartHour?: number;
  capacity?: opensearch.CapacityConfig;
  cognitoDashboardsAuth?: opensearch.CognitoOptions;
  customEndpoint?: opensearch.CustomEndpointOptions;
  domainName?: string;
  ebs?: opensearch.EbsOptions;
  enableVersionUpgrade?: boolean;
  encryptionAtRest?: opensearch.EncryptionAtRestOptions;
  /** Holds an `IKey` used only by `encryptionAtRest`. */
  kmsKeyArn?: kms.IKey;
  enforceHttps?: boolean;
  fineGrainedAccessControl?: opensearch.AdvancedSecurityOptions;
  logging?: opensearch.LoggingOptions;
  nodeToNodeEncryption?: boolean;
  tlsSecurityPolicy?: opensearch.TLSSecurityPolicy;
  useUnsignedBasicAuth?: boolean;
  vpcSubnets?: ec2.SubnetSelection[];
  zoneAwareness?: opensearch.ZoneAwarenessConfig;
}

/** Applies defaults before calling the L2 constructor. */
function domainProps(ctx: IdeaContext, name: string, props: OpenSearchProps): opensearch.DomainProps {
  const dataNodes = props.dataNodes;

  let zoneAwareness = props.zoneAwareness;
  if (zoneAwareness === undefined) {
    zoneAwareness =
      dataNodes > 1
        ? { enabled: true, availabilityZoneCount: Math.min(3, dataNodes) }
        : { enabled: false };
  }

  const domainName = props.domainName ?? resourceName(ctx, name).toLowerCase();

  let accessPolicies = props.accessPolicies;
  if (accessPolicies === undefined) {
    const arnBuilder = new ArnBuilder(ctx.config);
    accessPolicies = [
      new iam.PolicyStatement({
        principals: [new iam.AnyPrincipal()],
        actions: ['es:ESHttp*'],
        resources: [arnBuilder.getArn('es', `domain/${domainName}/*`)],
      }),
    ];
  }

  return {
    version: props.version ?? opensearch.EngineVersion.OPENSEARCH_2_19,
    accessPolicies,
    advancedOptions: props.advancedOptions ?? { 'rest.action.multi.allow_explicit_index': 'true' },
    // 0 in every deployment, and CDK drops `SnapshotOptions` because 0 is falsy. Do not "fix" it.
    automatedSnapshotStartHour: props.automatedSnapshotStartHour ?? 0,
    capacity: props.capacity ?? {
      dataNodeInstanceType: props.dataNodeInstanceType,
      dataNodes,
    },
    cognitoDashboardsAuth: props.cognitoDashboardsAuth,
    customEndpoint: props.customEndpoint,
    domainName,
    ebs: props.ebs ?? {
      volumeSize: props.ebsVolumeSize,
      volumeType: ec2.EbsDeviceVolumeType.GP3,
    },
    enableVersionUpgrade: props.enableVersionUpgrade,
    encryptionAtRest: props.encryptionAtRest ?? { enabled: true, kmsKey: props.kmsKeyArn },
    enforceHttps: props.enforceHttps ?? true,
    fineGrainedAccessControl: props.fineGrainedAccessControl,
    logging: props.logging,
    nodeToNodeEncryption: props.nodeToNodeEncryption,
    removalPolicy: props.removalPolicy ?? RemovalPolicy.DESTROY,
    securityGroups: props.securityGroups,
    tlsSecurityPolicy: props.tlsSecurityPolicy,
    useUnsignedBasicAuth: props.useUnsignedBasicAuth,
    vpc: props.cluster.vpc,
    vpcSubnets: props.vpcSubnets ?? [{ subnets: props.cluster.privateSubnets.slice(0, dataNodes) }],
    zoneAwareness,
  };
}

export class OpenSearch extends opensearch.Domain {
  constructor(ctx: IdeaContext, name: string, scope: Construct, props: OpenSearchProps) {
    super(scope, name, domainProps(ctx, name, props));

    addCommonTags(ctx, this, name);

    if (props.createServiceLinkedRole !== false) {
      let awsServiceName = ctx.config.getString('global-settings.opensearch.aws_service_name');
      if (isEmpty(awsServiceName)) {
        const dnsSuffix = ctx.config.getString('cluster.aws.dns_suffix', undefined, {
          required: true,
        }) as string;
        awsServiceName = `es.${dnsSuffix}`;
      }
      // DO NOT CHANGE THE DESCRIPTION OF THE ROLE: it is what AWS matches an existing SLR against.
      const serviceLinkedRole = new iam.CfnServiceLinkedRole(
        this,
        resourceName(ctx, 'es-service-linked-role'),
        {
          awsServiceName,
          description: 'Role for ES to access resources in the VPC',
        },
      );
      this.node.addDependency(serviceLinkedRole);
    }

    addNagSuppression(this, [
      {
        rule_id: 'AwsSolutions-OS3',
        reason: 'Access to OpenSearch cluster is restricted within a VPC',
      },
      {
        rule_id: 'AwsSolutions-OS4',
        reason:
          'Use existing resources flow to provision an even more scalable OpenSearch cluster with dedicated master nodes',
      },
      {
        rule_id: 'AwsSolutions-OS5',
        reason: 'Access to OpenSearch cluster is restricted within a VPC',
      },
    ]);
  }
}
