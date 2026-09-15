/**
 * Compare every branch family and every `base_os` with the Python renderer.
 *
 * Two gates, from one table of families:
 *
 * 1. `expected-digests.json` holds the SHA-256 of the bytes Jinja2 3.1.6 produced for each
 *    template in each family. That comparison needs nothing outside this tree, so it runs
 *    everywhere and is what stops a rewrite change from going unnoticed.
 * 2. The real Python renderer runs over the same families and the bytes are compared directly.
 *    It runs on the python3 on PATH when that interpreter can import Jinja2, and over SSH on
 *    `JINJA_BRANCH_ORACLE_HOST` when it cannot. On that host the interpreter is taken from
 *    `JINJA_BRANCH_ORACLE_VENV`, defaulting to `$HOME/venv`. `JINJA_BRANCH_RECORD=1` rewrites the
 *    digest file from that Python output, which is how the committed digests were produced.
 *
 * One of the two oracle transports is required unless IDEACTL_PUBLIC_CHECKOUT=1 declares
 * a public checkout without captured prerequisites.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join, relative } from "node:path";
import { describe, it } from "node:test";

import { jinjaEnv, renderTemplate } from "../../src/config/jinja.ts";
import { BRANCH_FAMILIES } from "./jinja-branch-context-table.ts";
import type { BranchFamily, BranchFlags } from "./jinja-branch-context-table.ts";
import { optionalService, requireCapture, requireFixtures } from "../support/fixtures.ts";

const BOOTSTRAP_SOURCE = join(import.meta.dirname, "..", "..", "..", "idea-bootstrap");
const CONFIG_TEMPLATE_SOURCE = join(
  import.meta.dirname,
  "..",
  "..",
  "resources",
  "config",
  "templates",
);
const PYTHON_ORACLE = join(import.meta.dirname, "jinja-branch-oracle.py");
const DIGESTS = join(import.meta.dirname, "expected-digests.json");
const ORACLE_HOST = process.env.JINJA_BRANCH_ORACLE_HOST;
const ORACLE_IDENTITY = join(homedir(), ".ssh", "id_ed25519");
requireCapture(
  [BOOTSTRAP_SOURCE, CONFIG_TEMPLATE_SOURCE, PYTHON_ORACLE],
  "Restore the config template and bootstrap source trees before running this test",
);

interface OracleText {
  readonly text: string;
}

interface OracleError {
  readonly error: string;
}

type OracleResult = OracleText | OracleError;

interface DefaultOptions {
  readonly default: unknown;
}

/** Identify a template keyword-argument object that supplies `default`. */
function hasDefault(value: unknown): value is DefaultOptions {
  return typeof value === "object" && value !== null && !Array.isArray(value) && Object.hasOwn(value, "default");
}

/** Identify a JSON object before reading a property from it. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Return a positional or keyword default passed to a template config getter. */
function defaultArgument(args: readonly unknown[]): unknown {
  for (const argument of args) {
    if (hasDefault(argument)) {
      return argument.default;
    }
  }
  return undefined;
}

/** Build the full storage map so each provider and scope body has concrete data. */
function sharedStorage(): Record<string, unknown> {
  return {
    apps: {
      provider: "efs",
      mount_dir: "/apps",
      mount_options: "defaults",
      efs: { dns: "fs-apps.example.invalid" },
    },
    scratch: {
      provider: "fsx_lustre",
      mount_dir: "/fsx",
      mount_options: "flock",
      fsx_lustre: { dns: "fsx.example.invalid", mount_name: "fsx" },
    },
  };
}

