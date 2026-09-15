import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import type { PutObjectCommand } from "@aws-sdk/client-s3";

import {
  uploadBootstrapPackage,
  uploadReleasePackage,
  type BootstrapPackageUploadClient,
} from "../../src/cli/bootstrap-package.ts";
import type {
  ConfigWriter,
  ConfigWriterOptions,
  Deps,
  PromptChoice,
} from "../../src/cli/cdk-invoker.ts";
import { run } from "../../src/cli/main.ts";
import { deleteBackupsCommand, deleteCluster, type DeleteClusterDeps } from "../../src/cli/commands/delete-cluster.ts";
import { configUpdate } from "../../src/cli/commands/config.ts";
import { runDeploy } from "../../src/cli/commands/deploy.ts";
import { ClusterConfig, type ModuleInfo, type ScanPage } from "../../src/config/cluster-config.ts";
import type { ConfigEntry, ModuleSpec } from "../../src/config/cluster-config-db.ts";
import {
  UpgradeStateConflictError,
  UpgradeStateJournal,
  type UpgradePlan,
  type UpgradeStateObjectApi,
  type UpgradeStateWriteCondition,
  type VersionedUpgradeStateObject,
} from "../../src/config/upgrade-state.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";
const MODULE_SET = "default";

interface StoredSetting {
  value: unknown;
  version: number;
}

/**
 * In-memory command dependencies that preserve the write semantics used by the
 * configuration and deployment paths.
 */
class MemoryCommandDeps implements Deps {
  readonly settings = new Map<string, StoredSetting>();
  readonly modules = new Map<string, ModuleInfo>();
  readonly objects = new Map<string, Uint8Array | string>();
  readonly output: string[] = [];
  readonly errors: string[] = [];
  readonly spawned: string[][] = [];
  readonly executed: string[] = [];
  readonly sleeps: number[] = [];
  settingMutations = 0;
  moduleMutations = 0;

  async spawn(argv: string[]): Promise<number> {
    this.spawned.push([...argv]);
    return 0;
  }

  readonly cfn = {
    describeChangeSet: async () => ({
      Status: "CREATE_COMPLETE",
      Changes: [{
        ResourceChange: {
          Action: "Modify",
          LogicalResourceId: "ClusterResource",
          ResourceType: "AWS::S3::Bucket",
          Replacement: "False",
        },
      }],
    }),
    executeChangeSet: async (input: { StackName: string; ChangeSetName: string }) => {
      this.executed.push(`${input.StackName}/${input.ChangeSetName}`);
      const moduleId = input.StackName.slice(`${CLUSTER}-`.length);
      const module = this.modules.get(moduleId);
      if (module !== undefined) {
        this.modules.set(moduleId, {
          ...module,
          status: "deployed",
          stack_name: input.StackName,
          version: "26.09.0",
        });
      }
    },
    describeStack: async () => ({ StackStatus: "UPDATE_COMPLETE", Outputs: [] }),
  };

  readonly s3 = {
    putObject: async (input: { Bucket: string; Key: string; Body: Uint8Array | string }) => {
      this.objects.set(`${input.Bucket}/${input.Key}`, input.Body);
    },
    getObject: async (input: { Bucket: string; Key: string }) => {
      const body = this.objects.get(`${input.Bucket}/${input.Key}`);
      if (body === undefined) throw new Error("NoSuchKey");
      return typeof body === "string" ? body : Buffer.from(body).toString("utf8");
    },
  };

  readonly scan = async (input: { TableName: string }): Promise<ScanPage> => {
    if (input.TableName === `${CLUSTER}.modules`) {
      return { Items: [...this.modules.values()].map((module) => ({ ...module })) };
    }
    if (input.TableName === `${CLUSTER}.cluster-settings`) {
      return {
        Items: [...this.settings.entries()].map(([key, entry]) => ({
          key,
          value: entry.value,
          version: entry.version,
        })),
      };
    }
    return { Items: [] };
  };

  async configWriter(_options: ConfigWriterOptions): Promise<ConfigWriter> {
    return {
      syncModulesInDb: async (modules) => this.syncModules(modules),
      syncClusterSettingsInDb: async (entries, overwrite) => this.syncSettings(entries, overwrite === true),
      setConfigEntry: async (key, value) => this.setSetting(key, value),
      deleteConfigEntries: async (prefix) => this.deleteSettings(prefix),
    };
  }

