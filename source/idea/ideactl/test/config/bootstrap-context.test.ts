/**
 * Host deployments render and upload their bootstrap packages before spawning a deployment.
 * Every external effect is injected, including table scans, object uploads, and child processes.
 */

import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, test } from "node:test";

import {
  renderedTreeId
} from "../../src/cli/bootstrap-package.ts";
import { bootstrapSourceDir, type Deps } from "../../src/cli/cdk-invoker.ts";
import { DeploymentHelper } from "../../src/cli/deployment-helper.ts";
import { ideaVersion } from "../../src/version.ts";
import { fakeContainerHosts } from "../support/host-pool-harness.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";
const MODULE_SET = "default";
const DEPLOYMENT_ID = "deployment-1";
const BUCKET = "sample-bucket";

interface Harness {
  deps: Deps;
  puts: Map<string, Uint8Array | string>;
  spawns: string[][];
}

interface HostCase {
  moduleId: string;
  moduleName: string;
  packages: Array<{
    component: string;
    contextParameter: string;
    basename: string;
    rendered: string;
  }>;
}

type JsonObject = Record<string, unknown>;

let homeDirectory = "";
let sourceDirectory = "";
let previousIdeaHome: string | undefined;
let previousCdkBin: string | undefined;

/** Writes a compact bootstrap tree whose templates exercise each role's provider values. */
function writeBootstrapSource(root: string): void {
  const templates: Record<string, string> = {
    "openldap-server":
      "module={{ context.module_name }};base={{ context.base_os }};instance={{ context.instance_type }};region={{ context.config.get_string('cluster.aws.region', required=True) }}",
    "cluster-manager":
      "module={{ context.module_name }};release={{ context.vars.app_package_uri }};packages={{ ' '.join(context.config.get_list('global-settings.test_packages', default=[])) }}",
    scheduler:
      "module={{ context.module_name }};release={{ context.vars.app_package_uri }}",
    "bastion-host":
      "module={{ context.module_name }};user={{ context.default_system_user }};storage={{ context.has_storage_provider('efs') }}",
    "virtual-desktop-controller":
      "module={{ context.module_name }};base={{ context.base_os }};release={{ context.vars.controller_package_uri }}",
    "dcv-broker":
      "module={{ context.module_name }};base={{ context.base_os }};instance={{ context.instance_type }}",
    "dcv-connection-gateway":
      "module={{ context.module_name }};base={{ context.base_os }};release={{ context.vars.dcv_connection_gateway_package_uri }}",
  };
  mkdirSync(join(root, "common"), { recursive: true });
  writeFileSync(join(root, "common", "bootstrap_common.sh"), "common", "utf8");
  for (const [component, template] of Object.entries(templates)) {
    const directory = join(root, component);
    mkdirSync(directory, { recursive: true });
    writeFileSync(join(directory, "setup.sh.jinja2"), template, "utf8");
  }
}

/** Narrows a context value to an object for precise assertions. */
function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Returns a validated context object. */
function objectValue(value: unknown, description: string): JsonObject {
  assert.ok(isJsonObject(value), description);
  return value;
}

/** Returns the synthetic settings shared by all host module cases. */
function settings(): Array<{ key: string; value: unknown }> {
  const values: Record<string, unknown> = {
    "cluster.cluster_name": CLUSTER,
    "cluster.cluster_s3_bucket": BUCKET,
    "cluster.home_dir": `/apps/${CLUSTER}`,
    "cluster.aws.region": REGION,
    "cluster.cloudwatch_logs.enabled": false,
    "global-settings.custom_tags": [],
    "global-settings.gpu_settings.instance_families": [],
    "global-settings.gpu_settings.nvidia_public_driver_versions": {},
    "global-settings.test_packages": ["one", "two"],
    "global-settings.module_sets.default.cluster.module_id": "cluster",
    "global-settings.module_sets.default.directoryservice.module_id":
      "directoryservice",
    "global-settings.module_sets.default.cluster-manager.module_id":
      "cluster-manager",
    "global-settings.module_sets.default.scheduler.module_id": "scheduler",
    "global-settings.module_sets.default.bastion-host.module_id": "bastion-host",
    "global-settings.module_sets.default.virtual-desktop-controller.module_id":
      "vdc",
    "directoryservice.provider": "openldap",
    "directoryservice.base_os": "amazonlinux2023",
    "directoryservice.instance_type": "m7i.large",
    "cluster-manager.ec2.autoscaling.base_os": "amazonlinux2023",
    "cluster-manager.ec2.autoscaling.instance_type": "m7i.large",
    "scheduler.base_os": "amazonlinux2023",
    "scheduler.instance_type": "m7i.large",
    "scheduler.provider": "openpbs",
    "shared-storage.data.provider": "efs",
    "bastion-host.base_os": "ubuntu2404",
    "bastion-host.instance_type": "m7i.large",
    "vdc.controller.autoscaling.base_os": "amazonlinux2023",
    "vdc.controller.autoscaling.instance_type": "m7i.large",
    "vdc.dcv_broker.autoscaling.base_os": "rhel9",
    "vdc.dcv_broker.autoscaling.instance_type": "m7i.xlarge",
    "vdc.dcv_connection_gateway.autoscaling.base_os": "rocky9",
    "vdc.dcv_connection_gateway.autoscaling.instance_type": "c7g.large",
  };
  return Object.entries(values).map(([key, value]) => ({ key, value }));
}

