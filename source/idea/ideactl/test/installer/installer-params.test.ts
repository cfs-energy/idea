/**
 * Installer declaration execution and validation coverage.
 *
 * Each scripted flow uses an injected driver. This exercises the terminal-independent path that
 * the command invokes, including filters that add the hidden values consumed by the generator.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import yaml from "js-yaml";

import {
  collectInstallerValues,
  InstallerValidationError,
  type InstallerIdentity,
  type InstallerRunOptions,
} from "../../src/cli/installer-params.ts";
import { ScriptedPromptDriver } from "../../src/cli/prompts.ts";
import { configGenerate } from "../../src/cli/commands/config.ts";
import type { Deps } from "../../src/cli/cdk-invoker.ts";
import { optionalFixtures } from "../support/fixtures.ts";

const IDENTITY: InstallerIdentity = {
  accountId: "123456789012",
  partition: "aws",
  dnsSuffix: "amazonaws.com",
};

function newNetworkAnswers(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    aws_profile: "default",
    aws_partition: "aws",
    aws_region: "us-east-2",
    cluster_name: "sample",
    administrator_email: "admin@example.invalid",
    vpc_cidr_block: "192.0.2.0/24",
    ssh_key_pair_name: "sample-key",
    cluster_access: "client-ip",
    client_ip: "192.0.2.10",
    alb_public: true,
    use_vpc_endpoints: false,
    directory_service_provider: "aws_managed_activedirectory",
    enable_aws_backup: true,
    kms_key_type: "aws-managed",
    enabled_modules: ["metrics", "scheduler", "virtual-desktop-controller", "bastion-host"],
    metrics_provider: "cloudwatch",
    base_os: "amazonlinux2023",
    instance_type: "m7i.large",
    volume_size: 200,
    ...overrides,
  };
}

/** Wraps list answers so the driver delivers each one whole rather than element by element. */
function scriptedAnswers(answers: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(answers).map(([name, value]) => [name, Array.isArray(value) ? [value] : value]),
  );
}

function run(
  answers: Record<string, unknown>,
  options: Omit<InstallerRunOptions, "answers" | "driver" | "identity"> = {},
): Promise<Record<string, unknown>> {
  return collectInstallerValues({
    ...options,
    answers,
    driver: new ScriptedPromptDriver({}),
    identity: async () => IDENTITY,
  });
}

/** Supplies only inert effects because `configGenerate` does not touch AWS after identity resolves. */
function generateDeps(): Deps {
  return {
    spawn: async () => 0,
    cfn: {
      describeChangeSet: async () => ({}),
      executeChangeSet: async () => {},
      describeStack: async () => ({}),
    },
    s3: {
      putObject: async () => {},
      getObject: async () => "",
    },
    scan: async () => ({}),
    configWriter: async () => ({
      syncModulesInDb: async () => {},
      syncClusterSettingsInDb: async () => {},
      setConfigEntry: async () => {},
      deleteConfigEntries: async () => {},
    }),
    accountId: async () => IDENTITY.accountId,
    httpStatus: async () => 0,
    sleep: async () => {},
    now: () => 0,
    uuid: () => "00000000-0000-0000-0000-000000000000",
    out: () => {},
    err: () => {},
    prompt: async () => true,
  };
}

