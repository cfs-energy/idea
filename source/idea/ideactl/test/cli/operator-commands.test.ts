import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { ClusterConfig, type ModuleInfo } from "../../src/config/cluster-config.ts";
import { generateConfigFromTemplates } from "../../src/config/generator.ts";
import { loadValuesFile } from "../../src/config/values.ts";
import { createServiceAccountSecrets } from "../../src/cli/commands/directoryservice.ts";
import { buildSharedStorageConfig, manageSharedStorage, NEXT_STEP_UPGRADE_MODULE } from "../../src/cli/commands/shared-storage.ts";
import { buildDeploymentSupportPackage, PACKAGE_CLUSTER_CONFIG_DB, PACKAGE_VALUES_FILE } from "../../src/cli/commands/support.ts";
import { configureSso, showIdpInfo } from "../../src/cli/commands/sso.ts";
import { IntegrationTestFailed, parseIntegrationParams, runIntegrationTests } from "../../src/cli/commands/tests.ts";
import { addPrefixListEntry, awsServiceAvailability, backupUpdateGlobalSettings, prefixListEntries, removePrefixListEntry, type UtilsApi, vpcEndpointServiceInfo } from "../../src/cli/commands/utils.ts";
import { requireCapture } from "../support/fixtures.ts";

const DEV27_VALUES = join(process.cwd(), "tools", "parity", "fixtures", "idea-dev27", "values.yml");
requireCapture(
  [DEV27_VALUES],
  "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27",
);

function config(entries: Record<string, unknown>, modules: ModuleInfo[] = []): ClusterConfig {
  return new ClusterConfig(Object.entries(entries).map(([key, value]) => ({ key, value })), modules);
}

function stringRecord(value: unknown): Record<string, string> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
  const result: Record<string, string> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === "string") result[key] = entry;
  }
  return result;
}

const modules: ModuleInfo[] = [
  { module_id: "identity", name: "identity-provider", type: "stack", status: "deployed" },
  { module_id: "cluster", name: "cluster", type: "stack", status: "deployed" },
  { module_id: "storage", name: "shared-storage", type: "stack", status: "deployed" },
  { module_id: "scheduler", name: "scheduler", type: "app", status: "deployed" },
];

test("sso configuration performs all steps and enables SSO last", async () => {
  const writes: Array<[string, unknown]> = [];
  const events: string[] = [];
  let clientRequest: Record<string, unknown> | undefined;
  let secretRequest: Record<string, unknown> | undefined;
  const settings = config({
    "global-settings.module_sets.default.identity-provider.module_id": "identity",
    "identity.cognito.user_pool_id": "pool",
    "identity.cognito.domain_url": "https://login.example.invalid",
    "cluster.load_balancers.external_alb.load_balancer_dns_name": "alb.example.invalid",
    "cluster-manager.server.web_resources_context_path": "/portal",
    "cluster.administrator_username": "admin",
  }, modules);
  await configureSso({
    config: settings,
    setConfigEntry: async (key, value) => { writes.push([key, value]); },
    sleep: async (ms) => { events.push(`sleep:${ms}`); },
    out: (line) => { events.push(line); },
    cognito: {
      getIdentityProviderByIdentifier: async () => { throw Object.assign(new Error("missing"), { name: "ResourceNotFoundException" }); },
      createIdentityProvider: async (input) => { events.push(`idp:${String(input.ProviderType)}`); },
      updateIdentityProvider: async () => { throw new Error("unexpected update"); },
      createUserPoolClient: async (input) => {
        clientRequest = input;
        events.push("client");
        return { UserPoolClient: { ClientId: "client", ClientSecret: "secret" } };
      },
      updateUserPoolClient: async () => { throw new Error("unexpected client update"); },
      listUsers: async () => ({ Users: [{ Username: "member", Attributes: [{ Name: "email", Value: "member@example.invalid" }] }] }),
      adminLinkProviderForUser: async () => { events.push("linked"); },
    },
    secrets: {
      describeSecret: async () => { throw Object.assign(new Error("missing"), { name: "ResourceNotFoundException" }); },
      createSecret: async (input) => {
        secretRequest = input;
        events.push("secret");
        return { ARN: "secret-id" };
      },
      updateSecret: async () => { throw new Error("unexpected secret update"); },
    },
  }, {
    clusterName: "sample-cluster", providerName: "provider", providerType: "OIDC", providerEmailAttribute: "mail",
    oidcClientId: "client-input", oidcClientSecret: "secret-input", oidcIssuer: "https://issuer.example.invalid",
  });
  assert.deepEqual(events, ["idp:OIDC", "client", "secret", "linking user: member, email: member@example.invalid ...", "linked", "sleep:200"]);
  assert.equal(clientRequest?.ClientName, "single-sign-on-client");
  assert.equal(clientRequest?.GenerateSecret, true);
  assert.deepEqual(clientRequest?.CallbackURLs, ["https://alb.example.invalid/portal/oauth2/callback"]);
  assert.deepEqual(clientRequest?.SupportedIdentityProviders, ["provider"]);
  assert.equal(secretRequest?.Name, "sample-cluster-sso-client-secret");
  assert.equal(secretRequest?.SecretString, "secret");
  assert.deepEqual(writes.find((entry) => entry[0] === "identity.cognito.sso_client_id"), ["identity.cognito.sso_client_id", "client"]);
  assert.deepEqual(writes.find((entry) => entry[0] === "identity.cognito.sso_client_secret"), ["identity.cognito.sso_client_secret", "secret-id"]);
  assert.equal(writes.at(-1)?.[0], "identity.cognito.sso_enabled");
  assert.equal(writes.at(-1)?.[1], true);
  assert.deepEqual(showIdpInfo(settings, "SAML"), { redirectUrl: "https://login.example.invalid/saml2/idpresponse", entityId: "urn:amazon:cognito:sp:pool" });
});

