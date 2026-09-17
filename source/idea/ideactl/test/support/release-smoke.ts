import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// The identity provider needs no compute instances or live service reads for a new pool.
// Its claim handler exercises prebuilt Lambda assets through the real application entry point.
export function smokeRelease(executable: string, runtimePath?: string): void {
  const root = mkdtempSync(join(tmpdir(), 'ideactl release smoke-'));
  try {
    for (const name of ['home', 'tmp', 'work', 'empty-path']) mkdirSync(join(root, name));
    const work = join(root, 'work');
    const account = '1'.repeat(12);
    const region = 'us-east-2';
    const settings: Record<string, string> = {
      'cluster.cluster_name': 'idea-test1',
      'cluster.aws.account_id': account,
      'cluster.aws.region': region,
      'cluster.aws.partition': 'aws',
      'cluster.aws.dns_suffix': 'amazonaws.com',
      'cluster.cluster_s3_bucket': 'idea-test1-assets',
      'cluster.cluster_settings_lambda_arn': `arn:aws:lambda:${region}:${account}:function:settings`,
      'cluster.network.vpc_id': 'vpc-test',
      'cluster.network.security_groups.default': 'sg-test',
      'cluster.iam.roles.log-retention': `arn:aws:iam::${account}:role/log-retention`,
      'cluster.load_balancers.external_alb.load_balancer_dns_name': 'cluster.example.invalid',
      'identity-provider.provider': 'cognito-idp',
      'identity-provider.cognito.removal_policy': 'RETAIN',
      'identity-provider.cognito.administrators_group_name': 'administrators',
      'identity-provider.cognito.managers_group_name': 'managers',
    };
    const json = (name: string, value: unknown): void => writeFileSync(join(work, name), JSON.stringify(value));
    json('settings.json', { Items: Object.entries(settings).map(([key, value]) => ({ key: { S: key }, value: { S: value } })) });
    json('reads.json', { 'sts:GetCallerIdentity:{}': { account, arn: `arn:aws:iam::${account}:root` } });
    json('cdk.context.json', {
      [`vpc-provider:account=${account}:filter.vpc-id=vpc-test:region=${region}:returnAsymmetricSubnets=true`]: {
        vpcId: 'vpc-test', vpcCidrBlock: '192.0.2.0/24', availabilityZones: [region + 'a'], subnetGroups: [],
      },
    });
    cpSync(new URL('../cli/shell-path-values.yml', import.meta.url), join(work, 'values.yml'));
    const run = (args: string[]): void => {
      const result = spawnSync(executable, args, {
        cwd: work, encoding: 'utf8',
        env: {
          // The CDK CLI runs the app command through the shell, which Node resolves from ComSpec.
          SystemRoot: process.env.SystemRoot, ComSpec: process.env.ComSpec,
          USERPROFILE: join(root, 'home'), TEMP: join(root, 'tmp'), TMP: join(root, 'tmp'),
          HOME: join(root, 'home'), IDEA_USER_HOME: join(root, 'home', '.idea'),
          TMPDIR: join(root, 'tmp'), PATH: runtimePath ?? join(root, 'empty-path'),
          AWS_EC2_METADATA_DISABLED: 'true', CDK_DISABLE_CLI_TELEMETRY: '1',
          NODE_PATH: '', LANG: 'en_US.UTF-8', CDK_OUTDIR: join(work, 'cdk.out'),
        },
      });
      assert.equal(result.status, 0, `${args.join(' ')}\n${result.stdout}\n${result.stderr}\n${result.error ?? ''}`);
    };
    run(['about']);
    if (runtimePath === undefined) run(['__ideactl_internal_cdk__', '--version']);
    run(['config', 'generate', '--values-file', join(work, 'values.yml'), '--config-dir', work, '--force']);
    const synthArgs = ['cdk', 'cdk-app', '--cluster-name', 'idea-test1', '--aws-region', region,
      '--module-id', 'identity-provider', '--module-name', 'identity-provider',
      '--deployment-id', 'release-smoke', '--config-file', join(work, 'settings.json'), '--synth-reads', join(work, 'reads.json'), '--termination-protection', 'true'];
    run(synthArgs);
    if (runtimePath === undefined) {
      // Exercise the deployment CLI's shell re-entry with spaces and no installed runtime.
      const quote = (value: string): string => /\s/.test(value) ? `"${value}"` : value;
      run(['__ideactl_internal_cdk__', 'synth', '--app', [executable, ...synthArgs].map(quote).join(' '),
        '--no-notices', '--no-lookups', '--no-version-reporting']);
    }
    const template = JSON.parse(readFileSync(join(work, 'cdk.out', 'idea-test1-identity-provider.template.json'), 'utf8')) as { Resources: Record<string, { Type: string; Properties?: { Handler?: string } }> };
    assert.ok(Object.values(template.Resources).some((resource) => resource.Type === 'AWS::Lambda::Function' && resource.Properties?.Handler === 'index.handler'));
    const manifest = JSON.parse(readFileSync(join(work, 'cdk.out', 'manifest.json'), 'utf8')) as { missing?: unknown[] };
    assert.deepEqual(manifest.missing ?? [], []);
    console.log('PASS extracted release: configuration and identity provider synthesis');
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  assert.ok(process.argv[2], 'release executable path is required');
  smokeRelease(resolve(process.argv[2]));
}
