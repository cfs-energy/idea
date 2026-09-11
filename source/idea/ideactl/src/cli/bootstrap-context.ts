/**
 * Builds the template context used by module host bootstrap packages.
 *
 * The property and method names are intentionally snake case because the bootstrap templates call
 * the same public surface as the reference implementation.
 */

import type { ClusterConfig } from "../config/cluster-config.ts";
import { GeneralException, isEmpty } from "../config/cluster-config.ts";
import { toYaml } from "../config/jinja.ts";
import { ideaVersion } from "../version.ts";
import type { BootstrapContextInput } from "./cdk-invoker.ts";

type JsonObject = Record<string, unknown>;
type NodeType = "app" | "infra";

interface LogFile {
  file_path: string;
  log_group_name: string;
  log_stream_name: string;
}

interface HostRole {
  baseOs: string;
  instanceType: string;
  metricsNamespace: string;
  nodeType: NodeType;
  enableLogging: boolean;
  logFiles: LogFile[];
  vars: JsonObject;
}

interface ConfigCall {
  key: string;
  fallback: unknown;
  required: boolean;
}

/** Returns true only for ordinary key-value objects. */
function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Reads keyword arguments emitted by the template renderer. */
function keywordArguments(args: readonly unknown[]): JsonObject {
  const last = args.at(-1);
  return isRecord(last) && last.__keywords === true ? last : {};
}

/** Reads one positional or named argument from a template call. */
function callArgument(
  args: readonly unknown[],
  position: number,
  name: string,
): unknown {
  const keywords = keywordArguments(args);
  const positionalLength = Object.keys(keywords).length === 0 ? args.length : args.length - 1;
  return position < positionalLength ? args[position] : keywords[name];
}

/** Parses the common `(key, default=None, required=False)` getter shape. */
function configCall(args: readonly unknown[]): ConfigCall {
  const key = callArgument(args, 0, "key");
  if (typeof key !== "string" || key.length === 0) {
    throw new GeneralException("config key is required");
  }
  return {
    key,
    fallback: callArgument(args, 1, "default"),
    required: callArgument(args, 2, "required") === true,
  };
}

/** Exposes the configuration methods used by the bootstrap templates. */
function configFacade(config: ClusterConfig): object {
  return {
    get_string: (...args: unknown[]) => {
      const call = configCall(args);
      return Reflect.apply(config.getString, config, [
        call.key,
        call.fallback,
        { required: call.required },
      ]);
    },
    get_bool: (...args: unknown[]) => {
      const call = configCall(args);
      return Reflect.apply(config.getBool, config, [
        call.key,
        call.fallback,
        { required: call.required },
      ]);
    },
    get_int: (...args: unknown[]) => {
      const call = configCall(args);
      return Reflect.apply(config.getInt, config, [
        call.key,
        call.fallback,
        { required: call.required },
      ]);
    },
    get_list: (...args: unknown[]) => {
      const call = configCall(args);
      return Reflect.apply(config.getList, config, [
        call.key,
        call.fallback,
        { required: call.required },
      ]);
    },
    get_config: (...args: unknown[]) => {
      const call = configCall(args);
      return Reflect.apply(config.getConfig, config, [
        call.key,
        call.fallback,
        { required: call.required },
      ]);
    },
    get_module_id: (moduleName: string) => config.moduleId(moduleName),
    is_module_enabled: (moduleName: string) => config.isModuleEnabled(moduleName),
    get_cluster_internal_endpoint: () => config.getClusterInternalEndpoint(),
    get_cluster_external_endpoint: () => config.getClusterExternalEndpoint(),
  };
}

/** Resolves the release URI that the package publisher already uploaded. */
function releaseUri(input: BootstrapContextInput, packagePrefix: string): string {
  const entry = Object.entries(input.releasePackageUris).find(([name]) =>
    name.startsWith(packagePrefix),
  );
  if (entry === undefined || isEmpty(entry[1])) {
    throw new GeneralException(
      `release package URI not found for module ${input.moduleId}: ${packagePrefix}`,
    );
  }
  return entry[1];
}

