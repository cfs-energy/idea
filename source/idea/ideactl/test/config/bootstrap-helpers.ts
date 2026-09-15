/**
 * Shared builders for host-bootstrap coverage. The package builder is exercised
 * against the real `idea-bootstrap` tree and unpacked with the same `tar` flags
 * the Linux user-data script uses.
 */

import { execFileSync } from "node:child_process";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { BootstrapPackageBuilder } from "../../src/cli/bootstrap-package.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";
import { requireCapture } from "../support/fixtures.ts";
import { ideaVersion } from "../../src/version.ts";

/** The `idea-bootstrap` tree, three levels up from this package. */
export const BOOTSTRAP_SOURCE = join(import.meta.dirname, "..", "..", "..", "idea-bootstrap");

requireCapture(
  [BOOTSTRAP_SOURCE],
  "Restore the bootstrap source tree before running host bootstrap tests",
);

export interface ComponentCase {
  /** Directory name under the bootstrap source tree. */
  name: string;
  /** Value passed as `baseOs` so Windows packages skip `common`. */
  baseOs: string;
  /** Path of the first script the user-data install command runs, relative to the extract root. */
  entryRelative: string;
  /** User-data install command after `cd /root/bootstrap/latest` or the Windows extract directory. */
  installCommand: string;
}

interface ConfigCall {
  key: string;
  fallback: unknown;
  required: boolean;
}

/** Every component directory the builder can copy, excluding `_templates`. */
export function listComponents(): string[] {
  return readdirSync(BOOTSTRAP_SOURCE, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name !== "_templates")
    .map((entry) => entry.name)
    .sort();
}

