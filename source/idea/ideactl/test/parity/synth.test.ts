// synth.ts: the harness runs the bundled CDK CLI in a fixture-only cwd and diffs the result.
import { match, strictEqual } from 'node:assert';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { requireAnyFixture, requireFixtures } from '../support/fixtures.ts';

const PKG = resolve(import.meta.dirname, '../..');
const SYNTH = join(PKG, 'tools/parity/synth.ts');
const FIXTURE = join(PKG, 'tools/parity/fixtures/idea-dev27');
requireAnyFixture(
  [join(FIXTURE, 'cdk.context.json'), join(FIXTURE, 'python/_cdk/cdk.context.json')],
  "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27",
);
requireFixtures(
  [
    join(FIXTURE, 'python/_cdk/cdk.out.metrics'),
    join(PKG, 'tools/parity/live/idea-dev27-metrics.json'),
  ],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

/**
 * Writes the live metrics template into a cloud assembly to exercise the harness.
 */
function fakeApp(): string {
  const dir = mkdtempSync(join(tmpdir(), 'ideactl-fakeapp-'));
  const file = join(dir, 'fake-app.mjs');
  writeFileSync(
    file,
    [
      "import { cpSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';",
      "import { join } from 'node:path';",
      `const PKG = ${JSON.stringify(PKG)};`,
      'const out = process.env.CDK_OUTDIR;',
      'mkdirSync(out, { recursive: true });',
      "cpSync(join(PKG, 'tools/parity/fixtures/idea-dev27/python/_cdk/cdk.out.metrics'), out, { recursive: true });",
      "const t = JSON.parse(readFileSync(join(PKG, 'tools/parity/live/idea-dev27-metrics.json'), 'utf8'));",
      "if (process.argv.includes('--mutate')) t.Resources.ideadev27metricssettings.Properties.module_id = 'mutated';",
      "writeFileSync(join(out, 'idea-dev27-metrics.template.json'), JSON.stringify(t));",
    ].join('\n'),
  );
  return `${process.execPath} ${file}`;
}

test('a synth that reproduces the live template reports PARITY', () => {
  const r = spawnSync(process.execPath, [SYNTH, '--cluster', 'idea-dev27', '--stack', 'metrics', '--app-override', fakeApp()], {
    encoding: 'utf8',
  });
  strictEqual(r.status, 0, r.stdout + r.stderr);
  match(r.stdout, /^PARITY  2 live resources, 0 missing, 0 extra, 0 property diffs, 0 soft$/m);
});

/**
 * A CDK app that emits one SNS topic whose DisplayName this test chooses.
 */
function displayNameApp(cluster: string, displayName: string): { command: string; dir: string } {
  const dir = mkdtempSync(join(PKG, "node_modules/.w4-against-app-"));
  const file = join(dir, "app.mjs");
  writeFileSync(
    file,
    [
      'import { App, CfnResource, Stack } from "aws-cdk-lib";',
      "const app = new App();",
      `const stack = new Stack(app, ${JSON.stringify(`${cluster}-metrics`)}, { stackName: ${JSON.stringify(`${cluster}-metrics`)} });`,
      `new CfnResource(stack, "marker", { type: "AWS::SNS::Topic", properties: { DisplayName: ${JSON.stringify(displayName)} } });`,
    ].join("\n"),
  );
  return { command: `${process.execPath} ${file}`, dir };
}

test("--against synth targets the Python cdk.out instead of the live template", () => {
  const cluster = "sample-cluster";
  const { root, fixturesRoot, liveDir, live } = topLevelContextFixture(cluster);
  const base = JSON.parse(readFileSync(live, "utf8")) as {
    Resources: { marker: { Properties?: Record<string, string> } };
  };
  const withName = (displayName: string) => {
    const template = structuredClone(base);
    template.Resources.marker.Properties = { DisplayName: displayName };
    return template;
  };
  mkdirSync(join(fixturesRoot, cluster, "python/_cdk/cdk.out.metrics"), { recursive: true });
  writeFileSync(
    join(fixturesRoot, cluster, "python/_cdk/cdk.out.metrics", `${cluster}-metrics.template.json`),
    JSON.stringify(withName("from-python")),
  );
  writeFileSync(live, JSON.stringify(withName("from-live")));
  const app = displayNameApp(cluster, "from-python");
  const run = (...flags: string[]) =>
    spawnSync(
      process.execPath,
      [
        SYNTH,
        "--cluster",
        cluster,
        "--stack",
        "metrics",
        "--app-override",
        app.command,
        "--fixtures",
        fixturesRoot,
        "--live-dir",
        liveDir,
        ...flags,
      ],
      { encoding: "utf8" },
    );
  try {
    const againstSynth = run("--against", "synth");
    strictEqual(againstSynth.status, 0, againstSynth.stdout + againstSynth.stderr);
    match(againstSynth.stdout, /^PARITY  1 live resources, 0 missing, 0 extra, 0 property diffs, 0 soft$/m);

    const againstLive = run();
    strictEqual(againstLive.status, 1, againstLive.stdout + againstLive.stderr);
    match(againstLive.stdout, /^DIFF     Resources\.marker\.Properties\.DisplayName$/m);
    match(againstLive.stdout, /^MISMATCH  1 live resources, 0 missing, 0 extra, 1 property diffs, 0 soft$/m);
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(app.dir, { recursive: true, force: true });
  }
});

test('a synth that differs from the live template exits 1', () => {
  const app = `${fakeApp()} --mutate`;
  const r = spawnSync(process.execPath, [SYNTH, '--cluster', 'idea-dev27', '--stack', 'metrics', '--app-override', app], {
    encoding: 'utf8',
  });
  strictEqual(r.status, 1, r.stdout + r.stderr);
  match(r.stdout, /^MISMATCH .*1 property diffs/m);
});

test('a cluster with no cdk.context.json exits 2 instead of synthesizing a dummy VPC', () => {
  const fixturesRoot = mkdtempSync(join(tmpdir(), "ideactl-no-context-"));
  try {
    const r = spawnSync(
      process.execPath,
      [SYNTH, "--cluster", "sample-cluster", "--stack", "metrics", "--fixtures", fixturesRoot],
      { encoding: "utf8" },
    );
    strictEqual(r.status, 2);
    match(r.stderr, /no cdk\.context\.json for sample-cluster/);
  } finally {
    rmSync(fixturesRoot, { force: true, recursive: true });
  }
});

type SyntheticFixture = Readonly<{
  root: string;
  fixturesRoot: string;
  liveDir: string;
  live: string;
}>;

/** Creates the smallest isolated replay fixture needed to reach the CDK app. */
function topLevelContextFixture(cluster: string): SyntheticFixture {
  const root = mkdtempSync(join(tmpdir(), "ideactl-synth-fixture-"));
  const fixturesRoot = join(root, "fixtures");
  const fixture = join(fixturesRoot, cluster);
  const liveDir = join(root, "live");
  const live = join(liveDir, `${cluster}-metrics.json`);
  mkdirSync(fixture, { recursive: true });
  mkdirSync(liveDir, { recursive: true });
  writeFileSync(join(fixture, "cdk.context.json"), "{}\n");
  writeFileSync(
    join(fixture, "cluster-settings.json"),
    JSON.stringify({ Items: [{ key: { S: "cluster.aws.region" }, value: { S: "us-east-2" } }] }),
  );
  writeFileSync(
    join(fixture, "modules.json"),
    JSON.stringify({ Items: [{ module_id: { S: "metrics" }, name: { S: "metrics" } }] }),
  );
  writeFileSync(join(fixture, "synth-reads.json"), "{}\n");
  writeFileSync(
    live,
    JSON.stringify({
      Parameters: {
        BootstrapVersion: {
          Type: "AWS::SSM::Parameter::Value<String>",
          Default: "/cdk-bootstrap/hnb659fds/version",
          Description: "Version of the CDK Bootstrap resources in this environment, automatically retrieved from SSM Parameter Store. [cdk:skip]",
        },
      },
      Rules: {
        CheckBootstrapVersion: {
          Assertions: [
            {
              Assert: { "Fn::Not": [{ "Fn::Contains": [["1", "2", "3", "4", "5"], { Ref: "BootstrapVersion" }] }] },
              AssertDescription: "CDK bootstrap stack version 6 required. Please run 'cdk bootstrap' with a recent version of the CDK CLI.",
            },
          ],
        },
      },
      Resources: {
        marker: {
          Type: "AWS::SNS::Topic",
          Metadata: { "aws:cdk:path": `${cluster}-metrics/marker` },
        },
      },
    }),
  );
  return { root, fixturesRoot, liveDir, live };
}

/**
 * Return a CDK app command that synthesizes one stable resource with the requested stack name.
 */
function singleResourceStackApp(cluster: string): { command: string; dir: string } {
  // Inside node_modules so aws-cdk-lib resolves and a crashed run leaves nothing git-visible.
  const dir = mkdtempSync(join(PKG, "node_modules/.w4-context-app-"));
  const file = join(dir, "app.mjs");
  writeFileSync(
    file,
    [
      'import { App, CfnResource, Stack } from "aws-cdk-lib";',
      "const app = new App();",
      `const stack = new Stack(app, ${JSON.stringify(`${cluster}-metrics`)}, { stackName: ${JSON.stringify(`${cluster}-metrics`)} });`,
      'new CfnResource(stack, "marker", { type: "AWS::SNS::Topic" });',
    ].join("\n"),
  );
  return { command: `${process.execPath} ${file}`, dir };
}

test("a top-level-only cdk.context.json is accepted", () => {
  const cluster = "idea-test1";
  const { root, fixturesRoot, liveDir } = topLevelContextFixture(cluster);
  const app = singleResourceStackApp(cluster);
  try {
    const r = spawnSync(
      process.execPath,
      [
        SYNTH,
        "--cluster",
        cluster,
        "--stack",
        "metrics",
        "--app-override",
        app.command,
        "--fixtures",
        fixturesRoot,
        "--live-dir",
        liveDir,
      ],
      { encoding: "utf8" },
    );
    strictEqual(r.status, 0, r.stdout + r.stderr);
    match(r.stdout, /^PARITY  1 live resources, 0 missing, 0 extra, 0 property diffs, 0 soft$/m);
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(app.dir, { recursive: true, force: true });
  }
});

test('synth refuses a fixture set that is missing a replay file', () => {
  // Built here rather than picked from tools/parity/fixtures, so the test cannot rot as
  // fixtures are completed.
  const dir = mkdtempSync(join(tmpdir(), 'synth-incomplete-'));
  const cluster = 'idea-test1';
  mkdirSync(join(dir, cluster), { recursive: true });
  writeFileSync(join(dir, cluster, 'cdk.context.json'), '{}');
  // Complete enough to reach the replay-fixture check: only cluster-settings.json is missing.
  const refDir = join(dir, cluster, 'python/_cdk/cdk.out.metrics');
  mkdirSync(refDir, { recursive: true });
  writeFileSync(join(refDir, `${cluster}-metrics.template.json`), '{"Resources":{}}');
  const result = spawnSync(
    process.execPath,
    [SYNTH, '--cluster', cluster, '--stack', 'metrics', '--against', 'synth', '--fixtures', dir],
    { encoding: 'utf8' },
  );
  rmSync(dir, { recursive: true, force: true });
  strictEqual(result.status, 2, `${result.stdout}${result.stderr}`);
  match(result.stderr, /missing replay fixtures:/);
});

test('bad usage exits 2', () => {
  const r = spawnSync(process.execPath, [SYNTH, '--cluster', 'idea-dev27'], { encoding: 'utf8' });
  strictEqual(r.status, 2);
  match(r.stderr, /usage: synth\.ts --cluster C --stack MODULE_ID/);
});

/** A CDK app that writes the two context values it was given into the one resource it emits. */
function contextEchoApp(cluster: string): { command: string; dir: string } {
  const dir = mkdtempSync(join(PKG, 'node_modules/.w4-context-echo-'));
  const file = join(dir, 'app.mjs');
  writeFileSync(
    file,
    [
      'import { App, CfnResource, Stack } from "aws-cdk-lib";',
      'const app = new App();',
      `const stack = new Stack(app, ${JSON.stringify(`${cluster}-metrics`)}, { stackName: ${JSON.stringify(`${cluster}-metrics`)} });`,
      'const echo = `${app.node.tryGetContext("first_package_uri")}|${app.node.tryGetContext("second_package_uri")}`;',
      'new CfnResource(stack, "marker", { type: "AWS::SNS::Topic", properties: { DisplayName: echo } });',
    ].join('\n'),
  );
  return { command: `${process.execPath} ${file}`, dir };
}

test('--context is repeatable and reaches the app', () => {
  const cluster = "sample-cluster";
  const { root, fixturesRoot, liveDir, live } = topLevelContextFixture(cluster);
  const app = contextEchoApp(cluster);
  const first = 's3://sample-bucket/idea/bootstrap/bootstrap-one.tar.gz';
  const second = 's3://sample-bucket/idea/bootstrap/bootstrap-two.tar.gz';
  try {
    // The deployed template carries the two locations the deploy passed as context.
    const deployed = JSON.parse(readFileSync(live, 'utf8')) as {
      Resources: { marker: { Properties?: Record<string, string> } };
    };
    deployed.Resources.marker.Properties = { DisplayName: `${first}|${second}` };
    writeFileSync(live, JSON.stringify(deployed));

    const run = (...flags: string[]) =>
      spawnSync(
        process.execPath,
        [
          SYNTH,
          "--cluster",
          cluster,
          "--stack",
          "metrics",
          "--app-override",
          app.command,
          "--fixtures",
          fixturesRoot,
          "--live-dir",
          liveDir,
          ...flags,
        ],
        { encoding: "utf8" },
      );

    const without = run();
    strictEqual(without.status, 1, without.stdout + without.stderr);
    match(without.stdout, /^DIFF     Resources\.marker\.Properties\.DisplayName$/m);

    const supplied = run('--context', `first_package_uri=${first}`, '--context', `second_package_uri=${second}`);
    strictEqual(supplied.status, 0, supplied.stdout + supplied.stderr);
    match(supplied.stdout, /^PARITY  1 live resources, 0 missing, 0 extra, 0 property diffs, 0 soft$/m);

    const malformed = run('--context', 'no-equals-sign');
    strictEqual(malformed.status, 2, malformed.stdout + malformed.stderr);
    match(malformed.stderr, /--context takes key=value/);
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(app.dir, { recursive: true, force: true });
  }
});