/** Creates one log-file entry in the order used by the reference model. */
function logFile(
  filePath: string,
  logGroupName: string,
  logStreamName: string,
): LogFile {
  return {
    file_path: filePath,
    log_group_name: logGroupName,
    log_stream_name: logStreamName,
  };
}

/** Resolves the role-specific inputs for one package plan. */
function hostRole(input: BootstrapContextInput): HostRole {
  const clusterName = requiredString(input.config, "cluster.cluster_name");
  const moduleLogGroup = `/${clusterName}/${input.moduleId}`;
  const moduleMetrics = `${clusterName}/${input.moduleId}`;

  if (input.moduleName === "directoryservice") {
    const name = `${moduleLogGroup}/openldap-server`;
    return {
      baseOs: input.baseOs,
      instanceType: input.instanceType,
      metricsNamespace: `${moduleMetrics}/openldap-server`,
      nodeType: "infra",
      enableLogging:
        input.config.getBool("directoryservice.cloudwatch_logs.enabled", false) ===
        true,
      logFiles: [
        logFile("/var/log/messages", name, "system_{ip_address}"),
        logFile("/var/log/syslog", name, "syslog_{ip_address}"),
        logFile("/var/log/slapd.log", name, "slapd_{ip_address}"),
      ],
      vars: {},
    };
  }

  if (input.moduleName === "cluster-manager") {
    return {
      baseOs: input.baseOs,
      instanceType: input.instanceType,
      metricsNamespace: moduleMetrics,
      nodeType: "app",
      enableLogging:
        input.config.getBool("cluster-manager.cloudwatch_logs.enabled", false) ===
        true,
      logFiles: [
        logFile("/opt/idea/app/logs/**.log", moduleLogGroup, "application_{ip_address}"),
        logFile("/var/log/messages", moduleLogGroup, "system_{ip_address}"),
        logFile("/var/log/syslog", moduleLogGroup, "syslog_{ip_address}"),
      ],
      vars: {
        app_package_uri: releaseUri(input, "idea-cluster-manager-"),
      },
    };
  }

  if (input.moduleName === "scheduler") {
    const logFiles = [
      logFile("/opt/idea/app/logs/**.log", moduleLogGroup, "application_{ip_address}"),
      logFile("/var/log/messages", moduleLogGroup, "system_{ip_address}"),
      logFile("/var/log/syslog", moduleLogGroup, "syslog_{ip_address}"),
    ];
    if (requiredString(input.config, "scheduler.provider") === "openpbs") {
      const openPbsLogGroup = `${moduleLogGroup}/openpbs`;
      logFiles.push(
        logFile(
          "/var/spool/pbs/server_logs/**.log",
          openPbsLogGroup,
          "server_logs_{ip_address}",
        ),
        logFile(
          "/var/spool/pbs/sched_logs/**.log",
          openPbsLogGroup,
          "sched_logs_{ip_address}",
        ),
        logFile(
          "/var/spool/pbs/server_priv/accounting/**.log",
          openPbsLogGroup,
          "accounting_logs_{ip_address}",
        ),
      );
    }
    return {
      baseOs: input.baseOs,
      instanceType: input.instanceType,
      metricsNamespace: moduleMetrics,
      nodeType: "app",
      enableLogging:
        input.config.getBool("scheduler.cloudwatch_logs.enabled", false) === true,
      logFiles,
      vars: {
        app_package_uri: releaseUri(input, "idea-scheduler-"),
      },
    };
  }

  if (input.moduleName === "bastion-host") {
    return {
      baseOs: input.baseOs,
      instanceType: input.instanceType,
      metricsNamespace: moduleMetrics,
      nodeType: "infra",
      enableLogging:
        input.config.getBool("bastion-host.cloudwatch_logs.enabled", false) === true,
      logFiles: [
        logFile("/var/log/messages", moduleLogGroup, "system_{ip_address}"),
        logFile("/var/log/syslog", moduleLogGroup, "syslog_{ip_address}"),
        logFile("/var/log/secure", moduleLogGroup, "secure_{ip_address}"),
        logFile("/var/log/auth.log", moduleLogGroup, "auth_{ip_address}"),
      ],
      vars: {},
    };
  }

  if (input.moduleName !== "virtual-desktop-controller") {
    throw new GeneralException(
      `bootstrap context is not defined for module: ${input.moduleName}`,
    );
  }

  const vdcLogging =
    input.config.getBool(
      "virtual-desktop-controller.cloudwatch_logs.enabled",
      false,
    ) === true;
  if (input.plan.contextParameter === "controller_bootstrap_package_uri") {
    const name = `${moduleLogGroup}/controller`;
    return {
      baseOs: input.baseOs,
      instanceType: input.instanceType,
      metricsNamespace: `${moduleMetrics}/controller`,
      nodeType: "app",
      enableLogging: vdcLogging,
      logFiles: [
        logFile("/opt/idea/app/logs/**.log", name, "application_{ip_address}"),
        logFile("/var/log/messages", name, "system_{ip_address}"),
        logFile("/var/log/syslog", name, "syslog_{ip_address}"),
      ],
      vars: {
        controller_package_uri: releaseUri(
          input,
          "idea-virtual-desktop-controller-",
        ),
      },
    };
  }

  if (input.plan.contextParameter === "dcv_broker_bootstrap_package_uri") {
    const name = `${moduleLogGroup}/dcv-broker`;
    return {
      baseOs: requiredString(
        input.config,
        `${input.moduleId}.dcv_broker.autoscaling.base_os`,
      ),
      instanceType: requiredString(
        input.config,
        `${input.moduleId}.dcv_broker.autoscaling.instance_type`,
      ),
      metricsNamespace: `${moduleMetrics}/dcv-broker`,
      nodeType: "infra",
      enableLogging: vdcLogging,
      logFiles: [
        logFile(
          "/var/log/dcv-session-manager-broker/**.log",
          name,
          "dcv-session-manager-broker_{ip_address}",
        ),
        logFile("/var/log/messages", name, "system_{ip_address}"),
        logFile("/var/log/syslog", name, "syslog_{ip_address}"),
      ],
      vars: {},
    };
  }

  if (input.plan.contextParameter === "dcv_connection_gateway_package_uri") {
    const name = `${moduleLogGroup}/dcv-connection-gateway`;
    return {
      baseOs: requiredString(
        input.config,
        `${input.moduleId}.dcv_connection_gateway.autoscaling.base_os`,
      ),
      instanceType: requiredString(
        input.config,
        `${input.moduleId}.dcv_connection_gateway.autoscaling.instance_type`,
      ),
      metricsNamespace: `${moduleMetrics}/dcv-connection-gateway`,
      nodeType: "infra",
      enableLogging: vdcLogging,
      logFiles: [
        logFile(
          "/var/log/dcv-connection-gateway/**.log",
          name,
          "dcv-connection-gateway_{ip_address}",
        ),
        logFile("/var/log/messages", name, "system_{ip_address}"),
        logFile("/var/log/syslog", name, "syslog_{ip_address}"),
      ],
      vars: {
        dcv_connection_gateway_package_uri: releaseUri(
          input,
          "idea-dcv-connection-gateway-",
        ),
      },
    };
  }

  throw new GeneralException(
    `bootstrap context parameter is not defined for module ${input.moduleId}: ${input.plan.contextParameter}`,
  );
}

