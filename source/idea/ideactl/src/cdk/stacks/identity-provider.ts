/**
 * `identity-provider.provider` is the only branch: `cognito-idp` builds everything below,
 * `keycloak` raises "not supported (yet)", anything else raises. The stack is twelve
 * resources: two lambda role/policy/function trios, the user pool with its two cluster groups,
 * its hosted domain, the pre-token-generation permission CDK adds for the trigger, and the
 * `Custom::ClusterSettings` row every module stack ends with.
 *
 * The invitation email subject and body are read back rather than generated: `buildStack` calls
 * `cognito-idp:DescribeUserPool` whenever `identity-provider.cognito.user_pool_id` is set, so an
 * admin's edits in the console survive the next deploy.
 *
 * The Cognito domain prefix comes from `identity-provider.cognito.domain_url`, and with that key
 * empty the shared `UserPool` construct mints `<cluster>-<uuid>`, replacing the deployed
 * `AWS::Cognito::UserPoolDomain`. The `Custom::ClusterSettings` row writes both keys together.
 */

import { RemovalPolicy } from 'aws-cdk-lib';

import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { IdeaCodeAsset } from '../code-asset.ts';
import { isEmpty } from '../../config/cluster-config.ts';
import { LambdaFunction, Policy, Role } from '../constructs/common.ts';
import { UserPool } from '../constructs/directory-service.ts';
import { ExistingSocaCluster } from '../constructs/existing-resources.ts';
import type { UserPool as UserPoolDescription } from '../synth-reads.ts';

/** `constants.IDENTITY_PROVIDER_COGNITO_IDP` / `..._KEYCLOAK`. */
export const IDENTITY_PROVIDER_COGNITO_IDP = 'cognito-idp';
export const IDENTITY_PROVIDER_KEYCLOAK = 'keycloak';

/** `app_constants.LOG_RETENTION_ROLE_NAME`. */
const LOG_RETENTION_ROLE_NAME = 'log-retention';

/** `constants.CAVEATS['COGNITO_REQUIRE_FIPS_ENDPOINT_REGION_LIST']`. */
export const COGNITO_REQUIRE_FIPS_ENDPOINT_REGION_LIST = ['us-gov-east-1', 'us-gov-west-1'];

/** `RemovalPolicy` lookup uses member names, not enum values. */
function removalPolicyByName(name: string): RemovalPolicy {
  if (!Object.prototype.hasOwnProperty.call(RemovalPolicy, name)) {
    throw new Error(`'${name}' is not a valid RemovalPolicy`);
  }
  return RemovalPolicy[name as keyof typeof RemovalPolicy];
}

/** The invitation email for a cluster that has no user pool yet, `os.linesep`-joined. */
export function generatedInvitationEmailBody(clusterName: string, externalEndpoint: string): string {
  return [
    '<p>Hello <b>{username},</b></p>',
    `<p>You have been invited to join the <b>${clusterName}</b> cluster.</p>`,
    '<p>Your temporary password is:</p>',
    '<h3>{####}</h3>',
    '<p>You can sign in to your account using the link below: <br/>',
    `<a href="${externalEndpoint}">${externalEndpoint}</a></p>`,
    '<p>---<br/>',
    '<b>IDEA Cluster Admin</b></p>',
  ].join('\n');
}