/** Builds module rows with deployed prerequisites and one deployable target. */
function moduleRows(moduleId: string, moduleName: string): Array<Record<string, unknown>> {
  const modules = new Map<string, Record<string, unknown>>([
    [
      "cluster",
      {
        module_id: "cluster",
        name: "cluster",
        type: "stack",
        status: "deployed",
      },
    ],
    [
      "scheduler",
      {
        module_id: "scheduler",
        name: "scheduler",
        type: "app",
        status: "deployed",
      },
    ],
  ]);
  modules.set(moduleId, {
    module_id: moduleId,
    name: moduleName,
    type: moduleName === "bastion-host" || moduleName === "directoryservice" ? "stack" : "app",
    status: "not-deployed",
  });
  return [...modules.values()];
}

/** Creates fakes for every effect the deployment helper can perform. */
function harness(
  moduleId: string,
  moduleName: string,
  failBootstrapUpload = false,
): Harness {
  const puts = new Map<string, Uint8Array | string>();
  const spawns: string[][] = [];
  const tables: Record<string, Array<Record<string, unknown>>> = {
    [`${CLUSTER}.cluster-settings`]: settings(),
    [`${CLUSTER}.modules`]: moduleRows(moduleId, moduleName),
  };
  const hosts = fakeContainerHosts();
  hosts.state.old = false;
  hosts.state.joined = true;
  const deps: Deps = {
    containerHosts: hosts.api,
    spawn: async (argv) => {
      spawns.push(argv);
      return 0;
    },
    cfn: {
      describeChangeSet: async () => ({
        Status: "CREATE_COMPLETE",
        Changes: [],
      }),
      executeChangeSet: async () => { },
      describeStack: async () => ({
        StackStatus: "UPDATE_COMPLETE",
        Outputs: [],
      }),
    },
    s3: {
      putObject: async (input) => {
        if (failBootstrapUpload && input.Key.startsWith("idea/bootstrap/")) {
          throw new Error("bootstrap upload failed");
        }
        puts.set(`${input.Bucket}/${input.Key}`, input.Body);
      },
      getObject: async () => {
        throw new Error("unexpected object read");
      },
    },
    scan: async (input) => ({ Items: tables[input.TableName] ?? [] }),
    configWriter: async () => {
      throw new Error("unexpected configuration write");
    },
    accountId: async () => "123456789012",
    httpStatus: async () => 200,
    sleep: async () => { },
    now: () => 0,
    uuid: () => DEPLOYMENT_ID,
    out: () => { },
    err: () => { },
    prompt: async () => true,
    bootstrapSourceDir: sourceDirectory,
  };
  return { deps, puts, spawns };
}

/** Runs one host deployment through the public helper. */
async function deploy(host: HostCase, fake: Harness): Promise<void> {
  const helper = await DeploymentHelper.open({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: MODULE_SET,
    deploymentId: DEPLOYMENT_ID,
    moduleIds: [host.moduleId],
    deps: fake.deps,
  });
  await helper.invoke();
}

before(() => {
  previousIdeaHome = process.env.IDEA_USER_HOME;
  previousCdkBin = process.env.IDEA_CDK_BIN;
  homeDirectory = mkdtempSync(join(tmpdir(), "f-bootctx-home-"));
  sourceDirectory = mkdtempSync(join(tmpdir(), "f-bootctx-source-"));
  process.env.IDEA_USER_HOME = homeDirectory;
  process.env.IDEA_CDK_BIN = "/opt/idea/test-cdk";
  writeBootstrapSource(sourceDirectory);

  const releaseVersion = ideaVersion();
  const downloads = join(homeDirectory, "downloads");
  mkdirSync(downloads, { recursive: true });
  for (const name of [
    `idea-cluster-manager-${releaseVersion}.tar.gz`,
    `idea-scheduler-${releaseVersion}.tar.gz`,
    `idea-virtual-desktop-controller-${releaseVersion}.tar.gz`,
    `idea-dcv-connection-gateway-${releaseVersion}.tar.gz`,
  ]) {
    writeFileSync(join(downloads, name), `release:${name}`, "utf8");
  }
});

