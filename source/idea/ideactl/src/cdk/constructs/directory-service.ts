/**
 * All four classes are plain holders. They create no wrapper resource, and every resource lands
 * directly under the caller's scope.
 */

import { randomUUID } from 'node:crypto';

import { Duration, Fn, RemovalPolicy, SecretValue, Tags } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ds from 'aws-cdk-lib/aws-directoryservice';
import type * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

import { IdeaCodeAsset } from '../code-asset.ts';
import { isEmpty } from '../../config/cluster-config.ts';
import type { IdeaContext, IdeaNagSuppression } from './base.ts';
import { IDEA_TAG_MODULE_NAME, addCommonTags, addNagSuppression, resourceName } from './base.ts';
import { CustomResourceProvider } from './common.ts';
import { DNSResolverEndpoint, DNSResolverRule } from './dns.ts';
import type { ExistingSocaCluster } from './existing-resources.ts';

/** `constants.MODULE_DIRECTORYSERVICE`. */
export const MODULE_DIRECTORYSERVICE = 'directoryservice';
/** `constants.GROUP_TYPE_CLUSTER`. */
const GROUP_TYPE_CLUSTER = 'cluster';

const SECRET_ROTATION_SUPPRESSION = (reason: string): IdeaNagSuppression[] => [
  { rule_id: 'AwsSolutions-SMG4', reason },
];

// --- credentials ------------------------------------------------------------------------------

/**
 * `DirectoryServiceCredentials`: the root username/password secrets, created once at cluster
 * creation. Construct ids are `<provider>-admin-username` / `<provider>-admin-password`, so the
 * provider name is part of the logical ID. When `directoryservice.root_credentials_provided` is
 * true nothing is created and the ARNs come from config instead.
 *
 * The secrets carry only the `idea:ModuleName` tag (access is granted by tag) and **no** removal
 * policy, so CloudFormation's default applies.
 */
export class DirectoryServiceCredentials {
  readonly ctx: IdeaContext;
  readonly credentialsProvided: boolean;
  readonly adminUsername: secretsmanager.CfnSecret | undefined;
  readonly adminPassword: secretsmanager.CfnSecret | undefined;

  constructor(ctx: IdeaContext, _name: string, scope: Construct, adminUsername: string, adminPassword?: string) {
    this.ctx = ctx;
    this.credentialsProvided = ctx.config.getBool('directoryservice.root_credentials_provided', false);
    if (this.credentialsProvided) return;

    const kmsKeyId = ctx.config.getString('cluster.secretsmanager.kms_key_id');
    const provider = ctx.config.getString('directoryservice.provider', undefined, {
      required: true,
    }) as string;

    const adminUsernameKey = `${provider}-admin-username`;
    this.adminUsername = new secretsmanager.CfnSecret(scope, adminUsernameKey, {
      description: `${provider} Root Username, Cluster: ${ctx.clusterName}`,
      kmsKeyId,
      name: resourceName(ctx, adminUsernameKey),
      secretString: adminUsername,
    });
    Tags.of(this.adminUsername).add(IDEA_TAG_MODULE_NAME, MODULE_DIRECTORYSERVICE);

    const adminPasswordKey = `${provider}-admin-password`;
    this.adminPassword = new secretsmanager.CfnSecret(scope, adminPasswordKey, {
      description: `${provider} Root Password, Cluster: ${ctx.clusterName}`,
      kmsKeyId,
      name: resourceName(ctx, adminPasswordKey),
      ...(adminPassword === undefined || adminPassword.trim() === ''
        ? { generateSecretString: { excludeCharacters: '$@;"\\\'', passwordLength: 16 } }
        : { secretString: adminPassword }),
    });
    Tags.of(this.adminPassword).add(IDEA_TAG_MODULE_NAME, MODULE_DIRECTORYSERVICE);

    const suppressions = SECRET_ROTATION_SUPPRESSION(
      'Secret rotation not applicable for DirectoryService credentials.',
    );
    addNagSuppression(this.adminUsername, suppressions);
    addNagSuppression(this.adminPassword, suppressions);
  }