/** Build the matching JavaScript object facade for the selected branch flags. */
function templateContext(flags: BranchFlags, baseOs: string): object {
  const strings: Record<string, string> = {
    "cluster.cluster_name": "sample-cluster",
    "cluster.cluster_s3_bucket": "sample-bucket",
    "cluster.home_dir": "/apps/sample-cluster",
    "cluster.aws.region": "us-east-2",
    "cluster.aws.account_id": "123456789012",
    "cluster.aws.dns_suffix": "amazonaws.com",
    "directoryservice.provider": flags.directory ? "activedirectory" : "openldap",
    "directoryservice.ad_short_name": "EXAMPLE",
    "directoryservice.hostname": "directory.example.invalid",
    "directoryservice.ldap_base": "dc=example,dc=invalid",
    "directoryservice.name": "example",
    "scheduler.provider": "openpbs",
    "virtual-desktop-controller.events_sqs_queue_url": "https://example.invalid/queue",
    "virtual-desktop-controller.dcv_broker.gateway_communication_port": "8445",
  };
  const config = {
    get_string(key: string, ...args: unknown[]): unknown {
      return strings[key] ?? defaultArgument(args) ?? "configured";
    },
    get_bool(_key: string, ...args: unknown[]): unknown {
      return defaultArgument(args) ?? false;
    },
    get_list(_key: string, ...args: unknown[]): unknown {
      return defaultArgument(args) ?? [];
    },
    get_int(_key: string, ...args: unknown[]): unknown {
      return defaultArgument(args) ?? 1;
    },
    get_config(key: string, ...args: unknown[]): unknown {
      return key === "shared-storage" ? sharedStorage() : defaultArgument(args) ?? {};
    },
    get_cluster_internal_endpoint(): string {
      return "https://example.invalid";
    },
    get_cluster_external_endpoint(): string {
      return "https://example.invalid";
    },
  };
  return {
    aws_region: "us-east-2",
    base_os: baseOs,
    module_name: "virtual-desktop-controller",
    module_id: "vdc",
    module_set: "default",
    module_version: "26.09.0",
    cluster_s3_bucket: "sample-bucket",
    cluster_name: "sample-cluster",
    cluster_home_dir: "/apps/sample-cluster",
    app_deploy_dir: "/opt/idea/app",
    https_proxy: "",
    no_proxy: "",
    config,
    vars: {
      idea_session_id: "sample-session",
      session_owner: "sample-user",
      dcv_host_ready_message: "sample-ready",
      controller_package_uri: "s3://sample-bucket/release.tar.gz",
      app_package_uri: "s3://sample-bucket/release.tar.gz",
      ami_dir: "/apps/sample-cluster/ami",
      ami_name: "sample-ami",
      // Empty Python mappings are false. Nunjucks needs undefined for the same branch result.
      bedrock_env: undefined,
      bedrock_model_messages: [],
      enabled_drivers: flags.fsx ? ["fsx_lustre"] : [],
      session: { type: "console" },
      job: {
        job_name: "sample-job",
        job_id: "sample-job-id",
        job_uid: "1000",
        job_group: "sample-group",
        owner: "sample-owner",
        owner_email: "sample-owner@example.invalid",
        project: "sample-project",
        queue: "normal",
        scaling_mode: "single_job",
        params: {
          fsx_lustre: {
            enabled: flags.fsx,
            existing_fsx: "fsx.example.invalid",
          },
          enable_efa_support: false,
          enable_ht_support: false,
          scratch_storage_size: {
            value: 0,
            int_val(): number {
              return 0;
            },
          },
        },
        is_persistent_capacity(): boolean {
          return false;
        },
        is_shared_capacity(): boolean {
          return false;
        },
        get_compute_stack(): string {
          return "sample-compute-stack";
        },
      },
      job_directory: "/apps/sample-cluster/jobs/sample-job",
    },
    utils: {
      to_json(value: unknown): string {
        return JSON.stringify(value);
      },
      to_yaml(value: unknown): string {
        return `${JSON.stringify(value)}\n`;
      },
      generate_password(): string {
        return "sample-password";
      },
      short_uuid(): string {
        return "sample-uuid";
      },
    },
    get_cloudwatch_agent_config(): null {
      return null;
    },
    get_custom_aws_tags(): readonly unknown[] {
      return [];
    },
    has_storage_provider(provider: string): boolean {
      return flags.fsx && (provider === "fsx_lustre" || provider === "fsx_cache");
    },
    is_metrics_provider_prometheus(): boolean {
      return flags.metrics;
    },
    is_prometheus_exporter_enabled(): boolean {
      return flags.metrics;
    },
    get_prometheus_config(): Record<string, unknown> | null {
      return flags.metrics ? { global: { scrape_interval: "15s" } } : null;
    },
    is_gpu_instance_type(): boolean {
      return flags.gpu;
    },
    is_nvidia_gpu(): boolean {
      return flags.gpu;
    },
    job_has_param(): boolean {
      return false;
    },
    fail_on_missing_gpu_driver(): boolean {
      return false;
    },
    get_nvidia_gpu_driver_version(): string {
      return "555.42.02";
    },
    eval_shared_storage_scope(): boolean {
      return flags.storageScope;
    },
  };
}

/** Select the local source directory that owns a family of templates. */
function sourceDirectory(family: BranchFamily): string {
  return family.source === "bootstrap" ? BOOTSTRAP_SOURCE : CONFIG_TEMPLATE_SOURCE;
}