/** Host roles the builder is asked to produce, including compute and desktop images. */
export function componentCases(): ComponentCase[] {
  return listComponents().map((name) => {
    const windows = name.includes("windows");
    return {
      name,
      baseOs: windows ? "windows2022" : "amazonlinux2023",
      entryRelative: windows
        ? `${name}/Install.ps1`
        : name === "common"
          ? "common/bootstrap_common.sh"
          : `${name}/setup.sh`,
      installCommand: windows
        ? `cd "${name}"`
        : `/bin/bash ${name === "common" ? "common/bootstrap_common.sh" : `${name}/setup.sh`}`,
    };
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Nunjucks keyword bags (`required=True`) arrive as `{ required, __keywords: true }`. */
function keywordArguments(args: readonly unknown[]): Record<string, unknown> {
  const last = args.at(-1);
  return isRecord(last) && last.__keywords === true ? last : {};
}

function callArgument(args: readonly unknown[], position: number, name: string): unknown {
  const keywords = keywordArguments(args);
  const positionalLength = Object.keys(keywords).length === 0 ? args.length : args.length - 1;
  return position < positionalLength ? args[position] : keywords[name];
}

/** Same `(key, default=None, required=False)` shape as `src/cli/bootstrap-context.ts`. */
function configCall(args: readonly unknown[]): ConfigCall {
  const key = callArgument(args, 0, "key");
  if (typeof key !== "string" || key.length === 0) {
    throw new Error("config key is required");
  }
  return {
    key,
    fallback: callArgument(args, 1, "default"),
    required: callArgument(args, 2, "required") === true,
  };
}

/**
 * A filled template context that lets every component render. Unknown required
 * keys still get a concrete value so the package can be unpacked and inspected.
 */
export function fullContext(): Record<string, unknown> {
  const strings: Record<string, string> = {
    "cluster.cluster_name": "sample-cluster",
    "cluster.cluster_s3_bucket": "sample-bucket",
    "cluster.home_dir": "/apps/sample-cluster",
    "cluster.aws.region": "us-east-2",
    "cluster.aws.account_id": "123456789012",
    "cluster.aws.dns_suffix": "amazonaws.com",
    "cluster.route53.private_hosted_zone_name": "cluster.example.invalid",
    "directoryservice.provider": "openldap",
    "directoryservice.hostname": "directory.example.invalid",
    "directoryservice.ldap_base": "dc=example,dc=invalid",
    "directoryservice.name": "example",
    "directoryservice.ad_short_name": "EXAMPLE",
    "scheduler.provider": "openpbs",
    "scheduler.private_dns_name": "scheduler.example.invalid",
    "scheduler.private_ip": "192.0.2.10",
    "scheduler.job_status_sqs_queue_url": "https://sqs.example.invalid/queue",
    "shared-storage.data.mount_dir": "/data",
    "shared-storage.apps.mount_dir": "/apps",
    "virtual-desktop-controller.dcv_broker.gateway_communication_port": "8445",
    "virtual-desktop-controller.dcv_broker.client_communication_port": "8443",
    "virtual-desktop-controller.dcv_broker.agent_communication_port": "8444",
    "virtual-desktop-controller.dcv_broker.session_token_validity": "30",
    "virtual-desktop-controller.dcv_session.idle_timeout": "60",
    "virtual-desktop-controller.dcv_session.idle_timeout_warning": "30",
    "virtual-desktop-controller.events_sqs_queue_url": "https://sqs.example.invalid/events",
    "global-settings.package_config.amazon_cloudwatch_agent.download_link_pattern":
      "https://example.invalid/%region%/%os%/%architecture%.%ext%",
    "global-settings.package_config.dcv.gpg_key": "https://example.invalid/dcv.gpg",
    "global-settings.package_config.efa.url": "https://example.invalid/efa.tgz",
    "global-settings.package_config.efa.checksum": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "global-settings.package_config.efa.checksum_method": "sha256",
  };
  const lists: Record<string, string[]> = {};
  const config = {
    get_string(key: string, ...args: unknown[]): unknown {
      const call = configCall([key, ...args]);
      if (Object.hasOwn(strings, key)) return strings[key];
      if (
        key.includes(".url") ||
        key.includes("download_link") ||
        key.endsWith(".sha256sum") ||
        key.includes("gpg_key") ||
        key.includes("s3_bucket")
      ) {
        return key.includes("sha256") || key.includes("checksum")
          ? "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
          : "https://example.invalid/pkg";
      }
      if (key.includes("mount_dir") || key.endsWith(".mount_point")) return "/data";
      return call.fallback ?? "configured";
    },
    get_bool(_key: string, ...args: unknown[]): unknown {
      return configCall([_key, ...args]).fallback ?? false;
    },
    get_list(key: string, ...args: unknown[]): unknown {
      const call = configCall([key, ...args]);
      if (Object.hasOwn(lists, key)) return lists[key];
      if (call.required) return ["coreutils"];
      return call.fallback ?? [];
    },
    get_int(_key: string, ...args: unknown[]): unknown {
      return configCall([_key, ...args]).fallback ?? 1;
    },
    get_config(_key: string, ...args: unknown[]): unknown {
      return configCall([_key, ...args]).fallback ?? {};
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
    base_os: "amazonlinux2023",
    instance_type: "c5.large",
    module_name: "scheduler",
    module_id: "scheduler",
    module_set: "default",
    module_version: ideaVersion(),
    cluster_s3_bucket: "sample-bucket",
    cluster_name: "sample-cluster",
    cluster_home_dir: "/apps/sample-cluster",
    app_deploy_dir: "/opt/idea/app",
    https_proxy: "",
    no_proxy: "",
    default_system_user: "ec2-user",
    config,
    vars: {
      idea_session_id: "sample-session",
      session_owner: "sample-user",
      dcv_host_ready_message: "sample-ready",
      controller_package_uri: `s3://sample-bucket/idea/releases/idea-virtual-desktop-controller-${ideaVersion()}.tar.gz`,
      app_package_uri: `s3://sample-bucket/idea/releases/idea-scheduler-${ideaVersion()}.tar.gz`,
      dcv_connection_gateway_package_uri:
        `s3://sample-bucket/idea/releases/idea-dcv-connection-gateway-${ideaVersion()}.tar.gz`,
      ami_dir: "/apps/sample-cluster/ami",
      ami_name: "sample-ami",
      enabled_drivers: [],
      bedrock_env: {},
      bedrock_model_messages: [],
      session: { type: "virtual" },
      job_directory: "/apps/sample-cluster/jobs/sample-job",
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
          fsx_lustre: { enabled: false, existing_fsx: "" },
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
    get_custom_aws_tags(): unknown[] {
      return [];
    },
    has_storage_provider(): boolean {
      return false;
    },
    is_metrics_provider_prometheus(): boolean {
      return false;
    },
    is_prometheus_exporter_enabled(): boolean {
      return false;
    },
    job_has_param(): boolean {
      return false;
    },
    job_has_storage_provider(): boolean {
      return false;
    },
    is_gpu_instance_type(): boolean {
      return false;
    },
    is_nvidia_gpu(): boolean {
      return false;
    },
    fail_on_missing_gpu_driver(): boolean {
      return false;
    },
    eval_shared_storage_scope(): boolean {
      return false;
    },
  };
}

/**
 * A context that throws on `required=True` misses and otherwise leaves values
 * undefined, so missing substitution is visible in the rendered bytes.
 */
export function sparseContext(options: {
  strings?: Record<string, string>;
  vars?: Record<string, unknown>;
  fields?: Record<string, unknown>;
}): Record<string, unknown> {
  const strings = options.strings ?? {};
  const config = {
    get_string(key: string, ...args: unknown[]): unknown {
      const call = configCall([key, ...args]);
      if (Object.hasOwn(strings, key)) return strings[key];
      if (call.required) throw new Error(`missing required config: ${key}`);
      return call.fallback;
    },
    get_bool(_key: string, ...args: unknown[]): unknown {
      const call = configCall([_key, ...args]);
      if (call.required) throw new Error("missing required bool");
      return call.fallback;
    },
    get_list(_key: string, ...args: unknown[]): unknown {
      const call = configCall([_key, ...args]);
      if (call.required) throw new Error("missing required list");
      return call.fallback;
    },
    get_int(_key: string, ...args: unknown[]): unknown {
      const call = configCall([_key, ...args]);
      if (call.required) throw new Error("missing required int");
      return call.fallback;
    },
    get_config(_key: string, ...args: unknown[]): unknown {
      return configCall([_key, ...args]).fallback ?? {};
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
    base_os: "amazonlinux2023",
    module_name: "scheduler",
    module_id: "scheduler",
    module_set: "default",
    module_version: ideaVersion(),
    cluster_s3_bucket: "sample-bucket",
    cluster_name: "sample-cluster",
    cluster_home_dir: "/apps/sample-cluster",
    app_deploy_dir: "/opt/idea/app",
    https_proxy: "",
    no_proxy: "",
    config,
    vars: options.vars ?? {},
    utils: {
      to_json(value: unknown): string {
        return JSON.stringify(value);
      },
    },
    get_cloudwatch_agent_config(): null {
      return null;
    },
    get_custom_aws_tags(): unknown[] {
      return [];
    },
    has_storage_provider(): boolean {
      return false;
    },
    is_metrics_provider_prometheus(): boolean {
      return false;
    },
    eval_shared_storage_scope(): boolean {
      return false;
    },
    ...options.fields,
  };
}

/**
 * Template context whose getters are the production ClusterConfig methods, so
 * a missing or empty required key behaves as a real deploy would.
 */
export function clusterConfigContext(
  entries: Record<string, unknown>,
  extra: { vars?: Record<string, unknown>; fields?: Record<string, unknown> } = {},
): Record<string, unknown> {
  const config = new ClusterConfig(
    Object.entries(entries).map(([key, value]) => ({ key, value })),
    [
      { module_id: "scheduler", name: "scheduler", type: "app" },
      { module_id: "cluster", name: "cluster", type: "stack" },
    ],
  );
  const getters = {
    get_string(...args: unknown[]): unknown {
      const call = configCall(args);
      return config.getString(call.key, call.fallback as string | undefined, { required: call.required });
    },
    get_bool(...args: unknown[]): unknown {
      const call = configCall(args);
      return config.getBool(call.key, call.fallback as boolean | undefined, { required: call.required });
    },
    get_list(...args: unknown[]): unknown {
      const call = configCall(args);
      return config.getList(call.key, call.fallback as unknown[] | undefined, { required: call.required });
    },
    get_int(...args: unknown[]): unknown {
      const call = configCall(args);
      return config.getInt(call.key, call.fallback as number | undefined, { required: call.required });
    },
    get_config(...args: unknown[]): unknown {
      const call = configCall(args);
      return config.getConfig(call.key, call.fallback as Record<string, unknown> | undefined, {
        required: call.required,
      });
    },
    get_cluster_internal_endpoint(): string {
      return "https://example.invalid";
    },
    get_cluster_external_endpoint(): string {
      return "https://example.invalid";
    },
  };
  return {
    ...sparseContext({ vars: extra.vars, fields: extra.fields }),
    config: getters,
  };
}

/** Render one component with the production builder. */
export function buildComponent(
  name: string,
  workDirectory: string,
  context: object = fullContext(),
  baseOs?: string,
): string {
  const os = baseOs ?? (name.includes("windows") ? "windows2022" : "amazonlinux2023");
  return new BootstrapPackageBuilder({
    sourceDirectory: BOOTSTRAP_SOURCE,
    targetPackageBasename: `bootstrap-${name}-hard`,
    components: [name],
    context: { ...fullContext(), ...context, base_os: os },
    tmpDir: workDirectory,
    baseOs: os,
    forceBuild: true,
  }).build();
}

/** Unpack the way Linux user data does: `tar -xvf archive -C package-dir`. */
export function unpackLikeLinux(archiveFile: string, extractRoot: string): string {
  const archiveName = archiveFile.split("/").pop() ?? archiveFile;
  const packageName = archiveName.replace(/\.tar\.gz.*/, "");
  const packageDir = join(extractRoot, packageName);
  mkdirSync(packageDir, { recursive: true });
  execFileSync("tar", ["-xvf", archiveFile, "-C", packageDir], { stdio: "pipe" });
  return packageDir;
}

/** Unpack the way Windows user data does: `Tar -xf archive` into the current directory. */
export function unpackLikeWindows(archiveFile: string, extractRoot: string): string {
  mkdirSync(extractRoot, { recursive: true });
  execFileSync("tar", ["-xf", archiveFile, "-C", extractRoot], { stdio: "pipe" });
  return extractRoot;
}

/** Collect regular files under a directory as posix-relative paths. */
export function listFiles(root: string, prefix = ""): string[] {
  const names: string[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) names.push(...listFiles(join(root, entry.name), relative));
    else names.push(relative);
  }
  return names.sort();
}

/**
 * Package-relative paths a rendered script sources or execs through `${SCRIPT_DIR}`.
 * Absolute paths such as `/etc/environment` are host files, not archive members.
 */
export function packageScriptRefs(component: string, content: string): string[] {
  const refs = new Set<string>();
  const sourceCommon = /source\s+"\$\{SCRIPT_DIR\}\/(\.\.\/common\/[^"]+)"/g;
  const bashSibling = /\/bin\/bash\s+\$\{SCRIPT_DIR\}\/([A-Za-z0-9_.-]+)/g;
  const importModule = /Import-Module\s+\.\\([A-Za-z0-9_.-]+)/g;
  let match: RegExpExecArray | null;
  while ((match = sourceCommon.exec(content)) !== null) {
    refs.add(match[1].replace(/^\.\.\//, ""));
  }
  while ((match = bashSibling.exec(content)) !== null) {
    refs.add(`${component}/${match[1]}`);
  }
  while ((match = importModule.exec(content)) !== null) {
    refs.add(`${component}/${match[1]}`);
  }
  return [...refs];
}

/** Jinja delimiters that should not survive rendering. */
export function leftoverJinja(content: string): string[] {
  const hits: string[] = [];
  const pattern = /\{\{|\{\%/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(content)) !== null) {
    hits.push(content.slice(match.index, match.index + 24).replaceAll("\n", " "));
  }
  return hits;
}

/** True when any execute bit is set. */
export function isExecutable(path: string): boolean {
  return (statSync(path).mode & 0o111) !== 0;
}

/** Create a tiny bootstrap source tree for substitution cases. */
export function writeMiniSource(
  workDirectory: string,
  files: Record<string, string>,
): string {
  const source = join(workDirectory, "source");
  for (const [relative, content] of Object.entries(files)) {
    const path = join(source, relative);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, content, "utf8");
    if (relative.endsWith(".sh") && !relative.endsWith(".jinja2")) chmodSync(path, 0o755);
  }
  return source;
}

/** Render a mini tree and return the named file from the archive extract. */
export function renderMini(
  workDirectory: string,
  files: Record<string, string>,
  context: object,
  readRelative: string,
  baseOs = "amazonlinux2023",
): string {
  const sourceDirectory = writeMiniSource(workDirectory, files);
  const components = Object.keys(files)
    .map((relative) => relative.split("/")[0])
    .filter((name, index, all) => all.indexOf(name) === index);
  const archiveFile = new BootstrapPackageBuilder({
    sourceDirectory,
    targetPackageBasename: "bootstrap-mini",
    components,
    context,
    tmpDir: workDirectory,
    baseOs,
    forceBuild: true,
  }).build();
  const extracted = unpackLikeLinux(archiveFile, join(workDirectory, "extracted"));
  return readFileSync(join(extracted, readRelative), "utf8");
}

export function withWorkdir<T>(fn: (directory: string) => T): T {
  const directory = mkdtempSync(join(tmpdir(), "bootstrap-hard-"));
  try {
    return fn(directory);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

export async function withWorkdirAsync<T>(fn: (directory: string) => Promise<T>): Promise<T> {
  const directory = mkdtempSync(join(tmpdir(), "bootstrap-hard-"));
  try {
    return await fn(directory);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}
