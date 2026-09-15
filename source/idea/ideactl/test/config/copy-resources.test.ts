/**
 * The build resource copy contains only directories that runtime code reads.
 */

import assert from "node:assert/strict";
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const packageRoot = dirname(dirname(dirname(fileURLToPath(import.meta.url))));
const copyScript = join(packageRoot, "scripts", "copy-resources.mjs");
const builtMain = join(packageRoot, "dist", "src", "cli", "main.js");
const expectedDirectories = [
  "bootstrap",
  "cdk",
  "config",
  "input_params",
  "integration_tests",
  "policies",
];

/** Reads the immediate directories in a copied resource tree. */
function copiedDirectories(resources: string): string[] {
  return readdirSync(resources, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
}

/** Runs compiled code from the isolated packaged tree. */
function runBuilt(stage: string, args: string[], environment: NodeJS.ProcessEnv): string {
  const result = spawnSync(process.execPath, args, {
    cwd: stage,
    env: environment,
    encoding: "utf8",
  });
  assert.equal(
    result.status,
    0,
    [`built command failed`, result.stdout, result.stderr, result.error?.message ?? ""].join("\n"),
  );
  return result.stdout;
}

test("resource build copy includes every runtime directory and nothing else", () => {
  const root = mkdtempSync(join(tmpdir(), "ideactl-f-resources-"));
  const resources = join(root, "dist", "resources");

  try {
    const result = spawnSync(process.execPath, [copyScript], {
      cwd: packageRoot,
      env: {
        ...process.env,
        IDEACTL_RESOURCE_OUTPUT_DIR: resources,
      },
      encoding: "utf8",
    });

    assert.equal(
      result.status,
      0,
      [`resource copy failed`, result.stdout, result.stderr, result.error?.message ?? ""].join("\n"),
    );
    assert.deepEqual(copiedDirectories(resources), expectedDirectories);
    assert.equal(existsSync(join(resources, "installer_policies")), false);
    const repositoryRoot = dirname(dirname(dirname(packageRoot)));
    assert.equal(
      readFileSync(join(root, "dist", "IDEA_VERSION.txt"), "utf8"),
      readFileSync(join(repositoryRoot, "IDEA_VERSION.txt"), "utf8"),
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("built commands load copied resources without the administrator source tree", () => {
  assert.equal(existsSync(builtMain), true, "run npm run build before this test");

  const root = mkdtempSync(join(tmpdir(), "ideactl-f-resources-runtime-"));
  const stage = join(root, "package");
  const home = join(root, "home");
  const compiled = join(stage, "dist");
  const environment: NodeJS.ProcessEnv = {
    ...process.env,
    HOME: home,
    IDEA_USER_HOME: join(home, ".idea"),
  };

  try {
    // Stage only the runtime tree. Release archives and compiled tests are not
    // inputs to this check and can make an isolated copy hundreds of times larger.
    mkdirSync(compiled, { recursive: true });
    cpSync(join(packageRoot, "dist", "src"), join(compiled, "src"), { recursive: true });
    cpSync(join(packageRoot, "dist", "resources"), join(compiled, "resources"), { recursive: true });
    copyFileSync(join(packageRoot, "dist", "IDEA_VERSION.txt"), join(compiled, "IDEA_VERSION.txt"));
    symlinkSync(join(packageRoot, "node_modules"), join(stage, "node_modules"), "dir");

    const mainModule = pathToFileURL(join(compiled, "src", "cli", "main.js")).href;
    const help = runBuilt(stage, [
      "--input-type=module",
      "--eval",
      `import { run } from ${JSON.stringify(mainModule)}; process.exitCode = await run(["quick-setup-help"]);`,
    ], environment);
    const stagedValues = readFileSync(join(compiled, "resources", "config", "values.yml"), "utf8");
    assert.equal(help.trimEnd(), stagedValues.trimEnd());

    const imports = {
      assets: pathToFileURL(join(compiled, "src", "cdk", "code-asset.js")).href,
      bootstrap: pathToFileURL(join(compiled, "src", "cli", "commands", "deploy.js")).href,
      config: pathToFileURL(join(compiled, "src", "cli", "commands", "config.js")).href,
      configModel: pathToFileURL(join(compiled, "src", "config", "cluster-config.js")).href,
      installer: pathToFileURL(join(compiled, "src", "cli", "prompts.js")).href,
      policy: pathToFileURL(join(compiled, "src", "cdk", "policy.js")).href,
    };
    const valuesDirectory = join(root, "generated-config");
    mkdirSync(valuesDirectory, { recursive: true });
    const probe = [
      `import { IdeaCodeAsset } from ${JSON.stringify(imports.assets)};`,
      `import { runBootstrap } from ${JSON.stringify(imports.bootstrap)};`,
      `import { configGenerate } from ${JSON.stringify(imports.config)};`,
      `import { ClusterConfig } from ${JSON.stringify(imports.configModel)};`,
      `import { ScriptedPromptDriver } from ${JSON.stringify(imports.installer)};`,
      `import { renderPolicy } from ${JSON.stringify(imports.policy)};`,
      "const entries = [",
      '  { key: "cluster.cluster_s3_bucket", value: "sample-bucket" },',
      '  { key: "cluster.aws.dns_suffix", value: "amazonaws.com" },',
      '  { key: "cluster.cluster_name", value: "sample-cluster" },',
      '  { key: "cluster.aws.region", value: "us-east-2" },',
      '  { key: "cluster.aws.partition", value: "aws" },',
      '  { key: "cluster.aws.account_id", value: "sample-account" },',
      "];",
      "const typedEntries = entries.map(({ key, value }) => ({ key: { S: key }, value: { S: value } }));",
      "const deps = {",
      "  scan: async (input) => input.TableName.endsWith('.cluster-settings') ? { Items: entries } : { Items: [] },",
      "  spawn: async () => 0,",
      "  out: () => {},",
      "  err: () => {},",
      "  prompt: async () => true,",
      '  uuid: () => "00000000-0000-0000-0000-000000000000",',
      "};",
      "await configGenerate(deps, {",
      `  configDir: ${JSON.stringify(valuesDirectory)},`,
      "  force: true,",
      "  installerDriver: new ScriptedPromptDriver({",
      '    aws_profile: "default", aws_partition: "aws", aws_region: "us-east-2",',
      '    cluster_name: "sample", administrator_email: "admin@example.invalid",',
      '    vpc_cidr_block: "192.0.2.0/24", ssh_key_pair_name: "sample-key",',
      '    cluster_access: "client-ip", client_ip: "192.0.2.10", alb_public: true,',
      '    use_vpc_endpoints: false, directory_service_provider: "aws_managed_activedirectory",',
      '    enable_aws_backup: false, kms_key_type: "aws-managed",',
      '    enabled_modules: [["metrics", "scheduler", "virtual-desktop-controller"]],',
      '    metrics_provider: "cloudwatch", base_os: "amazonlinux2023", instance_type: "m7i.large", volume_size: 200,',
      "  }),",
      '  installerIdentity: async () => ({ accountId: "sample-account", partition: "aws", dnsSuffix: "amazonaws.com" }),',
      "});",
      "const config = ClusterConfig.fromFile(JSON.stringify({ Items: typedEntries }), JSON.stringify({ Items: [] }));",
      'const policy = renderPolicy("amazon-ssm-managed-instance-core.yml", { config });',
      'const asset = new IdeaCodeAsset("idea_solution_metrics").assetPath();',
      "await runBootstrap(deps, {",
      '  clusterName: "sample-cluster", awsRegion: "us-east-2", moduleSet: "default",',
      "});",
      "const statements = Array.isArray(policy.Statement) ? policy.Statement : [];",
      "const actions = statements.flatMap((statement) => {",
      "  const action = statement && typeof statement === 'object' ? statement.Action : undefined;",
      "  return Array.isArray(action) ? action : [];",
      "});",
      "console.log(`RESOURCE_PROBE:${JSON.stringify({ policyVersion: policy.Version, actions, asset })}`);",
    ].join("\n");
    const probeOutput = runBuilt(
      stage,
      ["--input-type=module", "--eval", probe],
      environment,
    );

    const probeLine = probeOutput
      .split("\n")
      .find((line) => line.startsWith("RESOURCE_PROBE:"));
    assert.ok(probeLine !== undefined, "probe did not print a resource report");
    const report = JSON.parse(probeLine.slice("RESOURCE_PROBE:".length)) as {
      policyVersion?: string;
      actions?: string[];
      asset?: string;
    };
    assert.equal(report.policyVersion, "2012-10-17");
    assert.ok(
      Array.isArray(report.actions) && report.actions.includes("ssm:UpdateInstanceInformation"),
      "rendered policy is missing ssm:UpdateInstanceInformation",
    );
    assert.ok(
      Array.isArray(report.actions) && report.actions.includes("ssmmessages:CreateControlChannel"),
      "rendered policy is missing ssmmessages:CreateControlChannel",
    );
    assert.equal(typeof report.asset, "string");
    assert.match(report.asset ?? "", /idea_solution_metrics/);
    assert.equal(existsSync(report.asset ?? ""), true);
    assert.equal(
      existsSync(join(compiled, "resources", "integration_tests", "job_test_cases.yml")),
      true,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