test("SAML configuration sends metadata and enables SSO last", async () => {
  const writes: Array<[string, unknown]> = [];
  const events: string[] = [];
  let providerRequest: Record<string, unknown> | undefined;
  const settings = config({
    "global-settings.module_sets.default.identity-provider.module_id": "identity",
    "identity.cognito.user_pool_id": "pool",
    "identity.cognito.domain_url": "https://login.example.invalid",
    "cluster.load_balancers.external_alb.load_balancer_dns_name": "alb.example.invalid",
    "cluster-manager.server.web_resources_context_path": "/portal",
    "cluster.administrator_username": "admin",
  }, modules);
  await configureSso({
    config: settings,
    setConfigEntry: async (key, value) => { writes.push([key, value]); },
    sleep: async () => {},
    out: (line) => { events.push(line); },
    cognito: {
      getIdentityProviderByIdentifier: async () => { throw Object.assign(new Error("missing"), { name: "ResourceNotFoundException" }); },
      createIdentityProvider: async (input) => {
        providerRequest = input;
        events.push(`idp:${String(input.ProviderType)}`);
      },
      updateIdentityProvider: async () => { throw new Error("unexpected update"); },
      createUserPoolClient: async () => ({ UserPoolClient: { ClientId: "client", ClientSecret: "secret" } }),
      updateUserPoolClient: async () => { throw new Error("unexpected client update"); },
      listUsers: async () => ({ Users: [] }),
      adminLinkProviderForUser: async () => {},
    },
    secrets: {
      describeSecret: async () => { throw Object.assign(new Error("missing"), { name: "ResourceNotFoundException" }); },
      createSecret: async () => ({ ARN: "secret-id" }),
      updateSecret: async () => ({ ARN: "secret-id" }),
    },
  }, {
    clusterName: "sample-cluster",
    providerName: "saml-idp",
    providerType: "SAML",
    providerEmailAttribute: "mail",
    samlMetadataUrl: "https://idp.example.invalid/metadata",
  });
  assert.equal(events[0], "idp:SAML");
  assert.deepEqual(stringRecord(providerRequest?.ProviderDetails), { MetadataURL: "https://idp.example.invalid/metadata" });
  assert.equal(writes.at(-1)?.[0], "identity.cognito.sso_enabled");
  assert.equal(writes.at(-1)?.[1], true);
});

