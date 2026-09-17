// replaySynthReads against the dev27 fixture: the reads that shape a template, and a miss.
// Every real identifier is read out of the gitignored fixture at run time, never written here.
import { deepStrictEqual, match, ok, rejects, strictEqual } from 'node:assert';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { liveSynthReads, SynthReadMiss, replaySynthReads, synthReadKey } from '../../src/cdk/synth-reads.ts';
import { AwsProfileCredentialsError } from '../../src/cli/aws-client-options.ts';
import { requireCapture } from '../support/fixtures.ts';

const PKG = resolve(import.meta.dirname, '../..');
const FIXTURE = join(PKG, 'tools/parity/fixtures/idea-dev27/synth-reads.json');
requireCapture([FIXTURE], "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27");

/** The parameters of every captured read of one action, recovered from the keys. */
function params<T>(service: string, action: string): T[] {
  const prefix = `${service}:${action}:`;
  return Object.keys(JSON.parse(readFileSync(FIXTURE, 'utf8')))
    .filter((k) => k.startsWith(prefix))
    .map((k) => JSON.parse(k.slice(prefix.length)) as T);
}

test('the key format is service:action:JSON(params)', () => {
  strictEqual(synthReadKey('sts', 'GetCallerIdentity', {}), 'sts:GetCallerIdentity:{}');
  strictEqual(synthReadKey('iam', 'ListRoles', { PathPrefix: '/x' }), 'iam:ListRoles:{"PathPrefix":"/x"}');
});