  getUsernameSecretArn(): string {
    if (this.credentialsProvided) {
      return this.ctx.config.getString('directoryservice.root_username_secret_arn', undefined, {
        required: true,
      }) as string;
    }
    return (this.adminUsername as secretsmanager.CfnSecret).ref;
  }

  getPasswordSecretArn(): string {
    if (this.credentialsProvided) {
      return this.ctx.config.getString('directoryservice.root_password_secret_arn', undefined, {
        required: true,
      }) as string;
    }
    return (this.adminPassword as secretsmanager.CfnSecret).ref;
  }
}

/**
 * `OAuthClientIdAndSecret`: `<prefix>-client-id` / `<prefix>-client-secret` secrets, tagged with
 * the module name that is allowed to read them. Both get `RemovalPolicy.DESTROY`, which on an L1
 * sets `DeletionPolicy` **and** `UpdateReplacePolicy` to `Delete`.
 */
export class OAuthClientIdAndSecret {
  readonly clientId: secretsmanager.CfnSecret;
  readonly clientSecret: secretsmanager.CfnSecret;

  constructor(
    ctx: IdeaContext,
    secretNamePrefix: string,
    moduleName: string,
    scope: Construct,
    clientId: string,
    clientSecret: string,
  ) {
    const kmsKeyId = ctx.config.getString('cluster.secretsmanager.kms_key_id');

    const clientIdKey = `${secretNamePrefix}-client-id`;
    this.clientId = new secretsmanager.CfnSecret(scope, clientIdKey, {
      description: `${secretNamePrefix} ClientId, Cluster: ${ctx.clusterName}`,
      kmsKeyId,
      name: resourceName(ctx, clientIdKey),
      secretString: clientId,
    });
    this.clientId.applyRemovalPolicy(RemovalPolicy.DESTROY);
    Tags.of(this.clientId).add(IDEA_TAG_MODULE_NAME, moduleName);

    const clientSecretKey = `${secretNamePrefix}-client-secret`;
    this.clientSecret = new secretsmanager.CfnSecret(scope, clientSecretKey, {
      description: `${secretNamePrefix} ClientSecret, Cluster: ${ctx.clusterName}`,
      kmsKeyId,
      name: resourceName(ctx, clientSecretKey),
      secretString: clientSecret,
    });
    this.clientSecret.applyRemovalPolicy(RemovalPolicy.DESTROY);
    Tags.of(this.clientSecret).add(IDEA_TAG_MODULE_NAME, moduleName);

    const suppressions = SECRET_ROTATION_SUPPRESSION(
      'Secret rotation not applicable for OAuth 2.0 ClientId/Secret',
    );
    addNagSuppression(this.clientId, suppressions);
    addNagSuppression(this.clientSecret, suppressions);
  }
}

// --- AWS Managed Microsoft AD -----------------------------------------------------------------

export interface ActiveDirectoryProps {
  cluster: ExistingSocaCluster;
  subnets?: ec2.ISubnet[];
  /** Defaults to `false`. */
  enableSso?: boolean;
}

/**
 * `ActiveDirectory`: the credentials pair, the `AWS::DirectoryService::MicrosoftAD` (construct id
 * = `name`), and the DNS forwarding chain, a `Custom::ADSecurityGroupId` lookup, an outbound
 * resolver endpoint in the *same two* launch subnets, and a FORWARD rule to the AD's first two DNS
 * addresses on port `'53'` (a string).
 *
 * The AD launches into the **first two** subnets of the list, in config order.
 */
export class ActiveDirectory {
  readonly ctx: IdeaContext;
  readonly name: string;
  readonly credentials: DirectoryServiceCredentials;
  readonly launchSubnets: string[];
  readonly adName: string;
  readonly adShortName: string;
  readonly adEdition: string;
  readonly ad: ds.CfnMicrosoftAD;