/** Produces the default Linux metric sections emitted for module hosts. */
function defaultCloudWatchMetrics(
  interval: number,
  baseOs: string,
  nvidiaGpu: boolean,
): JsonObject {
  const rootDevice = baseOs === "amazonlinux2023" ? "/dev/xvda" : "/dev/sda1";
  const metrics: JsonObject = {
    cpu: {
      resources: ["*"],
      totalcpu: true,
      metrics_collection_interval: interval,
      measurement: [
        "time_active",
        "time_idle",
        "time_iowait",
        "time_system",
        "time_user",
        "usage_active",
        "usage_idle",
        "usage_iowait",
        "usage_system",
        "usage_user",
      ],
    },
    disk: {
      metrics_collection_interval: interval,
      resources: [rootDevice],
      measurement: [
        "free",
        "total",
        "used",
        "used_percent",
        "inodes_free",
        "inodes_used",
        "inodes_total",
      ],
      drop_device: true,
    },
    diskio: {
      metrics_collection_interval: interval,
      resources: [rootDevice],
      measurement: [
        "reads",
        "writes",
        "read_bytes",
        "write_bytes",
        "read_time",
        "write_time",
        "io_time",
        "iops_in_progress",
      ],
    },
    swap: {
      metrics_collection_interval: interval,
      measurement: ["free", "used", "used_percent"],
    },
    mem: {
      metrics_collection_interval: interval,
      measurement: [
        "active",
        "available",
        "available_percent",
        "buffered",
        "cached",
        "free",
        "inactive",
        "total",
        "used",
        "used_percent",
      ],
    },
    net: {
      metrics_collection_interval: interval,
      resources: ["*"],
      measurement: [
        "bytes_sent",
        "bytes_recv",
        "drop_in",
        "drop_out",
        "err_in",
        "err_out",
        "packets_sent",
        "packets_recv",
      ],
    },
    netstat: {
      metrics_collection_interval: interval,
      measurement: [
        "tcp_close",
        "tcp_close_wait",
        "tcp_closing",
        "tcp_established",
        "tcp_fin_wait1",
        "tcp_fin_wait2",
        "tcp_last_ack",
        "tcp_listen",
        "tcp_none",
        "tcp_syn_sent",
        "tcp_syn_recv",
        "tcp_time_wait",
        "udp_socket",
      ],
    },
    processes: {
      metrics_collection_interval: interval,
      measurement: [
        "blocked",
        "dead",
        "idle",
        "paging",
        "running",
        "sleeping",
        "stopped",
        "total",
        "total_threads",
        "wait",
        "zombies",
      ],
    },
  };
  if (nvidiaGpu) {
    metrics.nvidia_gpu = {
      metrics_collection_interval: interval,
      measurement: [
        "utilization_gpu",
        "temperature_gpu",
        "power_draw",
        "utilization_memory",
        "memory_total",
        "memory_used",
        "memory_free",
        "pcie_link_gen_current",
        "pcie_link_width_current",
        "encoder_stats_session_count",
        "encoder_stats_average_fps",
        "encoder_stats_average_latency",
        "clocks_current_graphics",
        "clocks_current_sm",
        "clocks_current_memory",
        "clocks_current_video",
      ],
    };
  }
  return metrics;
}

