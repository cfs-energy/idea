import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import { ArnBuilder } from '../../src/config/arn-builder.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { requireFixtures } from '../support/fixtures.ts';

const RAW = fileURLToPath(new URL('../../tools/parity/fixtures/idea-dev27/raw/', import.meta.url));
const LIVE = fileURLToPath(new URL('../../tools/parity/live/', import.meta.url));
const SCAN = `${RAW}cluster-settings.scan.json`;
const MODULES = `${RAW}modules.scan.json`;
requireFixtures(
  [SCAN, MODULES, LIVE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

const PARTITION = 'aws';
const REGION = 'us-east-2';
// The account ID is a non-numeric synthetic configuration value.
const ACCOUNT = 'aws-account-id';
const CLUSTER = 'sample-cluster';
const VPC = 'vpc-0a1b2c3d';
const BUCKET = 'sample-cluster-s3-bucket';
const USER_POOL = 'us-east-2_ABCDEfghi';
const DIRECTORY = 'd-90670abcde';
// Module IDs differ from module names.
const CLUSTER_MANAGER_ID = 'cm1';
const DIRECTORYSERVICE_ID = 'ds1';

/** One raw `aws dynamodb scan` item: typed attribute values, as the CLI prints them. */
type ScanItem = Record<string, Record<string, unknown>>;

function config(overrides: ScanItem[] = []): ClusterConfig {
  return ClusterConfig.fromFile(
    JSON.stringify({
      Items: [
        { key: { S: 'cluster.aws.partition' }, value: { S: PARTITION } },
        { key: { S: 'cluster.aws.region' }, value: { S: REGION } },
        { key: { S: 'cluster.aws.account_id' }, value: { S: ACCOUNT } },
        { key: { S: 'cluster.aws.dns_suffix' }, value: { S: 'amazonaws.com' } },
        { key: { S: 'cluster.cluster_name' }, value: { S: CLUSTER } },
        { key: { S: 'cluster.cluster_s3_bucket' }, value: { S: BUCKET } },
        { key: { S: 'cluster.network.vpc_id' }, value: { S: VPC } },
        { key: { S: 'cluster.secretsmanager.kms_key_id' }, value: { S: 'key-secretsmanager' } },
        { key: { S: 'cluster.sqs.kms_key_id' }, value: { S: 'key-sqs' } },
        { key: { S: 'cluster.sns.kms_key_id' }, value: { NULL: true } },
        { key: { S: 'cluster.dynamodb.kms_key_id' }, value: { S: 'key-dynamodb' } },
        { key: { S: 'cluster.ebs.kms_key_id' }, value: { NULL: true } },
        { key: { S: 'cluster.backups.backup_vault.kms_key_id' }, value: { S: 'key-backup' } },
        { key: { S: 'analytics.opensearch.kms_key_id' }, value: { S: 'key-opensearch' } },
        { key: { S: 'analytics.kinesis.kms_key_id' }, value: { S: 'key-kinesis' } },
        { key: { S: 'identity-provider.cognito.user_pool_id' }, value: { S: USER_POOL } },
        // rows are keyed by module id, so the directoryservice row lives under its module id
        { key: { S: `${DIRECTORYSERVICE_ID}.directory_id` }, value: { S: DIRECTORY } },
        {
          key: { S: 'global-settings.module_sets.default.cluster-manager.module_id' },
          value: { S: CLUSTER_MANAGER_ID },
        },
        {
          key: { S: 'global-settings.module_sets.default.directoryservice.module_id' },
          value: { S: DIRECTORYSERVICE_ID },
        },
        ...overrides,
      ],
    }),
  );
}

const arns = new ArnBuilder(config());

const IAM = `arn:${PARTITION}:iam::${ACCOUNT}`;
const LOGS = `arn:${PARTITION}:logs:${REGION}:${ACCOUNT}`;
const DDB = `arn:${PARTITION}:dynamodb:${REGION}:${ACCOUNT}`;
const KMS = `arn:${PARTITION}:kms:${REGION}:${ACCOUNT}`;
const S3 = `arn:${PARTITION}:s3::`;
const ELB = `arn:${PARTITION}:elasticloadbalancing:${REGION}:${ACCOUNT}`;
const BEDROCK = `arn:${PARTITION}:bedrock:${REGION}:${ACCOUNT}`;

/** Every scalar-returning member, with the exact string `arn_builder.py` produces for it. */
const scalarCases: Array<[string, () => string, string]> = [
  ['vpcArn', () => arns.vpcArn, `arn:${PARTITION}:ec2:${REGION}:${ACCOUNT}:vpc/${VPC}`],
  ['getLogGroupArn()', () => arns.getLogGroupArn(), `${LOGS}:log-group:/${CLUSTER}*`],
  ['getLogGroupArn(suffix)', () => arns.getLogGroupArn('-bootstrap'), `${LOGS}:log-group:/${CLUSTER}-bootstrap`],
  ['getLogStreamArn()', () => arns.getLogStreamArn(), `${LOGS}:log-group:/${CLUSTER}*:log-stream:*`],
  ['getLambdaLogGroupArn()', () => arns.getLambdaLogGroupArn(), `${LOGS}:log-group:/aws/lambda/${CLUSTER}*`],
  [
    'getLambdaLogGroupArn(suffix)',
    () => arns.getLambdaLogGroupArn('-self-signed-certificate'),
    `${LOGS}:log-group:/aws/lambda/${CLUSTER}-self-signed-certificate`,
  ],
  ['lambdaLogStreamArn', () => arns.lambdaLogStreamArn, `${LOGS}:log-group:/aws/lambda/${CLUSTER}*:log-stream:*`],
  ['albListenerRuleArn', () => arns.albListenerRuleArn, `${ELB}:listener-rule/app/${CLUSTER}*/*/*`],
  ['albListenerArn', () => arns.albListenerArn, `${ELB}:listener/app/${CLUSTER}*/*/*`],
  ['targetGroupArn', () => arns.targetGroupArn, `${ELB}:targetgroup/soca*/*`],
  ['getLambdaArn()', () => arns.getLambdaArn(), `arn:${PARTITION}:lambda:${REGION}:${ACCOUNT}:function:${CLUSTER}-*`],
  [
    'getLambdaArn(suffix)',
    () => arns.getLambdaArn('cluster-settings'),
    `arn:${PARTITION}:lambda:${REGION}:${ACCOUNT}:function:${CLUSTER}-cluster-settings`,
  ],
  ['projectRolePath', () => arns.projectRolePath, `/idea/${CLUSTER}/projects/`],
  ['getProjectRoleArn()', () => arns.getProjectRoleArn(), `${IAM}:role/idea/${CLUSTER}/projects/*`],
  [
    'getProjectRoleArn(name)',
    () => arns.getProjectRoleArn('sample-project-role'),
    `${IAM}:role/idea/${CLUSTER}/projects/sample-project-role`,
  ],
  [
    'getProjectInstanceProfileArn()',
    () => arns.getProjectInstanceProfileArn(),
    `${IAM}:instance-profile/idea/${CLUSTER}/projects/*`,
  ],
  ['getProjectPolicyArn()', () => arns.getProjectPolicyArn(), `${IAM}:policy/idea/${CLUSTER}/projects/*`],
  [
    'getProjectPermissionsBoundaryArn()',
    () => arns.getProjectPermissionsBoundaryArn(),
    `${IAM}:policy/${CLUSTER}-${REGION}-${CLUSTER_MANAGER_ID}-project-boundary`,
  ],
  [
    'bedrockInvocationLogGroupName',
    () => arns.bedrockInvocationLogGroupName,
    `/${CLUSTER}/${CLUSTER_MANAGER_ID}/bedrock-invocations`,
  ],
  [
    'bedrockInvocationLogGroupArn',
    () => arns.bedrockInvocationLogGroupArn,
    `${LOGS}:log-group:/${CLUSTER}/${CLUSTER_MANAGER_ID}/bedrock-invocations`,
  ],
  [
    'bedrockApplicationInferenceProfileArn',
    () => arns.bedrockApplicationInferenceProfileArn,
    `${BEDROCK}:application-inference-profile/*`,
  ],
  ['bedrockSystemInferenceProfileArn', () => arns.bedrockSystemInferenceProfileArn, `${BEDROCK}:inference-profile/*`],
  [
    'bedrockAnySystemInferenceProfileArn',
    () => arns.bedrockAnySystemInferenceProfileArn,
    `arn:${PARTITION}:bedrock:*:*:inference-profile/*`,
  ],
  ['bedrockFoundationModelArn', () => arns.bedrockFoundationModelArn, `arn:${PARTITION}:bedrock:*::foundation-model/*`],
  ['sesArn', () => arns.sesArn, `arn:${PARTITION}:ses:*:${ACCOUNT}:identity/*`],
  [
    'getSsmArn()',
    () => arns.getSsmArn('automation-definition/'),
    `arn:${PARTITION}:ssm::::automation-definition/`,
  ],
  ['getDdbTableArn(suffix)', () => arns.getDdbTableArn('projects'), `${DDB}:table/${CLUSTER}.projects`],
  ['getAdAutomationDdbTableArn()', () => arns.getAdAutomationDdbTableArn(), `${DDB}:table/${CLUSTER}.ad-automation`],
  [
    'getAdAutomationSqsQueueArn()',
    () => arns.getAdAutomationSqsQueueArn(),
    `arn:${PARTITION}:sqs:${REGION}:${ACCOUNT}:${CLUSTER}-${DIRECTORYSERVICE_ID}-ad-automation.fifo`,
  ],
  [
    'getKinesisArn()',
    () => arns.getKinesisArn(),
    `arn:${PARTITION}:kinesis:${REGION}:${ACCOUNT}:stream/${CLUSTER}-*`,
  ],
  [
    'getSnsArn(suffix)',
    () => arns.getSnsArn('ec2-state-change'),
    `arn:${PARTITION}:sns:${REGION}:${ACCOUNT}:${CLUSTER}-ec2-state-change`,
  ],
  [
    'getSqsArn(suffix)',
    () => arns.getSqsArn('vdc-events.fifo'),
    `arn:${PARTITION}:sqs:${REGION}:${ACCOUNT}:${CLUSTER}-vdc-events.fifo`,
  ],
  ['getRoute53HostedzoneArn()', () => arns.getRoute53HostedzoneArn(), `arn:${PARTITION}:route53:::hostedzone/*`],
  ['kmsSecretsmanagerKeyArn', () => arns.kmsSecretsmanagerKeyArn, `${KMS}:key/key-secretsmanager`],
  ['kmsSqsKeyArn', () => arns.kmsSqsKeyArn, `${KMS}:key/key-sqs`],
  ['kmsSnsKeyArn (unset key id)', () => arns.kmsSnsKeyArn, `${KMS}:key/undefined`],
  ['kmsDynamodbKeyArn', () => arns.kmsDynamodbKeyArn, `${KMS}:key/key-dynamodb`],
  ['kmsEbsKeyArn (unset key id)', () => arns.kmsEbsKeyArn, `${KMS}:key/undefined`],
  ['kmsBackupKeyArn', () => arns.kmsBackupKeyArn, `${KMS}:key/key-backup`],
  ['kmsOpensearchKeyArn', () => arns.kmsOpensearchKeyArn, `${KMS}:key/key-opensearch`],
  ['kmsKinesisKeyArn', () => arns.kmsKinesisKeyArn, `${KMS}:key/key-kinesis`],
  [
    'userPoolArn',
    () => arns.userPoolArn,
    `arn:${PARTITION}:cognito-idp:${REGION}:${ACCOUNT}:userpool/${USER_POOL}`,
  ],
  [
    'getDirectoryServiceArn()',
    () => arns.getDirectoryServiceArn(),
    `arn:${PARTITION}:ds:${REGION}:${ACCOUNT}:directory/${DIRECTORY}`,
  ],
  [
    'getDdbApplicationAutoscalingServiceRoleArn()',
    () => arns.getDdbApplicationAutoscalingServiceRoleArn(),
    `${IAM}:role/aws-service-role/dynamodb.application-autoscaling.amazonaws.com/AWSServiceRoleForApplicationAutoScaling_DynamoDBTable`,
  ],
];

/** Every list-returning member, with its exact contents in Python's order. */
const listCases: Array<[string, () => string[], string[]]> = [
  [
    'ec2CommonArns',
    () => arns.ec2CommonArns,
    [
      `arn:${PARTITION}:ec2:*:*:subnet/*`,
      `arn:${PARTITION}:ec2:*:${ACCOUNT}:key-pair/*`,
      `arn:${PARTITION}:ec2:*:${ACCOUNT}:instance/*`,
      `arn:${PARTITION}:ec2:*:*:snapshot/*`,
      `arn:${PARTITION}:ec2:*:${ACCOUNT}:launch-template/*`,
      `arn:${PARTITION}:ec2:*:${ACCOUNT}:volume/*`,
      `arn:${PARTITION}:ec2:*:${ACCOUNT}:security-group/*`,
      `arn:${PARTITION}:ec2:*:${ACCOUNT}:placement-group/*`,
      `arn:${PARTITION}:ec2:*:${ACCOUNT}:network-interface/*`,
      `arn:${PARTITION}:ec2:*:*:spot-instances-request/*`,
      `arn:${PARTITION}:ec2:*:*:image/*`,
    ],
  ],
  [
    's3GlobalArns',
    () => arns.s3GlobalArns,
    [
      `${S3}:dcv-license.${REGION}/*`,
      `${S3}:ec2-linux-nvidia-drivers/*`,
      `${S3}:ec2-linux-nvidia-drivers`,
      `${S3}:ec2-windows-nvidia-drivers/*`,
      `${S3}:ec2-windows-nvidia-drivers`,
      `${S3}:nvidia-gaming/*`,
      `${S3}:nvidia-gaming-drivers`,
      `${S3}:nvidia-gaming-drivers/*`,
      `${S3}:ec2-amd-linux-drivers/*`,
      `${S3}:ec2-amd-linux-drivers`,
      `${S3}:ec2-amd-windows-drivers/*`,
      `${S3}:ec2-amd-windows-drivers`,
    ],
  ],
  [
    'serviceRoleArns',
    () => arns.serviceRoleArns,
    [
      `${IAM}:role/aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*`,
      `${IAM}:role/aws-service-role/autoscaling.amazonaws.com/*`,
      `${IAM}:role/aws-service-role/spotfleet.amazonaws.com/*`,
      `${IAM}:role/aws-service-role/fsx.amazonaws.com/*`,
    ],
  ],
  ['dcvLicenseS3BucketArns', () => arns.dcvLicenseS3BucketArns, [`${S3}:dcv-license.*/*`, `${S3}:dcv-license.*`]],
  ['s3BucketArns', () => arns.s3BucketArns, [`${S3}:${BUCKET}/*`, `${S3}:${BUCKET}`]],
  [
    'clusterConfigDdbArn',
    () => arns.clusterConfigDdbArn,
    [
      `${DDB}:table/${CLUSTER}.cluster-settings`,
      `${DDB}:table/${CLUSTER}.cluster-settings/stream/*`,
      `${DDB}:table/${CLUSTER}.modules`,
    ],
  ],
  [
    'kmsKeyArn (only the key ids that are set, in Python dict order)',
    () => arns.kmsKeyArn,
    [
      `${KMS}:key/key-secretsmanager`,
      `${KMS}:key/key-sqs`,
      `${KMS}:key/key-dynamodb`,
      `${KMS}:key/key-backup`,
      `${KMS}:key/key-opensearch`,
      `${KMS}:key/key-kinesis`,
    ],
  ],
];

describe('ArnBuilder.buildArn', () => {
  it('resource wins over resource_type / resource_id', () => {
    assert.equal(
      ArnBuilder.buildArn({ partition: 'aws', service: 'sqs', region: REGION, accountId: ACCOUNT, resource: 'q' }),
      `arn:aws:sqs:${REGION}:${ACCOUNT}:q`,
    );
  });

  it('a bare resource_id is appended without a delimiter', () => {
    assert.equal(
      ArnBuilder.buildArn({ partition: 'aws', service: 'sns', region: REGION, accountId: ACCOUNT, resourceId: 't' }),
      `arn:aws:sns:${REGION}:${ACCOUNT}:t`,
    );
  });

  it('resource_type joins resource_id with the delimiter, "/" by default', () => {
    const base = { partition: 'aws', service: 'ssm', region: '', accountId: '', resourceId: 'doc' };
    assert.equal(ArnBuilder.buildArn({ ...base, resourceType: 'document' }), 'arn:aws:ssm:::document/doc');
    assert.equal(
      ArnBuilder.buildArn({ ...base, resourceType: 'document', resourceDelimiter: ':' }),
      'arn:aws:ssm:::document:doc',
    );
  });
});

describe('ArnBuilder against a synthetic cluster config', () => {
  for (const [label, actual, expected] of scalarCases) {
    it(label, () => assert.equal(actual(), expected));
  }

  for (const [label, actual, expected] of listCases) {
    it(label, () => {
      const value = actual();
      assert.equal(value.length, expected.length, `${label} returned ${value.length} arns`);
      assert.deepEqual(value, expected);
    });
  }

  it('carries the partition into every slot that names one, and nowhere else', () => {
    const gov = new ArnBuilder(
      config([
        { key: { S: 'cluster.aws.partition' }, value: { S: 'aws-us-gov' } },
        { key: { S: 'cluster.aws.region' }, value: { S: 'us-gov-west-1' } },
      ]),
    );
    assert.equal(gov.getLogGroupArn(), `arn:aws-us-gov:logs:us-gov-west-1:${ACCOUNT}:log-group:/${CLUSTER}*`);
    assert.equal(gov.getRoute53HostedzoneArn(), 'arn:aws-us-gov:route53:::hostedzone/*');
    // the service linked role path is 'aws-service-role' in every partition
    assert.equal(gov.serviceRoleArns[1], `arn:aws-us-gov:iam::${ACCOUNT}:role/aws-service-role/autoscaling.amazonaws.com/*`);
  });

  it('required config: a missing cluster name raises rather than building "undefined"', () => {
    const empty = ClusterConfig.fromFile(JSON.stringify({ Items: [] }));
    assert.throws(() => new ArnBuilder(empty).projectRolePath);
    assert.throws(() => new ArnBuilder(empty).getDirectoryServiceArn());
  });
});

/** Every literal arn that appears inside an IAM policy document in the live templates. */
function liveIamPolicyArns(): Set<string> {
  const found = new Set<string>();
  for (const file of readdirSync(LIVE).filter((name) => name.endsWith('.json'))) {
    const template = JSON.parse(readFileSync(`${LIVE}${file}`, 'utf-8')) as {
      Resources?: Record<string, { Type: string }>;
    };
    for (const resource of Object.values(template.Resources ?? {})) {
      if (!['AWS::IAM::Policy', 'AWS::IAM::Role', 'AWS::IAM::ManagedPolicy'].includes(resource.Type)) continue;
      for (const match of JSON.stringify(resource).matchAll(/"(arn:aws:[^"]*)"/g)) found.add(match[1] as string);
    }
  }
  return found;
}

/**
 * Build an ARN from live cluster-settings values and the static builder, not
 * from the instance method under test. A method that returns some other live
 * policy ARN must not pass.
 */
function arnFromSettings(
  settings: ClusterConfig,
  service: string,
  resource: string,
  accountId?: string,
  region?: string,
): string {
  return ArnBuilder.buildArn({
    partition: settings.getString('cluster.aws.partition'),
    service,
    region: region ?? settings.getString('cluster.aws.region'),
    accountId: accountId ?? settings.getString('cluster.aws.account_id'),
    resource,
  });
}

describe('ArnBuilder against the live dev27 IAM policy documents', () => {
  const settings = ClusterConfig.fromFile(readFileSync(SCAN, 'utf-8'), readFileSync(MODULES, 'utf-8'));
  const dev27 = new ArnBuilder(settings);
  const live = liveIamPolicyArns();
  const cluster = settings.getString('cluster.cluster_name') as string;
  const region = settings.getString('cluster.aws.region') as string;
  const dnsSuffix = settings.getString('cluster.aws.dns_suffix') as string;
  const bucket = settings.getString('cluster.cluster_s3_bucket') as string;
  const userPool = settings.getString('identity-provider.cognito.user_pool_id') as string;
  const directory = settings.getString('directoryservice.directory_id') as string;
  const clusterManagerId = settings.moduleId('cluster-manager');
  const directoryServiceId = settings.moduleId('directoryservice');

  const scalars: Array<[string, () => string, string]> = [
    [
      'getLambdaLogGroupArn()',
      () => dev27.getLambdaLogGroupArn(),
      arnFromSettings(settings, 'logs', `log-group:/aws/lambda/${cluster}*`),
    ],
    [
      'lambdaLogStreamArn',
      () => dev27.lambdaLogStreamArn,
      arnFromSettings(settings, 'logs', `log-group:/aws/lambda/${cluster}*:log-stream:*`),
    ],
    [
      'getAdAutomationSqsQueueArn()',
      () => dev27.getAdAutomationSqsQueueArn(),
      arnFromSettings(settings, 'sqs', `${cluster}-${directoryServiceId}-ad-automation.fifo`),
    ],
    [
      'getProjectRoleArn()',
      () => dev27.getProjectRoleArn(),
      arnFromSettings(settings, 'iam', `role/idea/${cluster}/projects/*`, undefined, ''),
    ],
    [
      'getProjectPolicyArn()',
      () => dev27.getProjectPolicyArn(),
      arnFromSettings(settings, 'iam', `policy/idea/${cluster}/projects/*`, undefined, ''),
    ],
    [
      'getProjectInstanceProfileArn()',
      () => dev27.getProjectInstanceProfileArn(),
      arnFromSettings(settings, 'iam', `instance-profile/idea/${cluster}/projects/*`, undefined, ''),
    ],
    [
      'getProjectPermissionsBoundaryArn()',
      () => dev27.getProjectPermissionsBoundaryArn(),
      arnFromSettings(
        settings,
        'iam',
        `policy/${cluster}-${region}-${clusterManagerId}-project-boundary`,
        undefined,
        '',
      ),
    ],
    [
      'getDdbApplicationAutoscalingServiceRoleArn()',
      () => dev27.getDdbApplicationAutoscalingServiceRoleArn(),
      arnFromSettings(
        settings,
        'iam',
        'role/aws-service-role/dynamodb.application-autoscaling.amazonaws.com/AWSServiceRoleForApplicationAutoScaling_DynamoDBTable',
        undefined,
        '',
      ),
    ],
    [
      'bedrockInvocationLogGroupArn',
      () => dev27.bedrockInvocationLogGroupArn,
      arnFromSettings(settings, 'logs', `log-group:/${cluster}/${clusterManagerId}/bedrock-invocations`),
    ],
    [
      'bedrockApplicationInferenceProfileArn',
      () => dev27.bedrockApplicationInferenceProfileArn,
      arnFromSettings(settings, 'bedrock', 'application-inference-profile/*'),
    ],
    [
      'bedrockSystemInferenceProfileArn',
      () => dev27.bedrockSystemInferenceProfileArn,
      arnFromSettings(settings, 'bedrock', 'inference-profile/*'),
    ],
    [
      'bedrockAnySystemInferenceProfileArn',
      () => dev27.bedrockAnySystemInferenceProfileArn,
      arnFromSettings(settings, 'bedrock', 'inference-profile/*', '*', '*'),
    ],
    [
      'bedrockFoundationModelArn',
      () => dev27.bedrockFoundationModelArn,
      arnFromSettings(settings, 'bedrock', 'foundation-model/*', '', '*'),
    ],
    [
      'userPoolArn',
      () => dev27.userPoolArn,
      arnFromSettings(settings, 'cognito-idp', `userpool/${userPool}`),
    ],
    [
      'getDirectoryServiceArn()',
      () => dev27.getDirectoryServiceArn(),
      arnFromSettings(settings, 'ds', `directory/${directory}`),
    ],
    ['getKinesisArn()', () => dev27.getKinesisArn(), arnFromSettings(settings, 'kinesis', `stream/${cluster}-*`)],
    ['sesArn', () => dev27.sesArn, arnFromSettings(settings, 'ses', 'identity/*', undefined, '*')],
    [
      'getRoute53HostedzoneArn()',
      () => dev27.getRoute53HostedzoneArn(),
      arnFromSettings(settings, 'route53', 'hostedzone/*', '', ''),
    ],
  ];

  const lists: Array<[string, () => string[], string[]]> = [
    [
      'ec2CommonArns',
      () => dev27.ec2CommonArns,
      [
        arnFromSettings(settings, 'ec2', 'subnet/*', '*', '*'),
        arnFromSettings(settings, 'ec2', 'key-pair/*', undefined, '*'),
        arnFromSettings(settings, 'ec2', 'instance/*', undefined, '*'),
        arnFromSettings(settings, 'ec2', 'snapshot/*', '*', '*'),
        arnFromSettings(settings, 'ec2', 'launch-template/*', undefined, '*'),
        arnFromSettings(settings, 'ec2', 'volume/*', undefined, '*'),
        arnFromSettings(settings, 'ec2', 'security-group/*', undefined, '*'),
        arnFromSettings(settings, 'ec2', 'placement-group/*', undefined, '*'),
        arnFromSettings(settings, 'ec2', 'network-interface/*', undefined, '*'),
        arnFromSettings(settings, 'ec2', 'spot-instances-request/*', '*', '*'),
        arnFromSettings(settings, 'ec2', 'image/*', '*', '*'),
      ],
    ],
    [
      's3GlobalArns',
      () => dev27.s3GlobalArns,
      [
        arnFromSettings(settings, 's3', `dcv-license.${region}/*`, '', ''),
        arnFromSettings(settings, 's3', 'ec2-linux-nvidia-drivers/*', '', ''),
        arnFromSettings(settings, 's3', 'ec2-linux-nvidia-drivers', '', ''),
        arnFromSettings(settings, 's3', 'ec2-windows-nvidia-drivers/*', '', ''),
        arnFromSettings(settings, 's3', 'ec2-windows-nvidia-drivers', '', ''),
        arnFromSettings(settings, 's3', 'nvidia-gaming/*', '', ''),
        arnFromSettings(settings, 's3', 'nvidia-gaming-drivers', '', ''),
        arnFromSettings(settings, 's3', 'nvidia-gaming-drivers/*', '', ''),
        arnFromSettings(settings, 's3', 'ec2-amd-linux-drivers/*', '', ''),
        arnFromSettings(settings, 's3', 'ec2-amd-linux-drivers', '', ''),
        arnFromSettings(settings, 's3', 'ec2-amd-windows-drivers/*', '', ''),
        arnFromSettings(settings, 's3', 'ec2-amd-windows-drivers', '', ''),
      ],
    ],
    [
      'dcvLicenseS3BucketArns',
      () => dev27.dcvLicenseS3BucketArns,
      [
        arnFromSettings(settings, 's3', 'dcv-license.*/*', '', ''),
        arnFromSettings(settings, 's3', 'dcv-license.*', '', ''),
      ],
    ],
    [
      's3BucketArns',
      () => dev27.s3BucketArns,
      [
        arnFromSettings(settings, 's3', `${bucket}/*`, '', ''),
        arnFromSettings(settings, 's3', `${bucket}`, '', ''),
      ],
    ],
    [
      'clusterConfigDdbArn',
      () => dev27.clusterConfigDdbArn,
      [
        arnFromSettings(settings, 'dynamodb', `table/${cluster}.cluster-settings`),
        arnFromSettings(settings, 'dynamodb', `table/${cluster}.cluster-settings/stream/*`),
        arnFromSettings(settings, 'dynamodb', `table/${cluster}.modules`),
      ],
    ],
    [
      'serviceRoleArns',
      () => dev27.serviceRoleArns,
      [
        arnFromSettings(settings, 'iam', `role/aws-service-role/s3.data-source.lustre.fsx.${dnsSuffix}/*`, undefined, ''),
        arnFromSettings(settings, 'iam', `role/aws-service-role/autoscaling.${dnsSuffix}/*`, undefined, ''),
        arnFromSettings(settings, 'iam', `role/aws-service-role/spotfleet.${dnsSuffix}/*`, undefined, ''),
        arnFromSettings(settings, 'iam', `role/aws-service-role/fsx.${dnsSuffix}/*`, undefined, ''),
      ],
    ],
  ];

  for (const [label, build, expected] of scalars) {
    it(`${label} is a literal in a live IAM policy document`, () => {
      const arn = build();
      assert.equal(arn, expected);
      assert.ok(live.has(arn), `${label} result is not among ${live.size} live policy arns`);
    });
  }

  for (const [label, build, expected] of lists) {
    it(`${label}: all ${expected.length} arns are literals in a live IAM policy document`, () => {
      const value = build();
      assert.deepEqual(value, expected);
      for (const arn of value) assert.ok(live.has(arn), `${arn} not found among ${live.size} live policy arns`);
    });
  }
});