test("directory service creates username before password secrets", async () => {
  const calls: string[] = [];
  const result = await createServiceAccountSecrets({
    secrets: { createSecret: async (input) => { calls.push(input.Name); return { ARN: `id:${calls.length}` }; } },
    out: () => {},
  }, { clusterName: "sample-cluster", purpose: "service-account", username: "bind-user", password: "bind-password", kmsKeyId: "key-id" });
  assert.deepEqual(calls, ["sample-cluster-directoryservice-service-account-username", "sample-cluster-directoryservice-service-account-password"]);
  assert.deepEqual(result, { purpose: "service-account", usernameSecretArn: "id:1", passwordSecretArn: "id:2" });
});

test("shared storage reads the provider response then writes and upgrades", async () => {
  const writes: Array<{ key: string; value: unknown }> = [];
  const deployments: Array<[string, boolean]> = [];
  const described: Array<string | undefined> = [];
  const deps = {
    config: config({
      "global-settings.module_sets.default.cluster.module_id": "cluster",
      "global-settings.module_sets.default.shared-storage.module_id": "storage",
    }, modules),
    awsDnsSuffix: async () => "amazonaws.com",
    storage: {
      describeFileSystems: async (input: { FileSystemId?: string }) => {
        described.push(input.FileSystemId);
        return { FileSystems: [{ Encrypted: input.FileSystemId === "fs-2" }] };
      },
      describeFileCaches: async () => ({ FileCaches: [] }),
      describeStorageVirtualMachines: async () => ({ StorageVirtualMachines: [] }),
      describeVolumes: async () => ({ Volumes: [] }),
    },
    prompt: async () => ({ shared_storage_name: "apps", shared_storage_provider: "efs", shared_storage_title: "Apps", shared_storage_scope: "cluster", "efs.file_system_id": "fs-1" }),
    promptNextStep: async (choices: string[]) => { assert.ok(choices.includes(NEXT_STEP_UPGRADE_MODULE)); return NEXT_STEP_UPGRADE_MODULE; },
    syncSettings: async (entries: Array<{ key: string; value: unknown }>) => { writes.push(...entries); },
    deploy: async (id: string, upgrade: boolean) => { deployments.push([id, upgrade]); },
    out: () => {},
  };
  await manageSharedStorage(deps, { clusterName: "sample-cluster", awsRegion: "us-east-1" }, false);
  assert.deepEqual(deployments, [["storage", true]]);
  assert.equal(writes.find((entry) => entry.key === "storage.apps.efs.use_existing_fs"), undefined);
  const attached = await buildSharedStorageConfig(deps, { awsRegion: "us-east-1" }, { shared_storage_name: "data", shared_storage_provider: "efs", "efs.file_system_id": "fs-2" }, true);
  const attachedEfs = (attached.data as { efs: { dns: string; encrypted: boolean; file_system_id: string } }).efs;
  assert.deepEqual(described, ["fs-2"]);
  assert.equal(attachedEfs.file_system_id, "fs-2");
  assert.equal(attachedEfs.dns, "fs-2.efs.us-east-1.amazonaws.com");
  assert.equal(attachedEfs.encrypted, true);
  const other = await buildSharedStorageConfig(deps, { awsRegion: "us-east-1" }, { shared_storage_name: "scratch", shared_storage_provider: "efs", "efs.file_system_id": "fs-3" }, true);
  assert.equal((other.scratch as { efs: { encrypted: boolean } }).efs.encrypted, false);
});

