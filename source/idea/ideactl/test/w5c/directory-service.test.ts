/**
 * `constructs/directory-service.ts` and the DNS chain it builds, against the live captured
 * `directoryservice` template (AWS Managed AD branch).
 */

import assert from 'node:assert/strict';
import { after, describe, test } from 'node:test';

import {
  ActiveDirectory,
  DirectoryServiceCredentials,
  OAuthClientIdAndSecret,
  UserPool,
} from '../../src/cdk/constructs/directory-service.ts';
import { ExistingSocaCluster } from '../../src/cdk/constructs/existing-resources.ts';
import { CLUSTER, cleanup, configWith, harness, liveResources, requireLiveFixture } from './harness.ts';
import type { Json } from './harness.ts';

after(cleanup);
requireLiveFixture('directoryservice');
requireLiveFixture('identity-provider');

const AD_LOGICAL_IDS = [
  'awsmanagedactivedirectoryadminusername',
  'awsmanagedactivedirectoryadminpassword',
  'activedirectory',
  'ideadev27activedirectory',
  'ideadev27dnsresolverendpoint',
  'activedirectorydnsresolverrule',
  'activedirectorydnsresolverruleassociation',
];

function buildActiveDirectory(configFile?: string): Json {
  const h = harness({ moduleId: 'directoryservice', moduleName: 'directoryservice', configFile });
  const cluster = new ExistingSocaCluster(h.ctx, h.base.stack);
  new ActiveDirectory(h.ctx, 'active-directory', h.base.stack, { cluster, enableSso: false });
  return h.template().Resources as Json;
}

describe('ActiveDirectory + DNS resolver', () => {
  const live = liveResources('directoryservice');

  test('every AD, secret and resolver resource matches the deployed template', () => {
    const resources = buildActiveDirectory();
    for (const logicalId of AD_LOGICAL_IDS) {
      assert.ok(resources[logicalId] !== undefined, `missing logical id: ${logicalId}`);
      assert.deepEqual(resources[logicalId], live[logicalId], `mismatch on ${logicalId}`);
    }
  });

  test('the AD launches into the first two private subnets only', () => {
    const resources = buildActiveDirectory();
    const subnets = resources.activedirectory.Properties.VpcSettings.SubnetIds as string[];
    assert.equal(subnets.length, 2);
    assert.deepEqual(
      (resources.ideadev27dnsresolverendpoint.Properties.IpAddresses as Json[]).map((entry) => entry.SubnetId),
      subnets,
    );
  });

  test('the forward rule targets the first two AD DNS addresses on port "53"', () => {
    const targets = buildActiveDirectory().activedirectorydnsresolverrule.Properties.TargetIps as Json[];
    assert.deepEqual(
      targets.map((target) => target.Port),
      ['53', '53'],
    );
    assert.deepEqual(
      targets.map((target) => target.Ip['Fn::Select'][0]),
      [0, 1],
    );
  });

  test('the resolver rule association carries no tags (the L1 has no tag property)', () => {
    const association = buildActiveDirectory().activedirectorydnsresolverruleassociation;
    assert.equal(association.Properties.Tags, undefined);
  });
});

describe('DirectoryServiceCredentials branches', () => {
  test('the provider name is part of the secret construct ids', () => {
    const configFile = configWith({ 'directoryservice.provider': 'openldap' });
    const h = harness({ moduleId: 'directoryservice', moduleName: 'directoryservice', configFile });
    const credentials = new DirectoryServiceCredentials(
      h.ctx,
      'directoryservice-openldap-credentials',
      h.base.stack,
      'Admin',
    );
    const resources = h.template().Resources as Json;
    assert.equal(resources.openldapadminusername.Properties.Name, `${CLUSTER}-openldap-admin-username`);
    assert.equal(resources.openldapadminpassword.Properties.Name, `${CLUSTER}-openldap-admin-password`);
    assert.equal(resources.openldapadminusername.Properties.SecretString, 'Admin');
    assert.deepEqual(resources.openldapadminpassword.Properties.GenerateSecretString, {
      ExcludeCharacters: '$@;"\\\'',
      PasswordLength: 16,
    });
    // the secrets are tagged for IAM, and carry no deletion policy at all
    assert.deepEqual(resources.openldapadminusername.Properties.Tags, [
      { Key: 'idea:ModuleName', Value: 'directoryservice' },
    ]);
    assert.equal(resources.openldapadminusername.DeletionPolicy, undefined);
    assert.equal(credentials.credentialsProvided, false);
  });

  test('a supplied password is written verbatim instead of generated', () => {
    const h = harness({ moduleId: 'directoryservice', moduleName: 'directoryservice' });
    new DirectoryServiceCredentials(h.ctx, 'creds', h.base.stack, 'Admin', 'sample-password');
    const secret = (h.template().Resources as Json).awsmanagedactivedirectoryadminpassword;
    assert.equal(secret.Properties.SecretString, 'sample-password');
    assert.equal(secret.Properties.GenerateSecretString, undefined);
  });

  test('root_credentials_provided builds nothing and reads the ARNs from config', () => {
    const usernameArn = 'arn:aws:secretsmanager:us-east-2:123456789012:secret:sample-cluster-admin-username';
    const passwordArn = 'arn:aws:secretsmanager:us-east-2:123456789012:secret:sample-cluster-admin-password';
    const configFile = configWith({
      'directoryservice.root_credentials_provided': true,
      'directoryservice.root_username_secret_arn': usernameArn,
      'directoryservice.root_password_secret_arn': passwordArn,
    });
    const h = harness({ moduleId: 'directoryservice', moduleName: 'directoryservice', configFile });
    const credentials = new DirectoryServiceCredentials(h.ctx, 'creds', h.base.stack, 'Admin');
    assert.equal(credentials.credentialsProvided, true);
    assert.deepEqual(h.template().Resources ?? {}, {});
    assert.equal(credentials.getUsernameSecretArn(), usernameArn);
    assert.equal(credentials.getPasswordSecretArn(), passwordArn);
  });
});