  constructor(ctx: IdeaContext, name: string, scope: Construct, props: ActiveDirectoryProps) {
    this.ctx = ctx;
    this.name = name;

    this.credentials = new DirectoryServiceCredentials(
      ctx,
      'ds-activedirectory-credentials',
      scope,
      'Admin',
    );

    this.adName = ctx.config.getString('directoryservice.name', undefined, { required: true }) as string;
    this.adShortName = ctx.config.getString('directoryservice.ad_short_name', undefined, {
      required: true,
    }) as string;
    this.adEdition = ctx.config.getString('directoryservice.ad_edition', undefined, {
      required: true,
    }) as string;

    const subnets = props.subnets ?? props.cluster.privateSubnets;
    this.launchSubnets = subnets.slice(0, 2).map((subnet) => subnet.subnetId);

    this.ad = new ds.CfnMicrosoftAD(scope, name, {
      name: this.adName,
      password: SecretValue.secretsManager(this.credentials.getPasswordSecretArn()).toString(),
      vpcSettings: { subnetIds: this.launchSubnets, vpcId: props.cluster.vpc.vpcId },
      edition: this.adEdition,
      enableSso: props.enableSso ?? false,
      shortName: this.adShortName,
    });
    addCommonTags(ctx, this.ad, name);

    this.buildDnsResolver(scope, props.cluster);
  }

  private buildDnsResolver(scope: Construct, cluster: ExistingSocaCluster): void {
    const getAdSecurityGroupResult = new CustomResourceProvider(
      this.ctx,
      'get-ad-security-group-id',
      scope,
      {
        ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_get_ad_security_group'),
        lambdaTimeoutSeconds: 15,
        policyTemplateName: 'custom-resource-get-ad-security-group.yml',
        resourceType: 'ADSecurityGroupId',
      },
    ).invoke(resourceName(this.ctx, this.name), { DirectoryId: this.ad.ref });

    const endpoint = new DNSResolverEndpoint(this.ctx, this.ctx.clusterName, scope, {
      subnetIds: this.launchSubnets,
      securityGroupIds: [getAdSecurityGroupResult.getAttString('SecurityGroupId')],
    });

    new DNSResolverRule(this.ctx, this.name, scope, {
      domainName: this.adName,
      vpc: cluster.vpc,
      resolverEndpointId: endpoint.resolverEndpoint.attrResolverEndpointId,
      ipAddresses: [Fn.select(0, this.ad.attrDnsIpAddresses), Fn.select(1, this.ad.attrDnsIpAddresses)],
      port: '53',
    });
  }
}

// --- Cognito user pool ------------------------------------------------------------------------

/** `GroupNameHelper.get_cluster_{administrators,managers}_group`. */
function clusterGroupName(ctx: IdeaContext, key: string): string {
  const groupName = ctx.config.getString(key, undefined, { required: true }) as string;
  return groupName.endsWith(`${GROUP_TYPE_CLUSTER}-group`)
    ? groupName
    : `${groupName}-${GROUP_TYPE_CLUSTER}-group`;
}

/**
 * `UserPool`: the pool, its two cluster-wide groups and its Cognito domain.
 *
 * Every `if <prop> is None` default below is what the live pool contains; the identity-provider
 * stack only passes `removalPolicy`, `userInvitation` and `lambdaTriggers`.
 *
 * Advanced security is disabled, so no `UserPoolAddOns` is emitted regardless of
 * `identity-provider.cognito.advanced_security_mode`.
 *
 * When `identity-provider.cognito.domain_url` is empty the domain prefix is a **fresh uuid** on
 * every synth, which replaces the live Cognito domain, the caller is expected to keep the key.
 */
export class UserPool {
  readonly userPool: cognito.UserPool;
  readonly domain: cognito.UserPoolDomain;
  readonly secrets: OAuthClientIdAndSecret[] = [];