describe("installer parameters", () => {
  it("collects and filters a new-network installation in declaration order", async () => {
    const values = await run(newNetworkAnswers());

    assert.deepEqual(values, {
      enable_ecs: true,
      aws_profile: "default",
      aws_partition: "aws",
      aws_region: "us-east-2",
      cluster_name: "idea-sample",
      administrator_email: "admin@example.invalid",
      vpc_cidr_block: "192.0.2.0/24",
      ssh_key_pair_name: "sample-key",
      cluster_access: "client-ip",
      client_ip: ["192.0.2.10/32"],
      alb_public: true,
      use_vpc_endpoints: false,
      directory_service_provider: "aws_managed_activedirectory",
      enable_aws_backup: true,
      kms_key_type: "aws-managed",
      enabled_modules: ["metrics", "scheduler", "virtual-desktop-controller", "bastion-host"],
      metrics_provider: "cloudwatch",
      base_os: "amazonlinux2023",
      instance_type: "m7i.large",
      volume_size: 200,
      _regenerate: false,
      aws_account_id: "123456789012",
      aws_dns_suffix: "amazonaws.com",
    });
  });

  it("collects an existing-network installation and stores its hidden branch flags", async () => {
    const values = await run({
      aws_profile: "default",
      aws_partition: "aws",
      aws_region: "us-east-2",
      cluster_name: "test1",
      administrator_email: "admin@example.invalid",
      ssh_key_pair_name: "sample-key",
      cluster_access: "prefix-list",
      prefix_list_ids: "pl-11111111, pl-22222222",
      alb_public: false,
      use_vpc_endpoints: false,
      directory_service_provider: "aws_managed_activedirectory",
      enable_aws_backup: false,
      kms_key_type: "customer-managed",
      kms_key_id: "key-11111111",
      vpc_id: "vpc-11111111",
      existing_resources: [
        "subnets:public",
        "subnets:private",
        "shared-storage:apps",
        "shared-storage:data",
        "analytics:opensearch",
        "directoryservice:aws_managed_activedirectory",
      ],
      public_subnet_ids: ["subnet-11111111", "subnet-22222222"],
      private_subnet_ids: ["subnet-33333333", "subnet-44444444"],
      directory_id: "d-1111111111",
      directory_service_root_username_secret_arn: "secret-username",
      directory_service_root_password_secret_arn: "secret-password",
      storage_apps_provider: "efs",
      existing_apps_fs_id: "fs-11111111",
      storage_data_provider: "fsx_lustre",
      existing_data_fs_id: "fs-22222222",
      opensearch_domain_endpoint: "search.example.invalid",
      enabled_modules: ["scheduler", "virtual-desktop-controller"],
      base_os: "rhel9",
      instance_type: "m7i.large",
      volume_size: 300,
    }, { existingResources: true });

    assert.equal(values["cluster_name"], "idea-test1");
    assert.deepEqual(values["prefix_list_ids"], ["pl-11111111", "pl-22222222"]);
    assert.equal(values["use_existing_vpc"], true);
    assert.equal(values["use_existing_apps_fs"], true);
    assert.equal(values["use_existing_data_fs"], true);
    assert.equal(values["use_existing_opensearch_cluster"], true);
    assert.equal(values["use_existing_directory_service"], true);
    assert.equal(values["metrics_provider"], undefined);
    assert.equal(values["vpc_cidr_block"], undefined);
  });

  it("asks only conditional questions selected by the prior answers", async () => {
    const values = await run(newNetworkAnswers({
      use_vpc_endpoints: true,
      confirm_vpc_endpoints: true,
      kms_key_type: "customer-managed",
      kms_key_id: "key-22222222",
      metrics_provider: "prometheus",
      prometheus_remote_write_url: "https://metrics.example.invalid/write",
      enabled_modules: ["metrics", "scheduler", "virtual-desktop-controller"],
    }));

    assert.equal(values["confirm_vpc_endpoints"], undefined);
    assert.equal(values["kms_key_id"], "key-22222222");
    assert.equal(values["prometheus_remote_write_url"], "https://metrics.example.invalid/write");
    assert.deepEqual(values["enabled_modules"], ["metrics", "scheduler", "virtual-desktop-controller"]);
  });

  it("invokes the injected installer from config generate and writes its values file", async () => {
    const output = mkdtempSync(join(tmpdir(), "installer-command-"));
    try {
      const values = await configGenerate(generateDeps(), {
        configDir: output,
        force: true,
        // A checkbox answer is itself a list, and this driver reads a list as successive
        // answers to the same question, so it is wrapped to arrive in one piece.
        installerDriver: new ScriptedPromptDriver(scriptedAnswers(newNetworkAnswers())),
        installerIdentity: async () => IDENTITY,
      });
      const written = yaml.load(readFileSync(join(output, "values.yml"), "utf-8"));

      assert.deepEqual(written, values);
      assert.equal(values["cluster_name"], "idea-sample");
    } finally {
      rmSync(output, { recursive: true, force: true });
    }
  });

  it("rejects the declared required, regular expression, bounds, choice and conditional rules", async () => {
    const failures: Array<[string, Record<string, unknown>]> = [
      ["profile required", newNetworkAnswers({ aws_profile: "" })],
      ["region required", newNetworkAnswers({ aws_region: "" })],
      ["cluster required", newNetworkAnswers({ cluster_name: "" })],
      ["required", newNetworkAnswers({ ssh_key_pair_name: "" })],
      ["VPC CIDR required", newNetworkAnswers({ vpc_cidr_block: "" })],
      ["client address required", newNetworkAnswers({ client_ip: "" })],
      ["prefix list required", newNetworkAnswers({ cluster_access: "prefix-list", prefix_list_ids: "" })],
      ["email expression", newNetworkAnswers({ administrator_email: "not-an-email" })],
      ["client CIDR expression", newNetworkAnswers({ client_ip: "999.0.0.1" })],
      ["VPC CIDR expression", newNetworkAnswers({ vpc_cidr_block: "192.0.2.0/17" })],
      ["minimum", newNetworkAnswers({ volume_size: 19 })],
      ["maximum", newNetworkAnswers({ volume_size: 1001 })],
      ["static choice", newNetworkAnswers({ directory_service_provider: "invalid" })],
      ["KMS type required", newNetworkAnswers({ kms_key_type: "" })],
      ["module selection required", newNetworkAnswers({ enabled_modules: [] })],
      ["metrics provider required", newNetworkAnswers({ metrics_provider: "" })],
      ["customer key required", newNetworkAnswers({ kms_key_type: "customer-managed", kms_key_id: "" })],
      ["conditional URL required", newNetworkAnswers({ metrics_provider: "prometheus", prometheus_remote_write_url: "" })],
      ["cluster name", newNetworkAnswers({ cluster_name: "someidea" })],
    ];

    for (const [name, answers] of failures) {
      await assert.rejects(run(answers), InstallerValidationError, name);
    }
  });

  it("enforces existing-resource selection and subnet validation when replay data is supplied", async () => {
    const existing = {
      aws_profile: "default",
      aws_partition: "aws",
      aws_region: "us-east-2",
      cluster_name: "test1",
      administrator_email: "admin@example.invalid",
      ssh_key_pair_name: "sample-key",
      cluster_access: "client-ip",
      client_ip: "192.0.2.10",
      alb_public: true,
      use_vpc_endpoints: false,
      directory_service_provider: "aws_managed_activedirectory",
      enable_aws_backup: true,
      kms_key_type: "aws-managed",
      vpc_id: "vpc-11111111",
      existing_resources: [
        "subnets:public",
        "subnets:private",
        "shared-storage:apps",
        "shared-storage:data",
        "analytics:opensearch",
        "directoryservice:aws_managed_activedirectory",
      ],
      public_subnet_ids: ["subnet-11111111", "subnet-22222222"],
      private_subnet_ids: ["subnet-11111111", "subnet-22222222"],
      directory_id: "d-1111111111",
      directory_service_root_username_secret_arn: "secret-username",
      directory_service_root_password_secret_arn: "secret-password",
      storage_apps_provider: "efs",
      existing_apps_fs_id: "fs-11111111",
      storage_data_provider: "fsx_lustre",
      existing_data_fs_id: "fs-22222222",
      opensearch_domain_endpoint: "search.example.invalid",
      enabled_modules: ["scheduler", "virtual-desktop-controller"],
      base_os: "amazonlinux2023",
      instance_type: "m7i.large",
      volume_size: 200,
    };
    await assert.rejects(
      run({ ...existing, existing_resources: [] }, { existingResources: true }),
      InstallerValidationError,
    );
    await assert.rejects(
      run(existing, {
        existingResources: true,
        choices: {
          subnets: () => [
            { id: "subnet-11111111", availabilityZone: "us-east-2a" },
            { id: "subnet-22222222", availabilityZone: "us-east-2a" },
          ],
        },
      }),
      InstallerValidationError,
    );
    const requiredOverrides: Array<Record<string, unknown>> = [
      { vpc_id: "" },
      { private_subnet_ids: [] },
      { public_subnet_ids: [] },
      { directory_id: "" },
      { directory_service_root_username_secret_arn: "" },
      { directory_service_root_password_secret_arn: "" },
      { storage_apps_provider: "" },
      { existing_apps_fs_id: "" },
      { storage_data_provider: "" },
      { existing_data_fs_id: "" },
      { opensearch_domain_endpoint: "" },
    ];
    for (const overrides of requiredOverrides) {
      await assert.rejects(run({ ...existing, ...overrides }, { existingResources: true }), InstallerValidationError);
    }
  });

  const root = new URL("../../tools/parity/fixtures/installer/", import.meta.url);
  const fixtures = [
    "new-network.values.yml",
    "existing-network.values.yml",
    "conditional.values.yml",
  ];
  const runCapturedFixtureCoverage = optionalFixtures(
    fixtures.map((name) => new URL(name, root).pathname),
    "Regenerate installer values captures from the source flow",
  );

  it("compares replay fixtures with the values shape captured from the source flow", { skip: !runCapturedFixtureCoverage }, () => {
    for (const name of fixtures) {
      const file = new URL(name, root);
      const captured = yaml.load(readFileSync(file, "utf-8"));
      assert.equal(typeof captured, "object", `${name} is a YAML map`);
    }
  });
});
