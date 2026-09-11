/**
 * Supplies the template context for behavior tests and the generated oracle.
 * Keep `oracle_build.py` aligned with these values.
 */

import { join } from "node:path";

/** The `idea-bootstrap` tree, three levels up from this package. */
export const BOOTSTRAP_SOURCE = join(import.meta.dirname, "..", "..", "..", "idea-bootstrap");

interface TemplateOptions {
  default?: unknown;
}

/** Jinja2 keyword arguments arrive as a trailing options object. */
function defaultArgument(args: unknown[]): unknown {
  for (const argument of args) {
    if (
      typeof argument === "object" &&
      argument !== null &&
      !Array.isArray(argument) &&
      Object.hasOwn(argument, "default")
    ) {
      return (argument as TemplateOptions).default;
    }
  }
  return undefined;
}

/**
 * Stable input values make rendering deterministic while exercising the real
 * component tree. The source templates use these method names.
 */
export function templateContext(): object {
  const values: Record<string, string> = {
    "cluster.cluster_name": "sample-cluster",
    "cluster.cluster_s3_bucket": "sample-bucket",
    "cluster.home_dir": "/apps/sample-cluster",
    "cluster.aws.region": "us-east-2",
    "virtual-desktop-controller.dcv_broker.gateway_communication_port": "8445",
  };
  const config = {
    get_string(key: string, ...args: unknown[]): unknown {
      return values[key] ?? defaultArgument(args) ?? "configured";
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
    get_config(_key: string, ...args: unknown[]): unknown {
      return defaultArgument(args) ?? {};
    },
    get_cluster_internal_endpoint(): string {
      return "https://example.invalid";
    },
  };
  return {
    aws_region: "us-east-2",
    base_os: "amazonlinux2023",
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
    vars: {
      dcv_connection_gateway_package_uri:
        "s3://sample-bucket/idea/releases/idea-dcv-connection-gateway-26.09.0.tar.gz",
    },
    config,
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
    utils: {
      to_json(value: unknown): string {
        return JSON.stringify(value);
      },
      to_yaml(value: unknown): string {
        return `${JSON.stringify(value)}\n`;
      },
    },
  };
}

export interface OracleCase {
  /** Also the generated oracle directory name. */
  basename: string;
  components: string[];
  /** Merged over `templateContext().vars`. */
  vars: Record<string, string>;
}

/** One case per host role that builds a bootstrap package. */
export const ORACLE_CASES: OracleCase[] = [
  { basename: "bootstrap-vdc-dcv-connection-gateway-deployment", components: ["dcv-connection-gateway"], vars: {} },
  { basename: "bootstrap-directoryservice-deployment", components: ["openldap-server"], vars: {} },
  {
    basename: "bootstrap-cluster-manager-deployment",
    components: ["cluster-manager"],
    vars: { app_package_uri: "s3://sample-bucket/release.tar.gz" },
  },
  {
    basename: "bootstrap-scheduler-deployment",
    components: ["scheduler"],
    vars: { app_package_uri: "s3://sample-bucket/release.tar.gz" },
  },
  { basename: "bootstrap-bastion-host-deployment", components: ["bastion-host"], vars: {} },
  {
    basename: "bootstrap-virtual-desktop-controller-deployment",
    components: ["virtual-desktop-controller"],
    vars: { controller_package_uri: "s3://sample-bucket/release.tar.gz" },
  },
  { basename: "bootstrap-dcv-broker-deployment", components: ["dcv-broker"], vars: {} },
];

export function caseContext(oracleCase: OracleCase): object {
  const context = templateContext() as Record<string, unknown>;
  context.vars = { ...(context.vars as Record<string, string>), ...oracleCase.vars };
  return context;
}