  constructor(ctx: IdeaContext, name: string, scope: Construct, props: cognito.UserPoolProps = {}) {
    const userPoolName = props.userPoolName ?? `${ctx.clusterName}-user-pool`;

    this.userPool = new cognito.UserPool(scope, userPoolName, {
      accountRecovery: props.accountRecovery ?? cognito.AccountRecovery.EMAIL_ONLY,
      autoVerify: props.autoVerify ?? { email: true, phone: false },
      customAttributes: props.customAttributes ?? {
        cluster_name: new cognito.StringAttribute({ mutable: true }),
        aws_region: new cognito.StringAttribute({ mutable: true }),
        password_last_set: new cognito.NumberAttribute({ mutable: true }),
        password_max_age: new cognito.NumberAttribute({ mutable: true }),
      },
      customSenderKmsKey: props.customSenderKmsKey,
      deletionProtection: true,
      deviceTracking: props.deviceTracking,
      email: props.email,
      enableSmsRole: props.enableSmsRole,
      lambdaTriggers: props.lambdaTriggers,
      mfa: props.mfa ?? cognito.Mfa.OPTIONAL,
      mfaMessage: props.mfaMessage,
      mfaSecondFactor: props.mfaSecondFactor ?? { otp: true, sms: false },
      passwordPolicy: props.passwordPolicy ?? {
        minLength: 8,
        requireDigits: true,
        requireLowercase: true,
        requireSymbols: true,
        requireUppercase: true,
        tempPasswordValidity: Duration.days(7),
      },
      removalPolicy: props.removalPolicy ?? RemovalPolicy.DESTROY,
      selfSignUpEnabled: props.selfSignUpEnabled ?? false,
      signInAliases: props.signInAliases ?? {
        username: true,
        preferredUsername: false,
        phone: false,
        email: true,
      },
      signInCaseSensitive: props.signInCaseSensitive ?? false,
      smsRole: props.smsRole,
      smsRoleExternalId: props.smsRoleExternalId,
      standardAttributes: props.standardAttributes ?? {
        email: { mutable: true, required: true },
      },
      userInvitation: props.userInvitation ?? {
        emailSubject: `(${ctx.clusterName}) Your IDEA Account`,
        emailBody:
          '\n                Hello <b>{username}</b>,\n' +
          '                <br/><br/>\n' +
          `                You have been invited to join the ${ctx.clusterName} cluster.\n` +
          '                <br/>\n' +
          '                Your temporary password is <b>{####}</b>\n' +
          '                ',
      },
      userPoolName,
      userVerification: props.userVerification,
    });
    addCommonTags(ctx, this.userPool, name);

    addNagSuppression(this.userPool, [
      {
        rule_id: 'AwsSolutions-COG2',
        reason: 'Suppress MFA warning. MFA provided by customer IdP/SSO methods.',
      },
    ]);
    addNagSuppression(this.userPool, [
      {
        rule_id: 'AwsSolutions-COG3',
        reason: 'suppress advanced security rule 1/to save cost, 2/Not supported in GovCloud',
      },
    ]);

    new cognito.CfnUserPoolGroup(scope, `${userPoolName}-administrators-group`, {
      description: 'Administrators group (Sudo Users)',
      groupName: clusterGroupName(ctx, 'identity-provider.cognito.administrators_group_name'),
      precedence: 1,
      userPoolId: this.userPool.userPoolId,
    });

    new cognito.CfnUserPoolGroup(scope, `${userPoolName}-managers-group`, {
      description: 'Managers group with limited administration access.',
      groupName: clusterGroupName(ctx, 'identity-provider.cognito.managers_group_name'),
      precedence: 2,
      userPoolId: this.userPool.userPoolId,
    });

    const domainUrl = ctx.config.getString('identity-provider.cognito.domain_url');
    const domainPrefix = isEmpty(domainUrl)
      ? `${ctx.clusterName}-${randomUUID()}`
      : ((domainUrl as string).replace('https://', '').split('.')[0] as string);

    this.domain = this.userPool.addDomain('domain', {
      cognitoDomain: { domainPrefix },
    });
  }
}
