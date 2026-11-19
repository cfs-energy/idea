/**
 * The desktop, compute node and control-plane task policies grant what their callers use and no
 * more. Each case fails if a wide grant this release removed comes back. Synthetic settings only,
 * so it runs in a public checkout.
 */

import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { renderPolicy, resourcesDir } from '../../src/cdk/policy.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';

const CLUSTER = 'example';
const ACCOUNT = '123456789012';
const BUCKET = 'example-cluster-us-east-2-bucket';

const ROWS: Record<string, string> = {
  'global-settings.module_sets.default.virtual-desktop-controller.module_id': 'vdc',
  'global-settings.module_sets.default.cluster-manager.module_id': 'cluster-manager',
  'global-settings.module_sets.default.scheduler.module_id': 'scheduler',
  'cluster.cluster_name': CLUSTER,
  'cluster.aws.region': 'us-east-2',
  'cluster.aws.dns_suffix': 'amazonaws.com',
  'cluster.aws.partition': 'aws',
  'cluster.aws.account_id': ACCOUNT,
  'cluster.cluster_s3_bucket': BUCKET,
  'cluster.network.vpc_id': 'vpc-0123456789abcdef0',
  'identity-provider.cognito.user_pool_id': 'example',
};

interface Statement {
  Effect: string;
  Action: string | string[];
  Resource?: string | string[];
  NotResource?: string | string[];
  Condition?: Record<string, Record<string, string | string[]>>;
}

