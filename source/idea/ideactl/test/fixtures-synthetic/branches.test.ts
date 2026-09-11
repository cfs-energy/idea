/**
 * Synthetic values.yml inputs for public automation.
 *
 * Each YAML file in this directory is one generator branch. Overlay files are merged over
 * `network-new.yml` (the complete new-VPC baseline) so a file only names the keys it changes.
 * The test generates configuration from each input and checks one key that only that branch
 * produces and one key that branch must not emit.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { flattenConfigDir, generateConfigFromTemplates } from "../../src/config/generator.ts";
import { loadValuesFile, type UserValues } from "../../src/config/values.ts";

const here = dirname(fileURLToPath(import.meta.url));
const BASE_FILE = "network-new.yml";

/** One generator branch: the input file, a key only it produces, and a key it must omit. */
interface BranchSpec {
  branch: string;
  file: string;
  present: Record<string, unknown>;
  absent: string[];
}

/**
 * Branch coverage: new and existing networks, each directory
 * provider, each storage provider, each metrics provider, customer-managed keys, no optional
 * modules, and each installer-offered base OS that has an AMI in us-east-2.
 */
const BRANCHES: BranchSpec[] = [
  {
    branch: "new network",
    file: "network-new.yml",
    present: { "cluster.network.vpc_cidr_block": "203.0.113.0/24" },
    absent: ["cluster.network.use_existing_vpc"],
  },
  {
    branch: "directory aws_managed_activedirectory",
    file: "network-new.yml",
    present: { "directoryservice.ad_edition": "Standard" },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "storage efs",
    file: "network-new.yml",
    present: { "shared-storage.apps.efs.performance_mode": "generalPurpose" },
    absent: ["shared-storage.apps.fsx_lustre.storage_type"],
  },
  {
    branch: "metrics cloudwatch",
    file: "network-new.yml",
    present: { "metrics.cloudwatch.dashboard_name": "idea-test1_us-east-2" },
    absent: ["metrics.dogstatsd.url"],
  },
  {
    branch: "base_os amazonlinux2023",
    file: "network-new.yml",
    present: { "bastion-host.base_os": "amazonlinux2023" },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "existing network",
    file: "network-existing.yml",
    present: {
      "cluster.network.use_existing_vpc": true,
      "cluster.network.vpc_id": "vpc-000000000000000a1",
    },
    absent: ["cluster.network.vpc_cidr_block"],
  },
  {
    branch: "vpc endpoints existing vpc",
    file: "network-vpc-endpoints-existing.yml",
    present: {
      "cluster.network.use_vpc_endpoints": true,
      "cluster.network.vpc_gateway_endpoints": [],
      "cluster.network.vpc_interface_endpoints": null,
    },
    absent: ["cluster.network.vpc_cidr_block"],
  },
  {
    branch: "directory openldap",
    file: "directory-openldap.yml",
    present: {
      "directoryservice.provider": "openldap",
      "directoryservice.hostname": "openldap.idea-test1.us-east-2.local",
    },
    absent: ["directoryservice.ad_edition"],
  },
  {
    branch: "directory activedirectory existing",
    file: "directory-activedirectory.yml",
    present: {
      "directoryservice.provider": "activedirectory",
      "directoryservice.root_credentials_provided": true,
      "directoryservice.group_mapping.idea-required-group": "IDEAUsers",
    },
    absent: ["directoryservice.ad_edition"],
  },
  {
    branch: "directory aws_managed_activedirectory existing",
    file: "directory-aws-managed-ad-existing.yml",
    present: {
      "directoryservice.provider": "aws_managed_activedirectory",
      "directoryservice.use_existing": true,
      "directoryservice.directory_id": "d-0000000001",
    },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "storage fsx_lustre",
    file: "storage-fsx-lustre.yml",
    present: {
      "shared-storage.apps.provider": "fsx_lustre",
      "shared-storage.data.provider": "fsx_lustre",
      "shared-storage.apps.fsx_lustre.storage_type": "SSD",
      "shared-storage.data.fsx_lustre.storage_capacity": 1200,
    },
    absent: ["shared-storage.apps.efs.performance_mode"],
  },
  {
    branch: "storage existing apps fs",
    file: "storage-existing-apps-fs.yml",
    present: {
      "shared-storage.apps.efs.use_existing_fs": true,
      "shared-storage.apps.efs.file_system_id": "fs-000000000000000d1",
    },
    absent: ["shared-storage.apps.efs.performance_mode"],
  },
  {
    branch: "metrics prometheus",
    file: "metrics-prometheus.yml",
    present: {
      "metrics.provider": "prometheus",
      "metrics.prometheus.remote_write.url": "https://prometheus.example.invalid/api/v1/write",
      "global-settings.package_config.prometheus.installer.linux.x86_64":
        "https://github.com/prometheus/prometheus/releases/download/v2.53.5/prometheus-2.53.5.linux-amd64.tar.gz",
    },
    absent: ["metrics.cloudwatch.dashboard_name"],
  },
  {
    branch: "metrics amazon_managed_prometheus",
    file: "metrics-amp.yml",
    present: {
      "metrics.provider": "amazon_managed_prometheus",
      "metrics.amazon_managed_prometheus.workspace_name": "idea-test1-workspace",
      "metrics.prometheus.remote_write.url": null,
    },
    absent: ["metrics.cloudwatch.dashboard_name"],
  },
  {
    branch: "metrics dogstatsd",
    file: "metrics-dogstatsd.yml",
    present: {
      "metrics.provider": "dogstatsd",
      "metrics.dogstatsd.url": "udp://127.0.0.1:8125",
    },
    absent: ["metrics.cloudwatch.dashboard_name"],
  },
  {
    branch: "customer managed encryption keys",
    file: "kms-customer-managed.yml",
    present: {
      "cluster.kms.key_type": "customer-managed",
      "cluster.secretsmanager.kms_key_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
      "shared-storage.data.efs.kms_key_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "no optional modules",
    file: "no-optional-modules.yml",
    present: { "cluster.cluster_name": "idea-test1" },
    absent: ["metrics.provider", "scheduler.provider", "vdc.dcv_session.idle_timeout"],
  },
  {
    branch: "alb_public false",
    file: "alb-private.yml",
    present: {
      "cluster.load_balancers.external_alb.public": false,
      "cluster.load_balancers.external_alb.waf.enabled": false,
      "bastion-host.public": false,
    },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "existing OpenSearch",
    file: "analytics-existing-opensearch.yml",
    present: {
      "analytics.opensearch.use_existing": true,
      "analytics.opensearch.domain_vpc_endpoint_url": "opensearch.example.invalid",
    },
    absent: ["analytics.opensearch.data_nodes"],
  },
  {
    branch: "base_os rhel8",
    file: "base-os-rhel8.yml",
    present: {
      "bastion-host.base_os": "rhel8",
      "scheduler.base_os": "rhel8",
      "cluster-manager.ec2.autoscaling.base_os": "rhel8",
    },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "base_os rhel9",
    file: "base-os-rhel9.yml",
    present: {
      "bastion-host.base_os": "rhel9",
      "scheduler.base_os": "rhel9",
      "vdc.controller.autoscaling.base_os": "rhel9",
    },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "base_os rhel10",
    file: "base-os-rhel10.yml",
    present: { "scheduler.base_os": "rhel10", "bastion-host.base_os": "rhel10" },
    absent: ["vdc.controller.autoscaling.base_os"],
  },
  {
    branch: "base_os rocky8",
    file: "base-os-rocky8.yml",
    present: {
      "bastion-host.base_os": "rocky8",
      "scheduler.base_os": "rocky8",
    },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "base_os rocky9",
    file: "base-os-rocky9.yml",
    present: {
      "bastion-host.base_os": "rocky9",
      "scheduler.base_os": "rocky9",
    },
    absent: ["directoryservice.hostname"],
  },
  {
    branch: "base_os rocky10",
    file: "base-os-rocky10.yml",
    present: { "scheduler.base_os": "rocky10", "bastion-host.base_os": "rocky10" },
    absent: ["vdc.controller.autoscaling.base_os"],
  },
];

const ALLOWED_ACCOUNT_IDS = new Set(["123456789012", "111111111111"]);

/** Overlay files are merged over the new-VPC baseline so each file names only its own keys. */
function valuesFor(file: string): UserValues {
  const base = loadValuesFile(join(here, BASE_FILE));
  if (file === BASE_FILE) return base;
  return { ...base, ...loadValuesFile(join(here, file)) };
}

function yamlFiles(): string[] {
  return readdirSync(here)
    .filter((name) => name.endsWith(".yml"))
    .sort();
}

function generateFlat(file: string): Record<string, unknown> {
  const outDir = mkdtempSync(join(tmpdir(), "ideactl-synthetic-branches-"));
  generateConfigFromTemplates(valuesFor(file), outDir);
  return flattenConfigDir(outDir);
}

function inputFiles(): string[] {
  return [...new Set(BRANCHES.map((spec) => spec.file))].sort();
}

describe("synthetic fixtures cover each generator branch", () => {
  it("names every YAML file in this directory as a branch input", () => {
    assert.deepStrictEqual(yamlFiles(), inputFiles());
  });

  const baseline = generateFlat(BASE_FILE);

  for (const file of inputFiles()) {
    const specs = BRANCHES.filter((spec) => spec.file === file);
    it(file, () => {
      const flat = generateFlat(file);
      for (const spec of specs) {
        for (const [key, value] of Object.entries(spec.present)) {
          assert.deepStrictEqual(flat[key], value, `${spec.branch}: ${key}`);
        }
        for (const key of spec.absent) {
          assert.ok(!(key in flat), `${spec.branch}: ${key} must be absent`);
        }
        if (file === BASE_FILE) continue;
        const distinguishing = [
          ...Object.entries(spec.present)
            .filter(([key, value]) => JSON.stringify(baseline[key]) !== JSON.stringify(value))
            .map(([key]) => key),
          ...spec.absent.filter((key) => key in baseline),
        ];
        assert.ok(
          distinguishing.length > 0,
          `${spec.branch}: every assertion also holds for ${BASE_FILE}`,
        );
      }
    });
  }

  it("prints the branch to asserted key table", () => {
    const header = ["branch", "file", "present_key", "absent_key"].join("\t");
    const rows = BRANCHES.map((spec) => {
      const flat = generateFlat(spec.file);
      const presentKey = Object.keys(spec.present)[0] ?? "";
      const absentKey = spec.absent[0] ?? "";
      assert.notEqual(presentKey, "", `${spec.branch}: present_key required`);
      assert.deepStrictEqual(flat[presentKey], spec.present[presentKey], `${spec.branch}: ${presentKey}`);
      assert.ok(!(absentKey in flat), `${spec.branch}: ${absentKey} must be absent`);
      return [spec.branch, spec.file, presentKey, absentKey].join("\t");
    });
    console.log(["BRANCH_TABLE", header, ...rows].join("\n"));
  });
});

describe("synthetic fixtures carry no live account data", () => {
  it("contains no production cluster name, foreign account id, or live hostname", () => {
    for (const name of yamlFiles()) {
      const text = readFileSync(join(here, name), "utf-8");
      const clusters = text.match(/\bidea-[a-z0-9]+\b/g) ?? [];
      for (const cluster of clusters) {
        assert.ok(cluster === "idea-test1", `${name} contains cluster name ${cluster}`);
      }
      const accounts = text.match(/\b\d{12}\b/g) ?? [];
      for (const account of accounts) {
        assert.ok(
          ALLOWED_ACCOUNT_IDS.has(account),
          `${name} contains account id ${account}`,
        );
      }
      const hosts = text.match(/\b[A-Za-z0-9.-]+\.(com|net|org|io|invalid)\b/g) ?? [];
      for (const host of hosts) {
        const allowed =
          host === "amazonaws.com" ||
          host === "example.invalid" ||
          host.endsWith(".example.invalid");
        assert.ok(allowed, `${name} contains hostname ${host}`);
      }
    }
  });
});