/** Build the top-level namespace the selected template tree expects. */
function renderContext(family: BranchFamily): object {
  if (family.source === "config") {
    return {
      aws_region: "us-east-2",
      cluster_name: "sample-cluster",
      metrics_provider: family.metricsProvider,
    };
  }
  return { context: templateContext(family.flags, family.baseOs ?? "rhel9") };
}

/** Read every source template so the oracle renders the local source revision. */
function templateSources(directory: string, root = directory, includeYaml = root === CONFIG_TEMPLATE_SOURCE): Record<string, string> {
  const sources: Record<string, string> = {};
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      Object.assign(sources, templateSources(path, root, includeYaml));
    } else if (entry.isFile() && (entry.name.endsWith(".jinja2") || (includeYaml && entry.name.endsWith(".yml")))) {
      sources[relative(root, path)] = readFileSync(path, "utf8");
    }
  }
  return sources;
}

/** Escape a command as one POSIX shell argument. */
function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

/** Return the first differing rendered line for an actionable failure. */
function firstDifference(actual: string, expected: string): string {
  const actualLines = actual.split("\n");
  const expectedLines = expected.split("\n");
  for (let index = 0; index < Math.max(actualLines.length, expectedLines.length); index += 1) {
    if (actualLines[index] !== expectedLines[index]) {
      return `line ${index + 1}: ${JSON.stringify(actualLines[index])} | ${JSON.stringify(expectedLines[index])}`;
    }
  }
  return "trailing bytes differ";
}

/** Confirm the remote response has one text or error result for every template. */
function parseOracleResponse(output: string, templates: readonly string[]): Map<string, OracleResult> {
  const parsed: unknown = JSON.parse(output);
  assert.ok(isRecord(parsed), "oracle did not return an object");
  const response = new Map<string, OracleResult>();
  for (const name of templates) {
    const candidate: unknown = parsed[name];
    assert.ok(isRecord(candidate), `${name}: oracle result is invalid`);
    if (typeof candidate.text === "string") {
      response.set(name, { text: candidate.text });
      continue;
    }
    assert.ok(typeof candidate.error === "string", `${name}: oracle result has no text or error`);
    response.set(name, { error: candidate.error });
  }
  return response;
}

/** The one render request both oracle transports send. */
function oracleRequest(family: BranchFamily): string {
  return JSON.stringify({
    flags: family.flags,
    baseOs: family.baseOs ?? null,
    sourceKind: family.source,
    metricsProvider: family.metricsProvider,
    sources: templateSources(sourceDirectory(family)),
    templates: family.templates,
  });
}