describe('OAuthClientIdAndSecret', () => {
  test('both secrets are module-tagged and removable', () => {
    const h = harness({ moduleId: 'cluster-manager', moduleName: 'cluster-manager' });
    new OAuthClientIdAndSecret(
      h.ctx,
      'cluster-manager',
      'cluster-manager',
      h.base.stack,
      'sample-client-id',
      'sample-client-secret',
    );
    const resources = h.template().Resources as Json;
    const clientId = resources.clustermanagerclientid;
    const clientSecret = resources.clustermanagerclientsecret;
    assert.equal(clientId.Properties.Name, `${CLUSTER}-cluster-manager-client-id`);
    assert.equal(clientId.Properties.SecretString, 'sample-client-id');
    assert.equal(clientId.DeletionPolicy, 'Delete');
    assert.equal(clientId.UpdateReplacePolicy, 'Delete');
    assert.equal(clientSecret.Properties.Description, `cluster-manager ClientSecret, Cluster: ${CLUSTER}`);
    assert.deepEqual(clientSecret.Properties.Tags, [{ Key: 'idea:ModuleName', Value: 'cluster-manager' }]);
  });
});

describe('UserPool', () => {
  const live = liveResources('identity-provider');

  function buildUserPool(configFile?: string): Json {
    const h = harness({ moduleId: 'identity-provider', moduleName: 'identity-provider', configFile });
    new UserPool(h.ctx, `${CLUSTER}-user-pool`, h.base.stack, { removalPolicy: undefined });
    return h.template().Resources as Json;
  }

  test('the pool and both cluster groups match the deployed template', () => {
    const resources = buildUserPool();
    // the deployed pool carries the pre-token-generation trigger the identity-provider stack adds,
    // and the invitation the stack reads back from the existing pool. This construct owns the
    // rest, plus the invitation default used when the stack does not pass one.
    const expected = structuredClone(live.ideadev27userpoolD5C370B5) as Json;
    delete expected.Properties.LambdaConfig;
    delete expected.Properties.AdminCreateUserConfig.InviteMessageTemplate;
    const actual = structuredClone(resources.ideadev27userpoolD5C370B5) as Json;
    const invite = actual.Properties.AdminCreateUserConfig.InviteMessageTemplate as Json;
    delete actual.Properties.AdminCreateUserConfig.InviteMessageTemplate;
    assert.deepEqual(actual, expected);
    assert.equal(invite.EmailSubject, `(${CLUSTER}) Your IDEA Account`);
    assert.equal(
      invite.EmailMessage,
      '\n                Hello <b>{username}</b>,\n' +
        '                <br/><br/>\n' +
        `                You have been invited to join the ${CLUSTER} cluster.\n` +
        '                <br/>\n' +
        '                Your temporary password is <b>{####}</b>\n' +
        '                ',
    );
    assert.deepEqual(resources.ideadev27userpooladministratorsgroup, live.ideadev27userpooladministratorsgroup);
    assert.deepEqual(resources.ideadev27userpoolmanagersgroup, live.ideadev27userpoolmanagersgroup);
  });

  test('the custom attributes keep their declaration order and advanced security stays off', () => {
    const pool = buildUserPool().ideadev27userpoolD5C370B5;
    assert.deepEqual(
      (pool.Properties.Schema as Json[]).map((entry) => entry.Name),
      ['email', 'cluster_name', 'aws_region', 'password_last_set', 'password_max_age'],
    );
    assert.equal(pool.Properties.UserPoolAddOns, undefined);
    assert.equal(pool.Properties.DeletionProtection, 'ACTIVE');
  });

  test('the domain prefix comes from domain_url when it is set', () => {
    const resources = buildUserPool();
    assert.deepEqual(resources.ideadev27userpooldomain237CD715, live.ideadev27userpooldomain237CD715);
  });

  test('an empty domain_url regenerates the prefix on every synth', () => {
    const configFile = configWith({ 'identity-provider.cognito.domain_url': null });
    const first = buildUserPool(configFile).ideadev27userpooldomain237CD715.Properties.Domain as string;
    const second = buildUserPool(configFile).ideadev27userpooldomain237CD715.Properties.Domain as string;
    assert.match(first, new RegExp(`^${CLUSTER}-[0-9a-f-]{36}$`));
    assert.notEqual(first, second);
  });
});