const asList = (value: string | string[] | undefined): string[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

function render(template: string, moduleId?: string, rows: Record<string, string> = ROWS): Statement[] {
  const config = ClusterConfig.fromFile(
    JSON.stringify({ Items: Object.entries(rows).map(([key, value]) => ({ key: { S: key }, value: { S: value } })) }),
    JSON.stringify({ Items: [] }),
  );
  const document = renderPolicy(template, {
    config,
    moduleId,
    vars: { compute_node_role_arn: 'arn:aws:iam::0:role/c', scheduler_role_arn: 'arn:aws:iam::0:role/s', spot_fleet_request_role_arn: 'arn:aws:iam::0:role/f' },
  }) as { Statement: Statement[] };
  return document.Statement;
}

/** IAM's action match: case-insensitive, `*` and `?` wildcards. */
const actionMatches = (pattern: string, action: string): boolean =>
  new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`, 'i').test(action);

/** Every allow statement that grants `action`, by wildcard as IAM matches it (`fsx:*` grants `fsx:CreateFileSystem`). */
const granting = (statements: Statement[], action: string): Statement[] =>
  statements.filter((statement) => statement.Effect === 'Allow' && asList(statement.Action).some((pattern) => actionMatches(pattern, action)));

/** True when `action` is allowed on every resource with no condition. */
const wide = (statements: Statement[], action: string): boolean =>
  granting(statements, action).some((statement) =>
    statement.Condition === undefined && asList(statement.Resource).includes('*'));

const ALL_TEMPLATES = [
  'compute-node.yml',
  'scheduler.yml',
  'project-role-boundary.yml',
  'virtual-desktop-controller.yml',
  'virtual-desktop-dcv-broker.yml',
  'virtual-desktop-dcv-host.yml',
];

describe('desktop gateway and broker tasks', () => {
  it('the gateway has no policy template: its task role holds only the instance managed policies', () => {
    assert.equal(existsSync(join(resourcesDir(), 'policies', 'virtual-desktop-dcv-connection-gateway.yml')), false);
  });

  it('the broker keeps its own tables and metrics, and no instance, command or role grant', () => {
    const statements = render('virtual-desktop-dcv-broker.yml');
    const actions = statements.flatMap((statement) => asList(statement.Action)).sort();
    for (const action of actions) assert.match(action, /^(dynamodb|cloudwatch):/, `broker grants ${action}`);
    for (const statement of granting(statements, 'dynamodb:CreateTable')) {
      // only its own tables: the controller's share the module prefix (vdc.controller.session-permissions)
      assert.deepEqual(asList(statement.Resource), [`arn:aws:dynamodb:us-east-2:${ACCOUNT}:table/${CLUSTER}.vdc.dcv-broker.*`]);
    }
    for (const statement of granting(statements, 'dynamodb:DeleteItem')) {
      assert.ok(!asList(statement.Resource).some((arn) => new RegExp(`^${arn.replace(/[.]/g, "\\.").replace(/\*/g, ".*")}$`).test(`arn:aws:dynamodb:us-east-2:${ACCOUNT}:table/${CLUSTER}.vdc.controller.session-permissions`)));
    }
  });
});

describe('desktop controller', () => {
  const statements = render('virtual-desktop-controller.yml', 'vdc');
  const instanceArn = `arn:aws:ec2:us-east-2:${ACCOUNT}:instance/*`;

  for (const action of [
    'ec2:StartInstances', 'ec2:StopInstances', 'ec2:RebootInstances', 'ec2:TerminateInstances',
    'ec2:ModifyInstanceAttribute', 'ec2:DeleteTags',
  ]) {
    it(`${action} reaches only instances tagged with this cluster and the desktop module`, () => {
      const found = granting(statements, action);
      assert.equal(found.length, 1);
      assert.deepEqual(asList(found[0]!.Resource), [instanceArn]);
      assert.deepEqual(found[0]!.Condition, { StringEquals: { 'aws:ResourceTag/idea:ClusterName': CLUSTER, 'aws:ResourceTag/idea:ModuleId': 'vdc' } });
    });
  }

  it('a launch must tag its instance with this cluster, and every other launch resource is open', () => {
    const found = granting(statements, 'ec2:RunInstances');
    assert.equal(found.length, 2);
    const onInstance = found.find((statement) => statement.Resource !== undefined)!;
    assert.deepEqual(asList(onInstance.Resource), [instanceArn]);
    assert.deepEqual(onInstance.Condition, { StringEquals: { 'aws:RequestTag/idea:ClusterName': CLUSTER } });
    const others = found.find((statement) => statement.NotResource !== undefined)!;
    // every region and account, so no instance anywhere is reached without the tag condition
    assert.deepEqual(asList(others.NotResource), ['arn:aws:ec2:*:*:instance/*']);
    assert.equal(others.Condition, undefined);
  });

  it('an image is taken only of this cluster\'s instances', () => {
    const found = granting(statements, 'ec2:CreateImage');
    assert.equal(found.length, 2);
    assert.ok(found.some((statement) => asList(statement.Resource).includes(instanceArn)
      && statement.Condition?.StringEquals?.['aws:ResourceTag/idea:ClusterName'] === CLUSTER));
    assert.ok(found.some((statement) => asList(statement.NotResource).includes('arn:aws:ec2:*:*:instance/*')));
  });

  it('tags are written on existing cluster instances, or by the launch or image call itself', () => {
    const found = granting(statements, 'ec2:CreateTags');
    assert.equal(found.length, 2);
    for (const statement of found) {
      assert.ok(!asList(statement.Resource).includes('*'));
      assert.ok(statement.Condition !== undefined);
    }
    const atCreate = found.find((statement) => statement.Condition?.StringEquals?.['ec2:CreateAction'] !== undefined)!;
    assert.deepEqual(asList(atCreate.Condition!.StringEquals!['ec2:CreateAction']), ['RunInstances', 'CreateImage']);
  });

  it('Run Command sends only the two AWS shell documents, to this cluster\'s desktop instances', () => {
    const found = granting(statements, 'ssm:SendCommand');
    assert.equal(found.length, 2);
    const documents = found.find((statement) => statement.Condition === undefined)!;
    assert.deepEqual(asList(documents.Resource), [
      'arn:aws:ssm:us-east-2::document/AWS-RunShellScript',
      'arn:aws:ssm:us-east-2::document/AWS-RunPowerShellScript',
    ]);
    const instances = found.find((statement) => statement.Condition !== undefined)!;
    assert.deepEqual(asList(instances.Resource), [instanceArn]);
    assert.deepEqual(instances.Condition, { StringEquals: { 'ssm:resourceTag/idea:ClusterName': CLUSTER, 'ssm:resourceTag/idea:ModuleId': 'vdc' } });
  });
});

describe('desktop and compute hosts', () => {
  it('a desktop sends to the controller queue and never reads it', () => {
    const statements = render('virtual-desktop-dcv-host.yml');
    const queue = `arn:aws:sqs:us-east-2:${ACCOUNT}:${CLUSTER}-vdc-events.fifo`;
    const onQueue = statements.filter((statement) => asList(statement.Resource).includes(queue));
    assert.deepEqual(onQueue.flatMap((statement) => asList(statement.Action)), ['sqs:SendMessage']);
  });

  const bucketGrants = (template: string, moduleId?: string, rows?: Record<string, string>) =>
    render(template, moduleId, rows).filter((statement) =>
      asList(statement.Resource).some((arn) => arn.startsWith(`arn:aws:s3:::${BUCKET}`)));

  it('a desktop reads only bootstrap packages from the cluster bucket, never the whole bucket', () => {
    const grants = bucketGrants('virtual-desktop-dcv-host.yml');
    const objects = grants.flatMap((statement) => asList(statement.Resource)).filter((arn) => arn.includes('/'));
    assert.deepEqual(objects, [
      `arn:aws:s3:::${BUCKET}/idea/vdc/dcv-host-bootstrap/*`,
      `arn:aws:s3:::${BUCKET}/idea/vdc/bootstrap/*`,
      // a project role runs compute nodes too
      `arn:aws:s3:::${BUCKET}/idea/scheduler/bootstrap/*`,
    ]);
    // a host fetches the one key it is given; a listing would name every other desktop's package
    for (const statement of grants) assert.deepEqual(asList(statement.Action), ['s3:GetObject']);
  });

  it('a desktop on a cluster without the scheduler reads no scheduler prefix', () => {
    const rows = { ...ROWS };
    delete rows['global-settings.module_sets.default.scheduler.module_id'];
    const arns = bucketGrants('virtual-desktop-dcv-host.yml', undefined, rows).flatMap((statement) => asList(statement.Resource));
    assert.ok(!arns.some((arn) => arn.includes('/idea/scheduler/')));
  });

  it('a compute node reads only its bootstrap packages from the cluster bucket', () => {
    const grants = bucketGrants('compute-node.yml', 'scheduler');
    assert.deepEqual(grants.flatMap((statement) => asList(statement.Resource)), [
      `arn:aws:s3:::${BUCKET}/idea/scheduler/bootstrap/*`,
    ]);
    for (const statement of grants) assert.deepEqual(asList(statement.Action), ['s3:GetObject']);
  });

  const group = (suffix: string) => `arn:aws:logs:us-east-2:${ACCOUNT}:log-group:/${CLUSTER}${suffix}`;
  for (const [template, moduleId, groups] of [
    // hosts set none (see 'log retention is set by whoever creates the group')
    ['virtual-desktop-controller.yml', 'vdc', ['/*']],
  ] as const) {
    it(`${template} sets log retention only on the groups its hosts write`, () => {
      const found = granting(render(template, moduleId), 'logs:PutRetentionPolicy');
      assert.ok(found.length > 0);
      assert.deepEqual(found.flatMap((statement) => asList(statement.Resource)), groups.map(group));
    });
  }

  it('a desktop writes only its own, its image builder\'s, its sessions\' and project job log groups', () => {
    const statements = render('virtual-desktop-dcv-host.yml');
    const found = granting(statements, 'logs:PutLogEvents');
    assert.deepEqual(found.flatMap((statement) => asList(statement.Resource)),
      ['/vdc/dcv-host*', '/vdc/ami-builder*', '/vdc/dcv-session/*', '/scheduler/job-*'].map(group));
  });

  it('the CloudWatch agent policy every host and task carries writes only under the cluster tree', () => {
    const statements = render('cloud-watch-agent-server-policy.yml');
    for (const action of ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents']) {
      assert.deepEqual(granting(statements, action).flatMap((statement) => asList(statement.Resource)), [group('/*')], action);
    }
  });

  for (const [template, moduleId] of [['compute-node.yml', 'scheduler'], ['virtual-desktop-dcv-host.yml', undefined], ['openldap-server.yml', undefined]] as const) {
    it(`${template}: volumes and interfaces naming another cluster are not retagged from the host`, () => {
      const found = granting(render(template, moduleId), 'ec2:CreateTags')
        .filter((statement) => asList(statement.Resource).some((arn) => /volume|network-interface/.test(arn)));
      assert.equal(found.length, 1);
      assert.deepEqual(asList(found[0]!.Resource), [
        `arn:aws:ec2:us-east-2:${ACCOUNT}:volume/*`, `arn:aws:ec2:us-east-2:${ACCOUNT}:network-interface/*`,
      ]);
      assert.deepEqual(found[0]!.Condition, { StringEqualsIfExists: { 'aws:ResourceTag/idea:ClusterName': CLUSTER } });
    });
  }

  for (const [template, moduleId] of [['compute-node.yml', 'scheduler'], ['virtual-desktop-dcv-host.yml', undefined]] as const) {
    it(`${template}: a host tags only its own instance, and only with its status keys`, () => {
      const onInstances = granting(render(template, moduleId), 'ec2:CreateTags')
        .filter((statement) => asList(statement.Resource).some((arn) => /instance/i.test(arn)));
      assert.equal(onInstances.length, 1);
      // IAM accepts a variable only in the resource part of an ARN, so the self match is a condition
      assert.deepEqual(asList(onInstances[0]!.Resource), [`arn:aws:ec2:us-east-2:${ACCOUNT}:instance/*`]);
      assert.deepEqual(onInstances[0]!.Condition, {
        ArnEquals: { 'ec2:SourceInstanceARN': `arn:aws:ec2:us-east-2:${ACCOUNT}:instance/\${ec2:InstanceID}` },
        'ForAllValues:StringEquals': { 'aws:TagKeys': ['idea:BootstrapStatus', 'idea:AmiBuilderStatus'] },
      });
    });
  }

  it('the desktop host writes logs only under the cluster tree', () => {
    const statements = render('virtual-desktop-dcv-host.yml');
    for (const action of ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents']) {
      assert.equal(wide(statements, action), false, action);
    }
  });
});

it('no template grants fsx:CreateDataRepositoryTask, which nothing calls', () => {
  for (const template of ALL_TEMPLATES) {
    const moduleId = template.startsWith('compute') || template.startsWith('scheduler') ? 'scheduler' : 'vdc';
    assert.equal(granting(render(template, moduleId), 'fsx:CreateDataRepositoryTask').length, 0, template);
  }
});

describe('service and host grants a host shell or service task does not need', () => {
  it('a host or task reads only the public AWS parameters, not a workload\'s own', () => {
    const found = granting(render('amazon-ssm-managed-instance-core.yml'), 'ssm:GetParameter');
    assert.deepEqual(found.flatMap((statement) => asList(statement.Resource)), ['arn:aws:ssm:*::parameter/aws/*']);
    assert.deepEqual(granting(render('amazon-ssm-managed-instance-core.yml'), 'ssm:GetParameters'), found);
  });

  it('a DynamoDB key grant is created only by DynamoDB on the caller\'s behalf', () => {
    const rows = { ...ROWS, 'cluster.dynamodb.kms_key_id': 'example-key' };
    for (const [template, moduleId] of [['compute-node.yml', 'scheduler'], ['virtual-desktop-dcv-host.yml', undefined]] as const) {
      const found = granting(render(template, moduleId, rows), 'kms:CreateGrant');
      assert.equal(found.length, 1, template);
      assert.deepEqual(found[0]!.Condition, { Bool: { 'kms:GrantIsForAWSResource': 'true' } });
    }
  });

  for (const [template, moduleId] of [['scheduler.yml', 'scheduler'], ['virtual-desktop-controller.yml', 'vdc']] as const) {
    it(`${template} records a token's use and writes no other token attribute`, () => {
      const found = granting(render(template, moduleId), 'dynamodb:UpdateItem')
        .filter((statement) => asList(statement.Resource).some((arn) => arn.includes('api-tokens')));
      assert.equal(found.length, 1);
      assert.deepEqual(asList(found[0]!.Resource), [`arn:aws:dynamodb:us-east-2:${ACCOUNT}:table/${CLUSTER}.cluster-manager.api-tokens`]);
      assert.deepEqual(found[0]!.Condition, { 'ForAllValues:StringEquals': { 'dynamodb:Attributes': ['token_id', 'last_used_on'] } });
    });
  }

  it('the scheduler invokes only the solution metrics function and tags an instance only at launch or when it is the cluster\'s', () => {
    const statements = render('scheduler.yml', 'scheduler');
    // old job stacks' SendAnonymousData custom resource is invoked with the scheduler's credentials on delete
    assert.deepEqual(granting(statements, 'lambda:InvokeFunction').flatMap((statement) => asList(statement.Resource)),
      [`arn:aws:lambda:us-east-2:${ACCOUNT}:function:${CLUSTER}-solution-metrics`]);
    const onInstances = granting(statements, 'ec2:CreateTags').filter((statement) => asList(statement.Resource).some((arn) => arn.includes(':instance/')));
    assert.equal(onInstances.length, 2);
    for (const statement of onInstances) {
      assert.deepEqual(asList(statement.Resource), [`arn:aws:ec2:us-east-2:${ACCOUNT}:instance/*`]);
      assert.ok(statement.Condition !== undefined);
    }
  });

  it('the scheduler sends Run Command only to this cluster\'s image builders and compute nodes', () => {
    const found = granting(render('scheduler-image-pipeline.yml', 'scheduler'), 'ssm:SendCommand');
    const onInstances = found.filter((statement) => asList(statement.Resource).some((arn) => arn.includes(':instance/')));
    assert.equal(onInstances.length, 1);
    assert.deepEqual(onInstances[0]!.Condition, {
      StringEquals: { 'ssm:resourceTag/idea:ClusterName': CLUSTER, 'ssm:resourceTag/idea:ModuleId': 'scheduler', 'ssm:resourceTag/idea:NodeType': ['ami-builder', 'compute-node'] },
    });
  });
});

describe('runtime services and the objects deployment writes', () => {
  const rows = { ...ROWS, 'global-settings.module_sets.default.directoryservice.module_id': 'directoryservice' };
  const bucket = `arn:aws:s3:::${BUCKET}`;
  for (const [template, moduleId, own] of [
    ['scheduler.yml', 'scheduler', ['idea/scheduler/bootstrap/', 'logs/scheduler/']],
    ['virtual-desktop-controller.yml', 'vdc', ['idea/vdc/bootstrap/', 'idea/vdc/dcv-host-bootstrap/']],
  ] as const) {
    it(`${template} writes only its own host packages${moduleId === 'scheduler' ? ' and log archive' : ''} in the cluster bucket, and deletes nothing`, () => {
      const statements = render(template, moduleId, rows);
      const writes = granting(statements, 's3:PutObject').flatMap((statement) => asList(statement.Resource)).filter((arn) => arn.startsWith(bucket));
      assert.deepEqual(writes, own.map((prefix) => `${bucket}/${prefix}*`));
      assert.deepEqual(granting(statements, 's3:DeleteObject').flatMap((statement) => asList(statement.Resource)).filter((arn) => arn.startsWith(bucket)), []);
    });
  }
});

describe('the OpenLDAP server', () => {
  const rows = { ...ROWS, 'global-settings.module_sets.default.directoryservice.module_id': 'directoryservice', 'directoryservice.provider': 'openldap' };
  const statements = render('openldap-server.yml', 'directoryservice', rows);
  it('reads only its bootstrap package from the cluster bucket and lists nothing', () => {
    assert.deepEqual(granting(statements, 's3:GetObject').flatMap((statement) => asList(statement.Resource)), [`arn:aws:s3:::${BUCKET}/idea/bootstrap/*`]);
    assert.deepEqual(granting(statements, 's3:ListBucket'), []);
  });
  it('may write its own bootstrap status tag', () => {
    const own = granting(statements, 'ec2:CreateTags').filter((statement) => asList(statement.Resource).some((arn) => arn.includes(':instance/')));
    assert.equal(own.length, 1);
    assert.deepEqual(own[0]!.Condition, {
      ArnEquals: { 'ec2:SourceInstanceARN': `arn:aws:ec2:us-east-2:${ACCOUNT}:instance/\${ec2:InstanceID}` },
      'ForAllValues:StringEquals': { 'aws:TagKeys': ['idea:BootstrapStatus'] },
    });
  });
});

describe('a GPU driver mirror in the cluster bucket', () => {
  const rows = { ...ROWS, 'global-settings.gpu_settings.nvidia.linux.s3_bucket_path': `s3://${BUCKET}/mirrors/nvidia/` };
  for (const [template, moduleId] of [['compute-node.yml', 'scheduler'], ['virtual-desktop-dcv-host.yml', 'vdc']] as const) {
    it(`${template} lists and reads the configured prefix only`, () => {
      const statements = render(template, moduleId, rows);
      assert.ok(granting(statements, 's3:GetObject').some((statement) => asList(statement.Resource).includes(`arn:aws:s3:::${BUCKET}/mirrors/nvidia/*`)));
      const lists = granting(statements, 's3:ListBucket').filter((statement) => asList(statement.Resource).includes(`arn:aws:s3:::${BUCKET}`));
      assert.equal(lists.length, 1);
      assert.deepEqual(lists[0]!.Condition, { StringLike: { 's3:prefix': 'mirrors/nvidia/*' } });
    });
    it(`${template} adds nothing for the default driver buckets`, () => {
      const statements = render(template, moduleId, ROWS);
      assert.ok(!granting(statements, 's3:ListBucket').some((statement) => asList(statement.Resource).includes(`arn:aws:s3:::${BUCKET}`)));
    });
  }
});

describe('lows: certificate lookup, notification role, agent policy, key ARNs', () => {
  it('the certificate lookup function only lists secrets and certificates', () => {
    const actions = render('custom-resource-self-signed-certificate.yml').flatMap((statement) => asList(statement.Action)).sort();
    assert.deepEqual(actions, ['acm:ListCertificates', 'logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'secretsmanager:ListSecrets']);
  });
  it('the Run Command notification role publishes and writes session logs, and touches no bucket', () => {
    const actions = render('controller-ssm-command-pass-role.yml', 'vdc').flatMap((statement) => asList(statement.Action));
    assert.ok(!actions.some((action) => action.startsWith('s3:') || action === 'logs:PutRetentionPolicy'), actions.join(', '));
    assert.ok(actions.includes('sns:Publish'));
  });
  it('no shared host policy reads a parameter outside parameter/aws/*', () => {
    for (const template of ['cloud-watch-agent-server-policy.yml', 'amazon-ssm-managed-instance-core.yml']) {
      for (const statement of granting(render(template), 'ssm:GetParameter')) {
        for (const arn of asList(statement.Resource)) assert.match(arn, /:parameter\/aws\/\*$/, `${template}: ${arn}`);
      }
    }
  });
  it('a key ARN in a kms_key_id setting is used as given', () => {
    const arn = `arn:aws:kms:us-east-2:${ACCOUNT}:key/0123abcd-0123-0123-0123-0123456789ab`;
    const statements = render('scheduler.yml', 'scheduler', { ...ROWS, 'cluster.dynamodb.kms_key_id': arn });
    assert.ok(granting(statements, 'kms:DescribeKey').some((statement) => asList(statement.Resource).includes(arn)));
    const byId = render('scheduler.yml', 'scheduler', { ...ROWS, 'cluster.dynamodb.kms_key_id': '0123abcd-0123-0123-0123-0123456789ab' });
    assert.ok(granting(byId, 'kms:DescribeKey').some((statement) => asList(statement.Resource).includes(arn)));
  });
});

describe('log retention is set by whoever creates the group, not by hosts', () => {
  for (const [template, moduleId] of [['compute-node.yml', 'scheduler'], ['virtual-desktop-dcv-host.yml', 'vdc'], ['project-role-boundary.yml', 'vdc'], ['openldap-server.yml', 'directoryservice'], ['cluster-manager.yml', 'cluster-manager']] as const) {
    it(`${template} sets no retention`, () => {
      const rows = { ...ROWS, 'global-settings.module_sets.default.directoryservice.module_id': 'directoryservice', 'global-settings.module_sets.default.cluster-manager.module_id': 'cluster-manager' };
      assert.deepEqual(granting(render(template, moduleId, rows), 'logs:PutRetentionPolicy'), []);
    });
  }
  it('the scheduler creates job groups with retention, and only job groups', () => {
    const found = granting(render('scheduler.yml', 'scheduler'), 'logs:PutRetentionPolicy');
    assert.deepEqual(found.flatMap((statement) => asList(statement.Resource)), [`arn:aws:logs:us-east-2:${ACCOUNT}:log-group:/${CLUSTER}/scheduler/job-*`]);
    assert.ok(granting(render('scheduler.yml', 'scheduler'), 'logs:CreateLogGroup').length > 0);
  });
});