/** Run the real Python renderer with the python3 on this machine. */
function runLocalOracle(family: BranchFamily): Map<string, OracleResult> {
  const scratch = mkdtempSync(join(tmpdir(), "jinja-branches-"));
  try {
    const output = execFileSync("python3", [PYTHON_ORACLE], {
      encoding: "utf8",
      env: { ...process.env, JINJA_BRANCH_SCRATCH: scratch },
      input: oracleRequest(family),
      maxBuffer: 64 * 1024 * 1024,
    });
    return parseOracleResponse(output, family.templates);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

/** Run the real Python renderer in a transient remote scratch directory. */
function runRemoteOracle(family: BranchFamily): Map<string, OracleResult> {
  assert.ok(ORACLE_HOST !== undefined && ORACLE_HOST !== "", "JINJA_BRANCH_ORACLE_HOST is required");
  assert.ok(existsSync(ORACLE_IDENTITY), `SSH identity is absent: ${ORACLE_IDENTITY}`);
  const oracleProgram = readFileSync(PYTHON_ORACLE).toString("base64");
  const command = [
    "set -euo pipefail",
    'mkdir -p "$HOME/tmp-jinja-branches"',
    'scratch="$(mktemp -d "$HOME/tmp-jinja-branches/render.XXXXXX")"',
    'trap \'rm -rf "$scratch"\' EXIT',
    'source "${JINJA_BRANCH_ORACLE_VENV:-$HOME/venv}/bin/activate"',
    'export JINJA_BRANCH_SCRATCH="$scratch"',
    `python3 -c "$(printf %s '${oracleProgram}' | base64 -d)"`,
  ].join("; ");
  const output = execFileSync(
    "ssh",
    [
      "-o",
      "BatchMode=yes",
      "-o",
      "ConnectTimeout=20",
      "-i",
      ORACLE_IDENTITY,
      ORACLE_HOST,
      `bash -lc ${shellQuote(command)}`,
    ],
    {
      encoding: "utf8",
      input: oracleRequest(family),
      maxBuffer: 64 * 1024 * 1024,
    },
  );
  return parseOracleResponse(output, family.templates);
}

/** Whether the python3 on PATH can import Jinja2, which is all the oracle program needs. */
function localOracleAvailable(): boolean {
  try {
    execFileSync("python3", ["-c", "import jinja2"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

const LOCAL_ORACLE = localOracleAvailable();
const REMOTE_ORACLE = ORACLE_HOST !== undefined && ORACLE_HOST !== "" && existsSync(ORACLE_IDENTITY);

// The oracle program reads one JSON request on stdin and needs nothing but Jinja2, so the
// local interpreter is preferred and the SSH transport is the fallback for a machine that
// cannot install it.
const runOracle = LOCAL_ORACLE ? runLocalOracle : runRemoteOracle;

const oracleReachable = optionalService(
  LOCAL_ORACLE || REMOTE_ORACLE,
  "Python Jinja2 oracle",
  "python3 -m pip install --user 'jinja2==3.1.6', or set JINJA_BRANCH_ORACLE_HOST and provide ~/.ssh/id_ed25519",
);

function digest(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

type DigestTable = Record<string, Record<string, string>>;

function readDigests(): DigestTable {
  return JSON.parse(readFileSync(DIGESTS, "utf8")) as DigestTable;
}

/** Rewrite one family's digests from the Python output that produced them. */
function recordDigests(family: BranchFamily, results: Map<string, OracleResult>): void {
  const table = existsSync(DIGESTS) ? readDigests() : {};
  const row: Record<string, string> = {};
  for (const name of family.templates) {
    const result = results.get(name);
    if (result !== undefined && "text" in result) row[name] = digest(result.text);
  }
  table[family.name] = row;
  const ordered = Object.fromEntries(Object.keys(table).sort().map((key) => [key, table[key]]));
  writeFileSync(DIGESTS, `${JSON.stringify(ordered, null, 2)}\n`);
}

// While the digests are being rewritten from Python output there is nothing to compare against.
const recording = process.env.JINJA_BRANCH_RECORD === "1";

describe("every branch family matches the recorded Python digests", { skip: recording }, () => {
  requireFixtures([DIGESTS], "JINJA_BRANCH_ORACLE_HOST=<host> JINJA_BRANCH_RECORD=1 node --test 'test/config/jinja-branch-parity.test.ts'");
  const expected = readDigests();
  let rendered = 0;
  for (const family of BRANCH_FAMILIES) {
    it(`${family.name} renders the recorded bytes`, () => {
      const environment = jinjaEnv(sourceDirectory(family));
      const row = expected[family.name];
      assert.ok(row !== undefined, `${family.name}: no recorded digests`);
      const differences: string[] = [];
      for (const name of family.templates) {
        assert.ok(row[name] !== undefined, `${family.name}/${name}: no recorded digest`);
        let actual: string;
        try {
          actual = renderTemplate(environment, name, renderContext(family));
        } catch (error) {
          differences.push(`${name}: raised ${String(error).split("\n")[0]}`);
          continue;
        }
        rendered += 1;
        if (digest(actual) !== row[name]) differences.push(`${name}: digest ${digest(actual)} is not the recorded ${row[name]}`);
      }
      assert.deepEqual(differences, []);
    });
  }
  it("compared every template in every family", () => {
    const total = BRANCH_FAMILIES.reduce((sum, family) => sum + family.templates.length, 0);
    console.log(`jinja branches: ${rendered} of ${total} renders compared against recorded Jinja2 3.1.6 digests`);
    assert.equal(rendered, total);
  });
});

describe("bootstrap branches match the Python renderer", () => {
  for (const family of BRANCH_FAMILIES) {
    it(`${family.name} matches the Python renderer`, { skip: !oracleReachable }, () => {
      const environment = jinjaEnv(sourceDirectory(family));
      const expected = runOracle(family);
      if (recording) recordDigests(family, expected);
      const differences: string[] = [];
      for (const name of family.templates) {
        const result = expected.get(name);
        assert.ok(result !== undefined, `${name}: oracle result is missing`);
        if ("error" in result) {
          differences.push(`${name}: Python raised ${result.error}`);
          continue;
        }
        let actual: string;
        try {
          actual = renderTemplate(environment, name, renderContext(family));
        } catch (error) {
          differences.push(`${name}: TypeScript raised ${String(error).split("\n")[0]}`);
          continue;
        }
        if (actual !== result.text) differences.push(`${name}: ${firstDifference(actual, result.text)}`);
      }
      console.log(`jinja branches ${family.name}: rendered ${family.templates.length}; differences ${differences.length}`);
      assert.deepEqual(differences, []);
    });
  }
});