/** Builds the agent configuration attached to every host bootstrap context. */
function cloudWatchAgentConfig(
  config: ClusterConfig,
  role: HostRole,
  moduleId: string,
  nvidiaGpu: boolean,
): JsonObject {
  const metricsEnabled = config.getString("metrics.provider") === "cloudwatch";
  const logsEnabled =
    config.getBool("cluster.cloudwatch_logs.enabled", false) === true &&
    role.enableLogging;
  const metricsInterval = config.getInt(
    "metrics.cloudwatch.metrics_collection_interval",
    60,
  );
  const agent: JsonObject = {};
  if (metricsEnabled && metricsInterval !== 0) {
    agent.metrics_collection_interval = metricsInterval;
  }
  agent.region = requiredString(config, "cluster.aws.region");
  agent.logfile =
    "/opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log";
  agent.debug = false;
  agent.run_as_user = "root";

  const result: JsonObject = { agent };
  const useEndpoints =
    config.getBool("cluster.network.use_vpc_endpoints", false) === true;
  if (logsEnabled) {
    const logs: JsonObject = {};
    if (
      useEndpoints &&
      config.getBool(
        "cluster.network.vpc_interface_endpoints.logs.enabled",
        false,
      ) === true
    ) {
      logs.endpoint_override = requiredString(
        config,
        "cluster.network.vpc_interface_endpoints.logs.endpoint_url",
      ).replaceAll("https://", "");
    }
    logs.log_stream_name = `${moduleId}_default_{ip_address}`;
    logs.force_flush_interval =
      config.getInt("cluster.cloudwatch_logs.force_flush_interval", 5) || 5;
    const retention = config.getInt(
      "cluster.cloudwatch_logs.retention_in_days",
      90,
    );
    logs.logs_collected = {
      files: {
        collect_list: role.logFiles.map((file) => ({
          ...file,
          retention_in_days: retention,
        })),
      },
    };
    result.logs = logs;
  }

  if (metricsEnabled) {
    const metrics: JsonObject = {
      namespace: role.metricsNamespace,
    };
    if (
      useEndpoints &&
      config.getBool(
        "cluster.network.vpc_interface_endpoints.monitoring.enabled",
        false,
      ) === true
    ) {
      metrics.endpoint_override = requiredString(
        config,
        "cluster.network.vpc_interface_endpoints.monitoring.endpoint_url",
      ).replaceAll("https://", "");
    }
    metrics.force_flush_interval =
      config.getInt("metrics.cloudwatch.force_flush_interval", 60) || 60;
    metrics.metrics_collected = defaultCloudWatchMetrics(
      metricsInterval || 60,
      role.baseOs,
      nvidiaGpu,
    );
    result.metrics = metrics;
  }
  return result;
}