test("utility commands page service and prefix-list reads and require the current version for writes", async () => {
  const calls: Array<Record<string, unknown>> = [];
  const ssmCalls: Array<{ Path?: string; NextToken?: string }> = [];
  const settings = config({
    "global-settings.module_sets.default.cluster.module_id": "cluster",
    "cluster.network.cluster_prefix_list_id": "pl-1",
  }, modules);
  let servicePage = 0;
  const deps = {
    config: settings,
    dnsSuffix: async () => "amazonaws.com",
    out: () => {},
    api: {
      getParametersByPath: async (input: { Path: string; NextToken?: string }) => {
        ssmCalls.push(input);
        return servicePage++ === 0 ? { Parameters: [{ Value: "ec2" }], NextToken: "next" } : { Parameters: [{ Value: "s3" }] };
      },
      describeVpcEndpointServices: async () => ({ ServiceDetails: [{ ServiceName: "com.amazonaws.us-east-1.s3", ServiceType: [{ ServiceType: "Gateway" }], AvailabilityZones: ["us-east-1a"] }] }),
      getManagedPrefixListEntries: async () => ({ Entries: [{ Cidr: "203.0.113.0/24", Description: "existing" }] }),
      describeManagedPrefixLists: async () => ({ PrefixLists: [{ Version: 7 }] }),
      modifyManagedPrefixList: async (input: Parameters<UtilsApi["modifyManagedPrefixList"]>[0]) => { calls.push(input); },
    },
  };
  const availability = await awsServiceAvailability(deps, ["us-east-1"]);
  assert.equal(ssmCalls.length, 2);
  assert.equal(ssmCalls[1]?.NextToken, "next");
  assert.match(availability, /Amazon Elastic Compute Cloud \(EC2\) \[ec2\]\s+\|\s+Yes\s+\|\s+Yes/);
  assert.match(availability, /Amazon Simple Storage Service \(S3\) \[s3\]\s+\|\s+Yes\s+\|\s+Yes/);
  assert.match(availability, /AWS Certificate Manager \(ACM\) \[acm\]\s+\|\s+Yes\s+\|\s+No/);
  const endpoints = await vpcEndpointServiceInfo(deps, "us-east-1");
  assert.match(endpoints, /com\.amazonaws\.us-east-1\.s3\s+\|\s+Yes\s+\|\s+Gateway\s+\|\s+us-east-1a/);
  assert.match(endpoints, /com\.amazonaws\.us-east-1\.dynamodb\s+\|\s+No\s+\|\s+-\s+\|\s+-/);
  assert.deepEqual(await prefixListEntries(deps), [{ cidr: "203.0.113.0/24", description: "existing" }]);
  await addPrefixListEntry(deps, "198.51.100.0/24", "new");
  await removePrefixListEntry({ ...deps, api: { ...deps.api, getManagedPrefixListEntries: async () => ({ Entries: [{ Cidr: "198.51.100.0/24" }] }) } }, "198.51.100.0/24");
  assert.deepEqual(calls.map((call) => call.CurrentVersion), [7, 7]);
});

test("support package includes selected local and configuration diagnostics", async () => {
  const root = mkdtempSync(join(tmpdir(), "ideactl-support-"));
  const original = process.env.IDEA_USER_HOME;
  process.env.IDEA_USER_HOME = root;
  try {
    const region = join(root, "clusters", "sample-cluster", "us-east-1");
    mkdirSync(region, { recursive: true });
    writeFileSync(join(region, "values.yml"), "cluster_name: sample-cluster\n", { flag: "w" });
    let packaged = "";
    const packageFile = await buildDeploymentSupportPackage({
      now: () => new Date("2026-01-02T03:04:05.000Z"),
      databaseConfig: async () => ({ configYaml: "storage:\n  answer: true\n", modulesYaml: "modules:\n  - scheduler\n" }),
      archive: async (directory) => { packaged = directory; return `${directory}.zip`; },
      out: () => {},
    }, { clusterName: "sample-cluster", awsRegion: "us-east-1" }, [PACKAGE_VALUES_FILE, PACKAGE_CLUSTER_CONFIG_DB]);
    assert.equal(packageFile, `${packaged}.zip`);
    assert.equal(readFileSync(join(packaged, "values.yml"), "utf8"), "cluster_name: sample-cluster\n");
    assert.match(readFileSync(join(packaged, "config_db", "modules.yml"), "utf8"), /scheduler/);
  } finally {
    if (original === undefined) delete process.env.IDEA_USER_HOME;
    else process.env.IDEA_USER_HOME = original;
  }
});

