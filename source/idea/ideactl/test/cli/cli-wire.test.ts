import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { CdkInvoker } from "../../src/cli/cdk-invoker.ts";
import { buildProgram } from "../../src/cli/main.ts";
import { fakeDeps, moduleRow } from "../support/deploy-harness.ts";
import { ideaVersion } from "../../src/version.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";

function commandNames(): string[] {
  return buildProgram(fakeDeps()).commands.map((command) => command.name());
}

for (const name of [
  "upgrade-cluster",
  "delete-cluster",
  "delete-backups",
  "sso",
  "directoryservice",
  "shared-storage",
  "utils",
  "support",
  "run-integration-tests",
  "backup-update-global-settings",
]) {
  test(`${name} is registered on the command program`, () => {
    assert.ok(commandNames().includes(name));
  });
}

test("a host deploy uploads its rendered bootstrap package before synthesis", async () => {
  const home = mkdtempSync(join(tmpdir(), "ideactl-cli-wire-"));
  const source = join(home, "bootstrap");
  const previousHome = process.env.IDEA_USER_HOME;
  const previousCdkBin = process.env.IDEA_CDK_BIN;
  process.env.IDEA_USER_HOME = home;
  process.env.IDEA_CDK_BIN = "/bin/cdk";

  try {
    mkdirSync(join(source, "common"), { recursive: true });
    mkdirSync(join(source, "cluster-manager"), { recursive: true });
    writeFileSync(join(source, "cluster-manager", "setup.sh.jinja2"), "MODULE={{ context.moduleName }}\n");
    const downloads = join(home, "downloads");
    mkdirSync(downloads, { recursive: true });
    writeFileSync(join(downloads, `idea-cluster-manager-${ideaVersion()}.tar.gz`), "release");

    const deps = fakeDeps({
      tables: {
        [`${CLUSTER}.modules`]: [moduleRow("cm", "cluster-manager", "app")],
        [`${CLUSTER}.cluster-settings`]: [
          { key: "cluster.cluster_s3_bucket", value: "sample-bucket" },
          { key: "cm.ec2.autoscaling.base_os", value: "amazonlinux2023" },
          { key: "cm.ec2.autoscaling.instance_type", value: "m7i.large" },
        ],
      },
      bootstrapContext: (input) => ({ moduleName: input.moduleName }),
    });
    deps.bootstrapSourceDir = source;

    const timeline: Array<{ kind: "put" | "spawn"; detail: string }> = [];
    const putObject = deps.s3.putObject.bind(deps.s3);
    deps.s3.putObject = async (input) => {
      timeline.push({ kind: "put", detail: `${input.Bucket}/${input.Key}` });
      return putObject(input);
    };
    const spawnFn = deps.spawn.bind(deps);
    deps.spawn = async (argv, options) => {
      timeline.push({ kind: "spawn", detail: argv.join(" ") });
      return spawnFn(argv, options);
    };

    const invoker = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: "cm",
      moduleSet: "default",
      deps,
    });
    await invoker.invoke({ forceBuildBootstrap: true });

    const releaseKey = `sample-bucket/idea/releases/idea-cluster-manager-${ideaVersion()}.tar.gz`;
    // The archive is named by the rendered tree's content id, in the deployment id's shape.
    const bootstrapKey = [...deps.puts.keys()].find((key) => /^sample-bucket\/idea\/bootstrap\/bootstrap-cm-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.tar\.gz$/.test(key));
    assert.ok(deps.puts.has(releaseKey));
    assert.ok(bootstrapKey !== undefined, "the bootstrap archive was not uploaded under a content-named key");
    assert.notEqual(bootstrapKey, "sample-bucket/idea/bootstrap/bootstrap-cm-00000000-0000-4000-8000-000000000000.tar.gz");
    const spawn = deps.spawns[0] ?? [];
    assert.ok(spawn.includes("-c"));
    const uriArgument = spawn.find((argument) => argument.includes("bootstrap_package_uri="));
    assert.equal(uriArgument, `bootstrap_package_uri=s3://${bootstrapKey}`);
    const releasePutAt = timeline.findIndex((event) => event.kind === "put" && event.detail === releaseKey);
    const bootstrapPutAt = timeline.findIndex((event) => event.kind === "put" && event.detail === bootstrapKey);
    const spawnAt = timeline.findIndex((event) => event.kind === "spawn");
    assert.notEqual(releasePutAt, -1, `release put missing from ${JSON.stringify(timeline)}`);
    assert.notEqual(bootstrapPutAt, -1, `bootstrap put missing from ${JSON.stringify(timeline)}`);
    assert.notEqual(spawnAt, -1, `spawn missing from ${JSON.stringify(timeline)}`);
    assert.ok(
      releasePutAt < spawnAt,
      `release package put at ${releasePutAt} must precede synthesis spawn at ${spawnAt}: ${JSON.stringify(timeline)}`,
    );
    assert.ok(
      bootstrapPutAt < spawnAt,
      `bootstrap package put at ${bootstrapPutAt} must precede synthesis spawn at ${spawnAt}: ${JSON.stringify(timeline)}`,
    );
  } finally {
    if (previousHome === undefined) delete process.env.IDEA_USER_HOME;
    else process.env.IDEA_USER_HOME = previousHome;
    if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
    else process.env.IDEA_CDK_BIN = previousCdkBin;
    rmSync(home, { recursive: true, force: true });
  }
});