/** Reads a required integer without weakening its return type. */
function requiredInt(config: ClusterConfig, key: string): number {
  const value = config.getInt(key, Number.NaN, { required: true });
  if (Number.isNaN(value)) {
    throw new GeneralException(`${key} is required`);
  }
  return value;
}

/** Reads a required non-empty string without weakening its return type. */
function requiredString(config: ClusterConfig, key: string): string {
  const value = config.getString(key, "", { required: true });
  if (isEmpty(value)) {
    throw new GeneralException(`${key} is required`);
  }
  return value;
}

/** Builds the Prometheus configuration attached for either supported provider. */
function prometheusConfig(
  config: ClusterConfig,
  role: HostRole,
  moduleId: string,
): JsonObject | undefined {
  const provider = config.getString("metrics.provider");
  if (provider !== "prometheus" && provider !== "amazon_managed_prometheus") {
    return undefined;
  }

  const externalLabels: JsonObject = {
    ...(config.getConfig("metrics.prometheus.external_labels") ?? {}),
  };
  const namespace = role.metricsNamespace.split("/");
  externalLabels.cluster_name = namespace[0];
  externalLabels.module_id = namespace[1];
  if (namespace[2] !== undefined) externalLabels.component = namespace[2];

  const configuredRemoteWrite = config.getConfig(
    "metrics.prometheus.remote_write",
    undefined,
    { required: true },
  );
  if (configuredRemoteWrite === undefined) {
    throw new GeneralException("metrics.prometheus.remote_write is required");
  }
  const remoteWrite = { ...configuredRemoteWrite };
  if (provider === "amazon_managed_prometheus") {
    remoteWrite.sigv4 = {
      region: requiredString(config, "cluster.aws.region"),
    };
  }

  const scrapeConfigs: JsonObject[] = [
    {
      job_name: "node_exporter",
      static_configs: [{ targets: ["localhost:9100"] }],
    },
  ];
  if (role.nodeType === "app") {
    const app: JsonObject = {
      job_name: "app_exporter",
      metrics_path: `${requiredString(
        config,
        `${moduleId}.server.api_context_path`,
      )}/metrics`,
      scheme: "http",
      authorization: {
        type: "Bearer",
        credentials_file: "/root/metrics_api_token.txt",
      },
      static_configs: [
        {
          targets: [
            `localhost:${requiredInt(
              config,
              `${moduleId}.server.port`,
            )}`,
          ],
        },
      ],
    };
    if (
      config.getBool(
        `${moduleId}.server.enable_tls`,
        false,
      ) === true
    ) {
      app.scheme = "https";
      app.tls_config = { insecure_skip_verify: true };
    }
    scrapeConfigs.push(app);
  }

  return {
    global: {
      scrape_interval: config.getString(
        "metrics.prometheus.scrape_interval",
        "60s",
      ),
      scrape_timeout: config.getString(
        "metrics.prometheus.scrape_timeout",
        "10s",
      ),
      external_labels: externalLabels,
    },
    remote_write: [remoteWrite],
    scrape_configs: scrapeConfigs,
  };
}