after(() => {
  if (previousIdeaHome === undefined) delete process.env.IDEA_USER_HOME;
  else process.env.IDEA_USER_HOME = previousIdeaHome;
  if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
  else process.env.IDEA_CDK_BIN = previousCdkBin;
  rmSync(homeDirectory, { recursive: true, force: true });
  rmSync(sourceDirectory, { recursive: true, force: true });
});

const releaseVersion = ideaVersion();
const hosts: HostCase[] = [{
  moduleId: "directoryservice",
  moduleName: "directoryservice",
  packages: [
    {
      component: "openldap-server",
      contextParameter: "bootstrap_package_uri",
      basename: `bootstrap-directoryservice-${DEPLOYMENT_ID}`,
      rendered:
        "module=directoryservice;base=amazonlinux2023;instance=m7i.large;region=us-east-2",
    },
  ],
}];

describe("host bootstrap deployment context", () => {
  for (const host of hosts) {
    test(`${host.moduleName} renders, uploads, and passes every package location`, async () => {
      const fake = harness(host.moduleId, host.moduleName);
      await deploy(host, fake);

      assert.equal(fake.spawns.length, 1);
      const argv = fake.spawns[0] ?? [];
      for (const expected of host.packages) {
        // The rendered directory keeps the deployment id; the archive takes the rendered tree's
        // content id in its place, so an unchanged package keeps its location across deploys.
        const renderedDirectory = join(homeDirectory, "clusters", CLUSTER, REGION, "deployments", DEPLOYMENT_ID, expected.basename);
        const archive = `${expected.basename.slice(0, -DEPLOYMENT_ID.length)}${renderedTreeId(renderedDirectory)}.tar.gz`;
        const uri = `s3://${BUCKET}/idea/bootstrap/${archive}`;
        assert.ok(
          fake.puts.has(`${BUCKET}/idea/bootstrap/${archive}`),
          `${archive} was not uploaded`,
        );
        assert.ok(
          argv.includes(`${expected.contextParameter}=${uri}`),
          `${expected.contextParameter} was not passed`,
        );
        const rendered = readFileSync(
          join(
            homeDirectory,
            "clusters",
            CLUSTER,
            REGION,
            "deployments",
            DEPLOYMENT_ID,
            expected.basename,
            expected.component,
            "setup.sh",
          ),
          "utf8",
        );
        assert.equal(rendered, expected.rendered);
      }
    });
  }
});

test("a failed bootstrap upload aborts before deployment", async () => {
  const host = hosts[0];
  if (host === undefined) throw new Error("host cases are required");
  const fake = harness(host.moduleId, host.moduleName, true);
  await assert.rejects(() => deploy(host, fake), /bootstrap upload failed/);
  assert.deepEqual(fake.spawns, []);
});

const shippedSetupTemplates = [
  ["openldap-server/setup.sh.jinja2", "IDEA_MODULE_NAME={{ context.module_name }}", "IDEA_BASE_OS={{ context.base_os }}", "AWS_REGION={{ context.aws_region }}"],
] as const;

for (const [relative, ...needles] of shippedSetupTemplates) {
  test(`shipped ${relative} interpolates host context`, () => {
    const source = readFileSync(join(bootstrapSourceDir(), relative), "utf8");
    for (const needle of needles) {
      assert.ok(source.includes(needle), `${relative} is missing ${needle}`);
    }
  });
}

test("activedirectory directoryservice deploys without a host package", async () => {
  const host = hosts[0];
  if (host === undefined) throw new Error("host cases are required");
  const fake = harness("directoryservice", "directoryservice");
  const originalScan = fake.deps.scan;
  fake.deps.scan = async (input) => {
    const page = await originalScan(input);
    if (input.TableName !== `${CLUSTER}.cluster-settings`) return page;
    const items = (page.Items ?? []).map((row) => (
      row.key === "directoryservice.provider" ? { key: "directoryservice.provider", value: "activedirectory" } : row
    ));
    items.push({ key: "cluster.cloudwatch_logs.enabled", value: true });
    return { Items: items };
  };
  const packageRoot = join(
    homeDirectory, "clusters", CLUSTER, REGION, "deployments", DEPLOYMENT_ID,
    `bootstrap-directoryservice-${DEPLOYMENT_ID}`,
  );
  rmSync(packageRoot, { recursive: true, force: true });
  await deploy(host, fake);
  const openldapSetup = join(packageRoot, "openldap-server", "setup.sh");
  assert.ok(!existsSync(openldapSetup), "AD must not keep the OpenLDAP component path");
  assert.ok(
    ![...fake.puts.keys()].some((key) => key.includes("idea/bootstrap/")),
    "AD must not upload a directoryservice host package",
  );
  assert.equal(fake.spawns.length, 1);
  const argv = fake.spawns[0] ?? [];
  assert.ok(
    !argv.some((argument) => argument.startsWith("bootstrap_package_uri=")),
    "AD must not pass bootstrap_package_uri",
  );
});