  async accountId(): Promise<string> {
    return "123456789012";
  }

  async httpStatus(_url: string): Promise<number> {
    return 200;
  }

  async sleep(ms: number): Promise<void> {
    this.sleeps.push(ms);
  }

  now(): number {
    return Date.UTC(2026, 8, 10, 12, 0, 0);
  }

  uuid(): string {
    return "00000000-0000-4000-8000-000000000001";
  }

  out(line: string): void {
    this.output.push(line);
  }

  err(line: string): void {
    this.errors.push(line);
  }

  async prompt(_choice: PromptChoice): Promise<string | boolean> {
    return true;
  }

  private async syncModules(modules: ModuleSpec[]): Promise<void> {
    for (const module of modules) {
      if (this.modules.has(module.id)) continue;
      this.modules.set(module.id, {
        module_id: module.id,
        name: module.name,
        type: module.type,
        status: module.type === "config" ? "deployed" : "not-deployed",
        stack_name: null,
        version: null,
      });
      this.moduleMutations += 1;
    }
  }

  private async syncSettings(entries: ConfigEntry[], overwrite: boolean): Promise<void> {
    for (const entry of entries) {
      if (!overwrite && this.settings.has(entry.key)) continue;
      this.setSetting(entry.key, entry.value);
    }
  }

  private async setSetting(key: string, value: unknown): Promise<void> {
    const previous = this.settings.get(key);
    this.settings.set(key, { value: value ?? null, version: (previous?.version ?? 0) + 1 });
    this.settingMutations += 1;
  }

  private async deleteSettings(prefix: string): Promise<void> {
    for (const key of [...this.settings.keys()]) {
      if (!key.startsWith(prefix)) continue;
      this.settings.delete(key);
      this.settingMutations += 1;
    }
  }
}

const ideaHome = mkdtempSync(join(tmpdir(), "ideactl-idempotency-"));
const previousIdeaHome = process.env.IDEA_USER_HOME;
const previousCdkBin = process.env.IDEA_CDK_BIN;

before(() => {
  process.env.IDEA_USER_HOME = ideaHome;
  process.env.IDEA_CDK_BIN = "/opt/idea/bin/cdk";
});

after(() => {
  if (previousIdeaHome === undefined) delete process.env.IDEA_USER_HOME;
  else process.env.IDEA_USER_HOME = previousIdeaHome;
  if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
  else process.env.IDEA_CDK_BIN = previousCdkBin;
  rmSync(ideaHome, { recursive: true, force: true });
});

/** Write the smallest real configuration tree accepted by `config update`. */
function writeConfigTree(): string {
  const root = mkdtempSync(join(tmpdir(), "ideactl-idempotency-config-"));
  const configDir = join(root, "config");
  mkdirSync(join(configDir, "global-settings"), { recursive: true });
  mkdirSync(join(configDir, "cluster"), { recursive: true });
  writeFileSync(
    join(configDir, "idea.yml"),
    [
      "modules:",
      "  - name: global-settings",
      "    id: global-settings",
      "    type: config",
      "    config_files: [settings.yml]",
      "  - name: cluster",
      "    id: cluster",
      "    type: stack",
      "    config_files: [settings.yml]",
      "",
    ].join("\n"),
  );
  writeFileSync(
    join(configDir, "global-settings", "settings.yml"),
    [
      "module_sets:",
      "  default:",
      "    cluster:",
      "      module_id: cluster",
      "locale: en_US.UTF-8",
      "",
    ].join("\n"),
  );
  writeFileSync(
    join(configDir, "cluster", "settings.yml"),
    [
      `cluster_name: ${CLUSTER}`,
      "aws:",
      `  region: ${REGION}`,
      "dynamodb:",
      "  kms_key_id: ~",
      "",
    ].join("\n"),
  );
  return root;
}

/** Return a stable value and version view of the in-memory settings table. */
function settingsSnapshot(deps: MemoryCommandDeps): Array<[string, StoredSetting]> {
  return [...deps.settings.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, entry]) => [key, structuredClone(entry)]);
}

