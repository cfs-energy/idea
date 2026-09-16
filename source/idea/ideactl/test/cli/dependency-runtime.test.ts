import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const cli = require.resolve('aws-cdk/bin/cdk');
const cdk = require.resolve('aws-cdk-lib');
const appSource = new URL('../../src/cdk/app.ts', import.meta.url).href;

test('installed CDK CLI synthesizes offline and rejects a real policy violation', () => {
  const root = mkdtempSync(join(tmpdir(), 'ideactl-dependency-runtime-'));
  try {
    const app = join(root, 'app.mjs');
    const output = join(root, 'assembly');
    const synth = () => spawnSync(process.execPath, [cli, 'synth', '--app',
      'node app.mjs', '--output', output, '--no-lookups', '--notices', 'false'], {
      cwd: root, encoding: 'utf8',
      env: { PATH: process.env.PATH, HOME: root, CDK_DISABLE_CLI_TELEMETRY: 'true', AWS_EC2_METADATA_DISABLED: 'true' },
    });
    const imports = `import cdk from ${JSON.stringify(cdk)};\nconst { App, Stack, CfnResource, Validations } = cdk;\n`;
    writeFileSync(app, imports + 'const app = new App(); const stack = new Stack(app, "DependencySmoke");\n' +
      'new CfnResource(stack, "Topic", { type: "AWS::SNS::Topic" }); app.synth();\n');
    const good = synth();
    assert.equal(good.status, 0, good.stdout + good.stderr);
    const template = JSON.parse(readFileSync(join(output, 'DependencySmoke.template.json'), 'utf8'));
    assert.equal(template.Resources.Topic.Type, 'AWS::SNS::Topic');

    // This must fail through the installed CLI, including the plugin registration used by the app.
    writeFileSync(app, imports + `import { IdeaSolutionsChecks } from ${JSON.stringify(appSource)};\n` +
      'const app = new App(); const stack = new Stack(app, "DependencySmoke");\n' +
      'Validations.of(app).addPlugins(new IdeaSolutionsChecks(app));\n' +
      'new cdk.aws_s3.CfnBucket(stack, "UnprotectedBucket"); app.synth();\n');
    const bad = synth();
    assert.notEqual(bad.status, 0, bad.stdout + bad.stderr);
    assert.match(bad.stdout + bad.stderr, /AwsSolutions-S1/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