test('the external ALB https listener replays its default action', async () => {
  const reads = replaySynthReads(FIXTURE);
  const arns = params<{ ListenerArns: string[] }>('elbv2', 'DescribeListeners').map((p) => p.ListenerArns[0]);
  strictEqual(arns.length, 5);
  const external = arns.find((a) => a.includes('-external-alb/'));
  ok(external, 'no external ALB listener in the fixture');
  const listener = await reads.describeListener(external);
  strictEqual(listener.ListenerArn, external);
  strictEqual(listener.Port, 443);
  // cluster_stack.get_alb_listener_default_actions only carries a `forward` action over,
  // which is how the cluster stack keeps cluster-manager's web portal as the default.
  strictEqual(listener.DefaultActions?.length, 1);
  strictEqual(listener.DefaultActions?.[0]?.Type, 'forward');
  match(listener.DefaultActions?.[0]?.TargetGroupArn ?? '', /:targetgroup\/[a-z0-9-]+-web-portal-[0-9a-f]{8}\//);
  // An internal listener replays its fixed-response default.
  const internal = arns.filter((a) => a !== external);
  const defaults = await Promise.all(internal.map((a) => reads.describeListener(a)));
  ok(defaults.some((l) => l.DefaultActions?.[0]?.Type === 'fixed-response'));
});

test('the other four reads replay', async () => {
  const reads = replaySynthReads(FIXTURE);
  const identity = await reads.callerIdentity();
  match(identity.account, /^\d{12}$/);
  ok(identity.arn.startsWith('arn:aws'));

  const [pool] = params<{ UserPoolId: string }>('cognito-idp', 'DescribeUserPool');
  const userPool = await reads.describeUserPool(pool.UserPoolId);
  // the invitation email must be replayed, or an upgrade silently rewrites it
  match(userPool.AdminCreateUserConfig?.InviteMessageTemplate?.EmailMessage ?? '', /You have been invited to join/);

  const [domain] = params<{ DomainName: string }>('opensearch', 'DescribeDomain');
  strictEqual((await reads.describeDomain(domain.DomainName)).ClusterConfig?.InstanceCount, 2);

  // check_service_linked_role_exists unions two prefixes; the second replays as []
  const prefixes = params<{ PathPrefix: string }>('iam', 'ListRoles').map((p) => p.PathPrefix);
  deepStrictEqual(prefixes.sort(), ['/aws-service-role/es.amazonaws.com', '/aws-service-role/opensearchservice.amazonaws.com']);
  strictEqual((await reads.listServiceLinkedRoles(prefixes[0])).length, 1);
  deepStrictEqual(await reads.listServiceLinkedRoles(prefixes[1]), []);
});

test('a miss throws SynthReadMiss naming the key', async () => {
  const reads = replaySynthReads(FIXTURE);
  const absent = 'arn:aws:elasticloadbalancing:us-east-2:000000000000:listener/app/nope/1/2';
  await rejects(
    () => reads.describeListener(absent),
    (e: Error) => {
      ok(e instanceof SynthReadMiss);
      strictEqual(e.message, `SynthReadMiss: elbv2:DescribeListeners:{"ListenerArns":["${absent}"]}`);
      return true;
    },
  );
  await rejects(() => reads.describeDomain('not-a-domain'), SynthReadMiss);
  await rejects(() => reads.describeUserPool('not-a-pool'), SynthReadMiss);
  await rejects(() => reads.listServiceLinkedRoles('/aws-service-role/nope'), SynthReadMiss);
});

/** Restore one environment variable without turning an absent value into a string. */
function restoreEnvironment(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

test("liveSynthReads selects a named profile without putting profile on the client", async () => {
  const previous = {
    profile: process.env.AWS_PROFILE,
    credentialsFile: process.env.AWS_SHARED_CREDENTIALS_FILE,
    configFile: process.env.AWS_CONFIG_FILE,
    accessKeyId: process.env.AWS_ACCESS_KEY_ID,
    secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
    sessionToken: process.env.AWS_SESSION_TOKEN,
    metadata: process.env.AWS_EC2_METADATA_DISABLED,
  };
  const directory = mkdtempSync(join(tmpdir(), "ideactl-synth-reads-"));
  writeFileSync(join(directory, "credentials"), "");
  writeFileSync(join(directory, "config"), "");
  process.env.AWS_SHARED_CREDENTIALS_FILE = join(directory, "credentials");
  process.env.AWS_CONFIG_FILE = join(directory, "config");
  process.env.AWS_EC2_METADATA_DISABLED = "true";
  delete process.env.AWS_ACCESS_KEY_ID;
  delete process.env.AWS_SECRET_ACCESS_KEY;
  delete process.env.AWS_SESSION_TOKEN;

  try {
    const reads = liveSynthReads("us-east-2", "ideactl-missing-profile");
    strictEqual(process.env.AWS_PROFILE, "ideactl-missing-profile");
    await rejects(
      () => reads.callerIdentity(),
      (error: unknown) => {
        ok(error instanceof AwsProfileCredentialsError);
        strictEqual(error.profile, "ideactl-missing-profile");
        match(error.message, /AWS profile ideactl-missing-profile was not found/);
        return true;
      },
    );

    const source = readFileSync(new URL("../../src/cdk/synth-reads.ts", import.meta.url), "utf8");
    const liveFn = source.slice(source.indexOf("export function liveSynthReads"));
    ok(liveFn.includes("STSClient(await config())"));
    ok(liveFn.includes("awsClientOptions(region, profile)"));
    ok(!liveFn.includes("config.profile"));
  } finally {
    rmSync(directory, { recursive: true, force: true });
    restoreEnvironment("AWS_PROFILE", previous.profile);
    restoreEnvironment("AWS_SHARED_CREDENTIALS_FILE", previous.credentialsFile);
    restoreEnvironment("AWS_CONFIG_FILE", previous.configFile);
    restoreEnvironment("AWS_ACCESS_KEY_ID", previous.accessKeyId);
    restoreEnvironment("AWS_SECRET_ACCESS_KEY", previous.secretAccessKey);
    restoreEnvironment("AWS_SESSION_TOKEN", previous.sessionToken);
    restoreEnvironment("AWS_EC2_METADATA_DISABLED", previous.metadata);
  }
});