export class IdentityProviderStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  idTokenClaimLambda: LambdaFunction | undefined;
  userPool: UserPool | undefined;
  oauthCredentialsLambda: LambdaFunction | undefined;
  /** `cognito-idp:DescribeUserPool` of the deployed pool, read by `buildStack`. */
  private readonly describedUserPool: UserPoolDescription | undefined;

  constructor(props: StackBuildProps, describedUserPool?: UserPoolDescription) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    this.describedUserPool = describedUserPool;
    this.cluster = new ExistingSocaCluster(this.context, this.stack);

    const provider = this.context.config.getString('identity-provider.provider', undefined, {
      required: true,
    }) as string;
    if (provider === IDENTITY_PROVIDER_KEYCLOAK) {
      throw new Error(`identity provider: ${provider} not supported (yet).`);
    }
    if (provider !== IDENTITY_PROVIDER_COGNITO_IDP) {
      throw new Error(`identity provider: ${provider} not supported`);
    }

    this.buildCognitoIdp();
    this.buildCognitoClusterSettings();
  }

  /** The invitation email is generated for a new pool and read from an existing pool. */
  userInvitation(): { emailSubject: string; emailBody: string } {
    const config = this.context.config;
    // Not the custom dns name: it may not be configured yet when the cluster is first created.
    const externalAlbDns = config.getString(
      'cluster.load_balancers.external_alb.load_balancer_dns_name',
      undefined,
      { required: true },
    ) as string;
    const userPoolId = config.getString('identity-provider.cognito.user_pool_id');

    if (isEmpty(userPoolId)) {
      return {
        emailSubject: `Invitation to Join IDEA Cluster: ${this.clusterName}`,
        emailBody: generatedInvitationEmailBody(this.clusterName, `https://${externalAlbDns}`),
      };
    }

    if (this.describedUserPool === undefined) {
      throw new Error(
        `identity-provider: cognito-idp:DescribeUserPool for user pool ${userPoolId as string} is required at ` +
          'synth time and was not read. Build this stack through buildStack(): synthesizing without the read ' +
          "would rewrite the user pool's invitation email.",
      );
    }
    const template = this.describedUserPool.AdminCreateUserConfig?.InviteMessageTemplate;
    if (template?.EmailSubject === undefined || template.EmailMessage === undefined) {
      throw new Error(
        `identity-provider: user pool ${userPoolId as string} has no AdminCreateUserConfig.InviteMessageTemplate`,
      );
    }
    return { emailSubject: template.EmailSubject, emailBody: template.EmailMessage };
  }

  buildCognitoIdp(): void {
    const config = this.context.config;
    const removalPolicy = removalPolicyByName(
      config.getString('identity-provider.cognito.removal_policy', undefined, { required: true }) as string,
    );

    const userInvitation = this.userInvitation();

    // Adds the custom claims to the Cognito ID token when SSO is enabled.
    const claimLambdaName = 'id-token-claim';
    const idTokenClaimLambdaRole = new Role(this.context, `${claimLambdaName}-role`, this.stack, {
      description: `Role for id token claim Lambda function for Cluster: ${this.clusterName}`,
      assumedBy: ['lambda', 'cognito-idp'],
    });
    idTokenClaimLambdaRole.attachInlinePolicy(
      new Policy(this.context, `${claimLambdaName}-policy`, this.stack, {
        policyTemplateName: 'custom_resource_sso_claim_modifier.yml',
      }),
    );
    this.idTokenClaimLambda = new LambdaFunction(this.context, claimLambdaName, this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_sso_claim_modifier'),
      description: 'Modify Cognito ID Token for SSO Enabled Clusters',
      timeoutSeconds: 180,
      role: idTokenClaimLambdaRole,
      logRetentionRole: this.cluster.getRole(LOG_RETENTION_ROLE_NAME),
    });
    this.addNagSuppression(
      [{ rule_id: 'AwsSolutions-L1', reason: 'Python Runtime is selected for stability.' }],
      this.idTokenClaimLambda,
    );
    this.idTokenClaimLambda.node.addDependency(idTokenClaimLambdaRole);

    this.userPool = new UserPool(this.context, `${this.clusterName}-user-pool`, this.stack, {
      removalPolicy,
      userInvitation,
      lambdaTriggers: { preTokenGeneration: this.idTokenClaimLambda },
    });

    // One lambda for the whole cluster: every module stack invokes it as a custom resource to
    // fetch its own OAuth2 client id and secret, through the arn in the cluster settings below.
    const lambdaName = 'oauth-credentials';
    const oauthCredentialsLambdaRole = new Role(this.context, `${lambdaName}-role`, this.stack, {
      description: `Role for auth credentials Lambda function for Cluster: ${this.clusterName}`,
      assumedBy: ['lambda'],
    });
    oauthCredentialsLambdaRole.attachInlinePolicy(
      new Policy(this.context, `${lambdaName}-policy`, this.stack, {
        policyTemplateName: 'custom-resource-get-user-pool-client-secret.yml',
      }),
    );
    this.oauthCredentialsLambda = new LambdaFunction(this.context, lambdaName, this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_get_user_pool_client_secret'),
      description: 'Get OAuth Credentials for a ClientId in UserPool',
      timeoutSeconds: 180,
      role: oauthCredentialsLambdaRole,
      logRetentionRole: this.cluster.getRole(LOG_RETENTION_ROLE_NAME),
    });
    this.addNagSuppression(
      [{ rule_id: 'AwsSolutions-L1', reason: 'Python Runtime is selected for stability.' }],
      this.oauthCredentialsLambda,
    );
    this.oauthCredentialsLambda.node.addDependency(oauthCredentialsLambdaRole);
  }

  buildCognitoClusterSettings(): void {
    const userPool = this.userPool as UserPool;
    this.updateClusterSettings({
      deployment_id: this.deploymentId,
      'cognito.user_pool_id': userPool.userPool.userPoolId,
      'cognito.provider_url': userPool.userPool.userPoolProviderUrl,
      'cognito.domain_url': userPool.domain.baseUrl({
        fips: COGNITO_REQUIRE_FIPS_ENDPOINT_REGION_LIST.includes(this.awsRegion),
      }),
      'cognito.oauth_credentials_lambda_arn': (this.oauthCredentialsLambda as LambdaFunction).functionArn,
    });
  }
}

/**
 * Reads the deployed user pool before the tree is built. `SynthReads` is asynchronous and a stack
 * constructor is not, so the one synth-time read this stack needs happens here. A cluster with no
 * pool yet skips it and the invitation email is generated instead.
 */
export async function buildStack(props: StackBuildProps): Promise<void> {
  const userPoolId = props.ctx.config.getString('identity-provider.cognito.user_pool_id');
  const describedUserPool = isEmpty(userPoolId)
    ? undefined
    : await props.ctx.synthReads.describeUserPool(userPoolId as string);
  new IdentityProviderStack(props, describedUserPool);
}