test("global settings backup copies first, regenerates, deletes, then synchronizes", async () => {
  const fixture = DEV27_VALUES;
  const root = mkdtempSync(join(tmpdir(), "ideactl-backup-"));
  const original = process.env.IDEA_USER_HOME;
  process.env.IDEA_USER_HOME = root;
  try {
    const values = loadValuesFile(fixture);
    const clusterName = String(values.cluster_name);
    const region = String(values.aws_region);
    const regionDir = join(root, "clusters", clusterName, region);
    mkdirSync(regionDir, { recursive: true });
    writeFileSync(join(regionDir, "values.yml"), readFileSync(fixture));
    generateConfigFromTemplates(values, join(regionDir, "config"));
    const settingsPath = join(regionDir, "config", "global-settings", "settings.yml");
    const generatedSettings = readFileSync(settingsPath, "utf8");
    const liveMarker = "amazoncloudwatch-agent";
    const staleMarker = "stale-cloudwatch-agent";
    assert.match(generatedSettings, new RegExp(liveMarker));
    writeFileSync(settingsPath, generatedSettings.replaceAll(liveMarker, staleMarker));
    const events: string[] = [];
    const backup = await backupUpdateGlobalSettings({
      config: config({}, modules),
      api: {
        getParametersByPath: async () => ({}),
        describeVpcEndpointServices: async () => ({}),
        getManagedPrefixListEntries: async () => ({}),
        describeManagedPrefixLists: async () => ({}),
        modifyManagedPrefixList: async () => {},
      },
      dnsSuffix: async () => "amazonaws.com",
      exportConfig: async () => { events.push("export"); },
      syncGlobalSettings: async ({ deletePrefix, entries }) => { events.push(`delete:${deletePrefix}`, `sync:${entries.length}`); },
      now: () => new Date("2026-01-02T03:04:05.000Z"),
      out: () => {},
    }, { clusterName, awsRegion: region, force: true });
    assert.equal(existsSync(backup), true);
    assert.ok(events[0] === "export");
    assert.ok(events[1] === "delete:global-settings.");
    assert.match(events[2] ?? "", /^sync:[1-9]/);
    assert.match(readFileSync(join(backup, "global-settings", "settings.yml"), "utf8"), new RegExp(staleMarker));
    const liveSettings = readFileSync(settingsPath, "utf8");
    assert.doesNotMatch(liveSettings, new RegExp(staleMarker));
    assert.match(liveSettings, new RegExp(liveMarker));
  } finally {
    if (original === undefined) delete process.env.IDEA_USER_HOME;
    else process.env.IDEA_USER_HOME = original;
  }
});

test("integration runner dedupes modules, filters cases, and calculates a failure percentage", async () => {
  assert.deepEqual(parseIntegrationParams(["a=one", "ignored", "a=two"]), { a: "two" });
  const moduleNames: string[] = [];
  const passing: string[] = [];
  await runIntegrationTests({
    config: config({}, modules),
    casesForModule: (name) => {
      moduleNames.push(name);
      return [{ id: "pass", run: async () => { passing.push(`${name}:pass`); } }];
    },
    out: () => {}, err: () => {},
  }, { clusterName: "sample-cluster", awsRegion: "us-east-1", adminUsername: "admin", adminPassword: "password" }, ["scheduler", "scheduler"]);
  assert.deepEqual(moduleNames, ["scheduler"]);
  assert.deepEqual(passing, ["scheduler:pass"]);
  const seen: string[] = [];
  await assert.rejects(
    () => runIntegrationTests({
      config: config({}, modules),
      casesForModule: () => [
        { id: "pass", run: async () => { seen.push("pass"); } },
        { id: "skip-me", run: async () => { seen.push("skip-me"); } },
        { id: "fail", run: async () => { seen.push("fail"); throw new Error("bad"); } },
      ],
      out: () => {}, err: () => {},
    }, {
      clusterName: "sample-cluster",
      awsRegion: "us-east-1",
      adminUsername: "admin",
      adminPassword: "password",
      testCaseId: "pass,fail",
    }, ["scheduler"]),
    (error: unknown) => {
      assert.ok(error instanceof IntegrationTestFailed);
      assert.equal(error.message, "1 of 2 test cases failed. success rate: 50%");
      return true;
    },
  );
  assert.deepEqual(seen, ["pass", "fail"]);
});