/** Converts the configured `Key=...,Value=...` strings to API tag objects. */
function customTags(config: ClusterConfig): Array<{ Key: string; Value: string }> {
  const values: Record<string, string> = {};
  for (const configured of config.getList<unknown>(
    "global-settings.custom_tags",
    [],
  )) {
    if (typeof configured !== "string") {
      throw new GeneralException("global-settings.custom_tags entries must be strings");
    }
    const separator = configured.indexOf(",");
    if (separator < 0) {
      throw new GeneralException(`invalid custom tag: ${configured}`);
    }
    const keyToken = configured.slice(0, separator);
    const valueToken = configured.slice(separator + 1);
    const key = keyToken.split("Key=")[1]?.trim();
    const value = valueToken.split("Value=")[1]?.trim();
    if (key === undefined || value === undefined) {
      throw new GeneralException(`invalid custom tag: ${configured}`);
    }
    if (key !== "" && value !== "") values[key] = value;
  }
  return Object.entries(values).map(([Key, Value]) => ({ Key, Value }));
}

/** Returns the login user associated with the supported host operating systems. */
function defaultSystemUser(baseOs: string): string {
  if (
    [
      "amazonlinux2023",
      "rhel8",
      "rhel9",
      "rhel10",
      "rocky8",
      "rocky9",
      "rocky10",
    ].includes(baseOs)
  ) {
    return "ec2-user";
  }
  if (["ubuntu2204", "ubuntu2404", "ubuntu2604"].includes(baseOs)) {
    return "ubuntu";
  }
  throw new GeneralException(`unknown system user name for base_os: ${baseOs}`);
}

/** Returns whether any configured shared storage uses the requested provider. */
function hasStorageProvider(config: ClusterConfig, provider: string): boolean {
  const storage = config.getConfig("shared-storage") ?? {};
  return Object.values(storage).some(
    (entry) => isRecord(entry) && entry.provider === provider,
  );
}

/** Applies the cluster and module scope rules used for module hosts. */
function sharedStorageApplies(
  moduleName: string,
  storage: unknown,
): boolean {
  if (!isRecord(storage)) return false;
  const scope = Array.isArray(storage.scope) ? storage.scope : [];
  if (scope.length === 0 || scope.includes("cluster")) return true;
  if (scope.includes("module") && scope.includes("project")) return false;
  if (scope.includes("project") && scope.includes("scheduler:queue-profile")) {
    return false;
  }
  if (scope.includes("module")) {
    const modules = Array.isArray(storage.modules) ? storage.modules : [];
    return modules.length === 0 || modules.includes(moduleName);
  }
  return false;
}

/** Returns whether the host's instance family has a configured public driver. */
function isNvidiaGpu(config: ClusterConfig, instanceType: string): boolean {
  const family = instanceType.split(".")[0];
  return Object.hasOwn(
    config.getConfig(
      "global-settings.gpu_settings.nvidia_public_driver_versions",
      {},
    ) ?? {},
    family,
  );
}

