/**
 * `DNSResolverEndpoint` and `DNSResolverRule` are plain holders that build L1 resources directly
 * under the caller's scope. `PrivateHostedZone` is a CDK construct.
 */

import type { Construct } from 'constructs';
import type * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53resolver from 'aws-cdk-lib/aws-route53resolver';

import type { IdeaContext } from './base.ts';
import { addCommonTags } from './base.ts';

export interface DNSResolverEndpointProps {
  securityGroupIds: string[];
  subnetIds: string[];
  /** Defaults to `OUTBOUND`. */
  direction?: string;
}

/**
 * `dns.py:DNSResolverEndpoint`. Construct id `<name>-dns-resolver-endpoint`; one `IpAddresses`
 * entry per subnet id, carrying the subnet only (the resolver picks the address).
 */
export class DNSResolverEndpoint {
  readonly resolverEndpoint: route53resolver.CfnResolverEndpoint;

  constructor(ctx: IdeaContext, name: string, scope: Construct, props: DNSResolverEndpointProps) {
    this.resolverEndpoint = new route53resolver.CfnResolverEndpoint(scope, `${name}-dns-resolver-endpoint`, {
      direction: props.direction ?? 'OUTBOUND',
      name,
      ipAddresses: props.subnetIds.map((subnetId) => ({ subnetId })),
      securityGroupIds: props.securityGroupIds,
    });
    addCommonTags(ctx, this.resolverEndpoint, name);
  }
}

export interface DNSResolverRuleProps {
  domainName: string;
  vpc: ec2.IVpc;
  resolverEndpointId: string;
  ipAddresses: string[];
  /** Defaults to `FORWARD`. */
  ruleType?: string;
  /** The AD caller passes the string `'53'`. */
  port?: string;
}

/**
 * `dns.py:DNSResolverRule`: the forward rule plus its VPC association. Construct ids
 * `<name>-dns-resolver-rule` and `<name>-dns-resolver-rule-association`; the rule's `Name`
 * property is the same `<name>-dns-resolver-rule` string as its construct id.
 *
 * `add_common_tags` is called on the association too, but `AWS::Route53Resolver::ResolverRuleAssociation`
 * has no tag property, so the aspect drops them and the live resource carries none.
 */
export class DNSResolverRule {
  readonly resolverRule: route53resolver.CfnResolverRule;
  readonly resolverRuleAssoc: route53resolver.CfnResolverRuleAssociation;

  constructor(ctx: IdeaContext, name: string, scope: Construct, props: DNSResolverRuleProps) {
    this.resolverRule = new route53resolver.CfnResolverRule(scope, `${name}-dns-resolver-rule`, {
      name: `${name}-dns-resolver-rule`,
      domainName: props.domainName,
      ruleType: props.ruleType ?? 'FORWARD',
      resolverEndpointId: props.resolverEndpointId,
      targetIps: props.ipAddresses.map((ip) => ({ ip, port: props.port })),
    });
    addCommonTags(ctx, this.resolverRule, name);

    this.resolverRuleAssoc = new route53resolver.CfnResolverRuleAssociation(
      scope,
      `${name}-dns-resolver-rule-association`,
      {
        resolverRuleId: this.resolverRule.attrResolverRuleId,
        vpcId: props.vpc.vpcId,
      },
    );
    addCommonTags(ctx, this.resolverRuleAssoc, name);
  }
}

/**
 * Construct id and tag name are both `<cluster>-private-hosted-zone`,
 * so `build_resource_name` prepends the cluster a second time and the `Name` tag reads
 * `<cluster>-<cluster>-private-hosted-zone`. Reproduce the doubled prefix.
 */
export class PrivateHostedZone extends route53.PrivateHostedZone {
  constructor(ctx: IdeaContext, scope: Construct, vpc: ec2.IVpc) {
    const name = `${ctx.clusterName}-private-hosted-zone`;
    super(scope, name, {
      vpc,
      comment: `Private Hosted Zone for IDEA Cluster: ${ctx.clusterName}`,
      zoneName: ctx.config.getString('cluster.route53.private_hosted_zone_name', undefined, {
        required: true,
      }) as string,
    });
    addCommonTags(ctx, this, name);
  }
}