/** Pins keys the config tree must write, so an empty update cannot pass. */
function assertConfigTreeWritten(deps: MemoryCommandDeps): void {
  assert.equal(deps.settings.get("cluster.cluster_name")?.value, CLUSTER);
  assert.equal(deps.settings.get("cluster.aws.region")?.value, REGION);
  assert.equal(deps.settings.get("global-settings.locale")?.value, "en_US.UTF-8");
  assert.equal(
    deps.settings.get("global-settings.module_sets.default.cluster.module_id")?.value,
    "cluster",
  );
  assert.equal(deps.modules.get("cluster")?.type, "stack");
  assert.equal(deps.modules.get("global-settings")?.type, "config");
  assert.ok(deps.settingMutations > 0);
}

test("deploy without upgrade performs no second deployment after the first run marks the module deployed", async () => {
  const deps = new MemoryCommandDeps();
  deps.modules.set("cluster", {
    module_id: "cluster",
    name: "cluster",
    type: "stack",
    status: "not-deployed",
  });
  deps.settings.set("global-settings.module_sets.default.cluster.module_id", { value: "cluster", version: 1 });

  const options = {
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: MODULE_SET,
    deploymentId: "00000000-0000-4000-8000-000000000001",
  };
  await runDeploy(deps, ["cluster"], options);
  await runDeploy(deps, ["cluster"], options);

  assert.equal(deps.spawned.length, 1);
  assert.equal(deps.executed.length, 1);
  assert.equal(deps.modules.get("cluster")?.status, "deployed");
});