/** Creates the provider consumed by the deployment package publisher. */
export function buildBootstrapContext(input: BootstrapContextInput): object {
  for (const [name, value] of [
    ["module_name", input.moduleName],
    ["module_id", input.moduleId],
    ["module_set", input.moduleSet],
    ["base_os", input.baseOs],
    ["instance_type", input.instanceType],
  ] as const) {
    if (isEmpty(value)) throw new GeneralException(`${name} is required`);
  }
  const role = hostRole(input);
  const clusterName = requiredString(input.config, "cluster.cluster_name");
  const clusterBucket = requiredString(
    input.config,
    "cluster.cluster_s3_bucket",
  );
  const clusterHome = requiredString(input.config, "cluster.home_dir");
  const awsRegion = requiredString(input.config, "cluster.aws.region");
  const proxy =
    input.config.getString("cluster.network.https_proxy", "") ?? "";
  const noProxy =
    proxy === ""
      ? ""
      : (input.config.getString("cluster.network.no_proxy", "") ?? "");
  const nvidiaGpu = isNvidiaGpu(input.config, role.instanceType);
  const cloudWatchConfig = cloudWatchAgentConfig(
    input.config,
    role,
    input.moduleId,
    nvidiaGpu,
  );
  const initialPrometheusConfig = prometheusConfig(
    input.config,
    role,
    input.moduleId,
  );
  const exporters =
    initialPrometheusConfig === undefined
      ? []
      : role.nodeType === "app"
        ? ["node_exporter", "app_exporter"]
        : ["node_exporter"];

  return {
    config: configFacade(input.config),
    base_os: role.baseOs,
    instance_type: role.instanceType,
    module_name: input.moduleName,
    module_id: input.moduleId,
    module_set: input.moduleSet,
    module_version: ideaVersion(),
    vars: {
      ...role.vars,
      cloudwatch_agent_config: cloudWatchConfig,
      ...(initialPrometheusConfig === undefined
        ? {}
        : {
            prometheus_config: initialPrometheusConfig,
            prometheus_exporters: exporters,
          }),
    },
    utils: {
      to_json: (value: unknown, ...args: unknown[]) =>
        JSON.stringify(
          value,
          undefined,
          callArgument(args, 0, "indent") === true ? 2 : undefined,
        ),
      to_yaml: (value: unknown) => toYaml(value),
    },
    cluster_name: clusterName,
    cluster_s3_bucket: clusterBucket,
    cluster_home_dir: clusterHome,
    aws_region: awsRegion,
    app_deploy_dir: "/opt/idea/app",
    https_proxy: proxy,
    no_proxy: noProxy,
    default_system_user: defaultSystemUser(role.baseOs),
    has_storage_provider: (provider: string) =>
      hasStorageProvider(input.config, provider),
    job_has_storage_provider: () => false,
    job_has_param: () => false,
    eval_shared_storage_scope: (...args: unknown[]) =>
      sharedStorageApplies(
        input.moduleName,
        callArgument(args, 0, "shared_storage"),
      ),
    is_gpu_instance_type: () => {
      const family = role.instanceType.split(".")[0];
      return input.config
        .getList<string>(
          "global-settings.gpu_settings.instance_families",
          [],
        )
        .includes(family);
    },
    is_nvidia_gpu: () => nvidiaGpu,
    is_amd_gpu: () => {
      const family = role.instanceType.split(".")[0];
      return (
        input.config
          .getList<string>(
            "global-settings.gpu_settings.instance_families",
            [],
          )
          .includes(family) && !nvidiaGpu
      );
    },
    fail_on_missing_gpu_driver: () =>
      input.config.getBool(
        "global-settings.gpu_settings.fail_on_missing_driver",
        true,
      ),
    get_nvidia_gpu_driver_version: () => {
      const family = role.instanceType.split(".")[0];
      return requiredString(
        input.config,
        `global-settings.gpu_settings.nvidia_public_driver_versions.${family}`,
      );
    },
    get_custom_aws_tags: () => customTags(input.config),
    get_cloudwatch_agent_config: (...args: unknown[]) => {
      const additional = callArgument(args, 0, "additional_log_files");
      if (!Array.isArray(additional) || additional.length === 0) {
        return cloudWatchConfig;
      }
      const logs = cloudWatchConfig.logs;
      if (!isRecord(logs)) return cloudWatchConfig;
      const collected = logs.logs_collected;
      if (!isRecord(collected) || !isRecord(collected.files)) {
        return cloudWatchConfig;
      }
      const existing = collected.files.collect_list;
      collected.files.collect_list = [
        ...(Array.isArray(existing) ? existing : []),
        ...additional,
      ];
      return cloudWatchConfig;
    },
    is_metrics_provider_prometheus: () =>
      initialPrometheusConfig !== undefined,
    get_prometheus_config: (...args: unknown[]) => {
      if (initialPrometheusConfig === undefined) return undefined;
      const additional = callArgument(args, 0, "additional_scrape_configs");
      if (!Array.isArray(additional) || additional.length === 0) {
        return initialPrometheusConfig;
      }
      const existing = initialPrometheusConfig.scrape_configs;
      initialPrometheusConfig.scrape_configs = [
        ...(Array.isArray(existing) ? existing : []),
        ...additional,
      ];
      return initialPrometheusConfig;
    },
    is_prometheus_exporter_enabled: (name: string) =>
      exporters.includes(name),
  };
}