test("config update without overwrite makes the second run a strict no-op", async () => {
  const root = writeConfigTree();
  const deps = new MemoryCommandDeps();
  try {
    const options = {
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: MODULE_SET,
      configDir: root,
      force: true,
    };
    await configUpdate(deps, options);
    assertConfigTreeWritten(deps);
    const first = settingsSnapshot(deps);
    const settingMutations = deps.settingMutations;
    const moduleMutations = deps.moduleMutations;

    await configUpdate(deps, options);

    assert.deepEqual(settingsSnapshot(deps), first);
    assert.equal(deps.settingMutations, settingMutations);
    assert.equal(deps.moduleMutations, moduleMutations);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("config update with overwrite converges values but repeats writes and version increments", async () => {
  const root = writeConfigTree();
  const deps = new MemoryCommandDeps();
  try {
    const options = {
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: MODULE_SET,
      configDir: root,
      force: true,
      overwrite: true,
    };
    await configUpdate(deps, options);
    assertConfigTreeWritten(deps);
    const firstValues = settingsSnapshot(deps).map(([key, entry]) => [key, entry.value]);
    const firstMutations = deps.settingMutations;

    await configUpdate(deps, options);

    assert.deepEqual(
      settingsSnapshot(deps).map(([key, entry]) => [key, entry.value]),
      firstValues,
    );
    assert.equal(deps.settingMutations, firstMutations * 2);
    assert.ok([...deps.settings.values()].every((entry) => entry.version === 2));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("config set reaches the same value on repeat while preserving the write-counter quirk", async () => {
  const deps = new MemoryCommandDeps();
  const argv = [
    "config",
    "set",
    "Key=global-settings.retry_limit,Type=int,Value=4",
    "--force",
    "--cluster-name",
    CLUSTER,
    "--aws-region",
    REGION,
  ];

  assert.equal(await run(argv, deps), 0);
  assert.equal(await run(argv, deps), 0);

  assert.deepEqual(deps.settings.get("global-settings.retry_limit"), { value: 4, version: 2 });
});

test("config delete removes a prefix once and the second run changes nothing", async () => {
  const deps = new MemoryCommandDeps();
  deps.settings.set("global-settings.a", { value: 1, version: 1 });
  deps.settings.set("cluster.a", { value: 2, version: 1 });
  const argv = [
    "config",
    "delete",
    "global-settings.",
    "--cluster-name",
    CLUSTER,
    "--aws-region",
    REGION,
  ];

  assert.equal(await run(argv, deps), 0);
  const mutations = deps.settingMutations;
  assert.equal(await run(argv, deps), 0);

  assert.equal(deps.settings.has("global-settings.a"), false);
  assert.equal(deps.settings.has("cluster.a"), true);
  assert.equal(deps.settingMutations, mutations);
});

/** In-memory current-object view for unconditional package uploads. */
class MemoryPackageClient implements BootstrapPackageUploadClient {
  readonly objects = new Map<string, Buffer>();
  puts = 0;

  async send(command: PutObjectCommand): Promise<unknown> {
    const { Bucket: bucket, Key: key, Body: body } = command.input;
    if (bucket === undefined || key === undefined || !(body instanceof Uint8Array)) {
      throw new Error("invalid package upload");
    }
    this.objects.set(`${bucket}/${key}`, Buffer.from(body));
    this.puts += 1;
    return {};
  }
}

test("bootstrap and release package uploads converge on the same current object when repeated", async () => {
  const directory = mkdtempSync(join(tmpdir(), "ideactl-idempotency-packages-"));
  const bootstrapFile = join(directory, "bootstrap-cluster-deployment.tar.gz");
  const releaseName = "idea-cluster-manager-26.09.0.tar.gz";
  writeFileSync(bootstrapFile, "bootstrap-bytes");
  writeFileSync(join(directory, releaseName), "release-bytes");
  const client = new MemoryPackageClient();
  try {
    for (let runNumber = 0; runNumber < 2; runNumber += 1) {
      await uploadBootstrapPackage({
        client,
        clusterS3Bucket: "sample-bucket",
        archiveFile: bootstrapFile,
      });
      await uploadReleasePackage({
        client,
        clusterS3Bucket: "sample-bucket",
        packageDistDir: directory,
        packageName: releaseName,
      });
    }

    assert.deepEqual(
      client.objects.get("sample-bucket/idea/bootstrap/bootstrap-cluster-deployment.tar.gz"),
      readFileSync(bootstrapFile),
    );
    assert.deepEqual(
      client.objects.get(`sample-bucket/idea/releases/${releaseName}`),
      readFileSync(join(directory, releaseName)),
    );
    assert.equal(client.objects.size, 2);
    assert.equal(client.puts, 4);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

interface DeleteState {
  instances: Set<string>;
  stacks: Set<string>;
  destructiveCalls: number;
}

/** Build a stateful deletion fake whose discovery reflects prior deletions. */
function deletionDeps(state: DeleteState): DeleteClusterDeps {
  const modules: ModuleInfo[] = [
    {
      module_id: "cluster",
      name: "cluster",
      type: "stack",
      status: "deployed",
      stack_name: `${CLUSTER}-cluster`,
    },
    {
      module_id: "metrics",
      name: "metrics",
      type: "stack",
      status: "deployed",
      stack_name: `${CLUSTER}-metrics`,
    },
  ];
  return {
    loadConfig: async () => new ClusterConfig([], modules),
    findInstances: async () => [...state.instances].map((instanceId) => ({ instanceId, state: "running" })),
    instanceTerminationProtection: async () => false,
    disableInstanceTerminationProtection: async () => {},
    terminateInstance: async ({ instanceId }) => {
      if (state.instances.delete(instanceId)) state.destructiveCalls += 1;
    },
    getTaggedStacks: async () => ({ stacks: [...state.stacks] }),
    describeStack: async (stackName) => {
      if (!state.stacks.has(stackName)) throw new Error("ValidationError: stack not found");
      return { stackName, stackStatus: "CREATE_COMPLETE", terminationProtection: false };
    },
    disableStackTerminationProtection: async () => {},
    deleteStack: async (stackName) => {
      if (state.stacks.delete(stackName)) state.destructiveCalls += 1;
    },
    findAppInstance: async () => undefined,
    sendAppCleanup: async () => "cleanup",
    appCleanupStatus: async () => [],
    findBedrockProjects: async () => [],
    deleteBedrockProjectResources: async () => {},
    listUserPools: async () => ({ pools: [] }),
    describeUserPool: async () => ({}),
    disableUserPoolDeletionProtection: async () => {},
    describeLambdaNetworkInterfaces: async () => [],
    deleteNetworkInterface: async () => {},
    describeBackupVault: async () => {},
    listRecoveryPoints: async () => [],
    deleteRecoveryPoint: async () => {},
    listTables: async () => ({ tableNames: [] }),
    deleteTable: async () => {},
    listDynamoDbAlarms: async () => [],
    deleteAlarms: async () => {},
    listLogGroups: async () => [],
    deleteLogGroup: async () => {},
    accountId: async () => "123456789012",
    bucketExists: async () => false,
    deleteAllBucketObjectVersions: async () => {},
    deleteBucket: async () => {},
    prompt: async () => true,
    sleep: async () => {},
    out: () => {},
    err: () => {},
  };
}

test("delete-cluster converges when discovery no longer returns removed resources", async () => {
  const state: DeleteState = {
    instances: new Set(["instance-1"]),
    stacks: new Set([`${CLUSTER}-metrics`, `${CLUSTER}-cluster`]),
    destructiveCalls: 0,
  };
  const deps = deletionDeps(state);
  const options = { clusterName: CLUSTER, awsRegion: REGION, force: true };

  await deleteCluster(deps, options);
  const firstCalls = state.destructiveCalls;
  await deleteCluster(deps, options);

  assert.equal(state.instances.size, 0);
  assert.equal(state.stacks.size, 0);
  assert.equal(firstCalls, 3);
  assert.equal(state.destructiveCalls, firstCalls);
});

test("delete-backups converges while the vault remains and discovery returns no deleted points", async () => {
  const points = new Set(["recovery-point-1", "recovery-point-2"]);
  let deletes = 0;
  const deps = deletionDeps({ instances: new Set(), stacks: new Set(), destructiveCalls: 0 });
  deps.listRecoveryPoints = async () => [...points].map((arn) => ({ arn, status: "COMPLETED" }));
  deps.deleteRecoveryPoint = async ({ recoveryPointArn }) => {
    if (points.delete(recoveryPointArn)) deletes += 1;
  };

  await deleteBackupsCommand(deps, { clusterName: CLUSTER, force: true });
  await deleteBackupsCommand(deps, { clusterName: CLUSTER, force: true });

  assert.equal(points.size, 0);
  assert.equal(deletes, 2);
});

/** Atomic current-object implementation used to exercise concurrent lease acquisition. */
class MemoryStateObjects implements UpgradeStateObjectApi {
  private object: VersionedUpgradeStateObject | undefined;
  private revision = 0;

  async getObject(): Promise<VersionedUpgradeStateObject | undefined> {
    return this.object === undefined ? undefined : { ...this.object };
  }

  async putObject(input: {
    bucket: string;
    key: string;
    body: string;
    condition: UpgradeStateWriteCondition;
  }): Promise<{ revision: string } | undefined> {
    const matches = input.condition.kind === "absent"
      ? this.object === undefined
      : this.object?.revision === input.condition.revision;
    if (!matches) return undefined;
    this.revision += 1;
    const revision = `revision-${this.revision}`;
    this.object = { body: input.body, revision };
    return { revision };
  }
}

test("the durable upgrade record admits exactly one concurrent starter", async () => {
  const api = new MemoryStateObjects();
  const plan = (deploymentId: string): UpgradePlan => ({
    clusterName: CLUSTER,
    awsRegion: REGION,
    targetVersion: "26.09.0",
    targetBaseOs: "amazonlinux2023",
    moduleSet: MODULE_SET,
    selectedModules: ["cluster"],
    deploymentId,
  });

  const results = await Promise.allSettled([
    UpgradeStateJournal.start(
      api,
      { bucket: "sample-bucket" },
      plan("00000000-0000-4000-8000-000000000001"),
      { holderId: "operator-1", now: () => 1, leaseMs: 60_000 },
    ),
    UpgradeStateJournal.start(
      api,
      { bucket: "sample-bucket" },
      plan("00000000-0000-4000-8000-000000000002"),
      { holderId: "operator-2", now: () => 1, leaseMs: 60_000 },
    ),
  ]);

  assert.equal(results.filter((result) => result.status === "fulfilled").length, 1);
  const rejected = results.find((result) => result.status === "rejected");
  assert.ok(rejected?.status === "rejected" && rejected.reason instanceof UpgradeStateConflictError);
});
