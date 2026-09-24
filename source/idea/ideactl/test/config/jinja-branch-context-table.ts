/**
 * Branch combinations that open template bodies absent from the baseline audit.
 * Each target is rendered through its loader root so included templates use the
 * same path resolution as package creation.
 */

export interface BranchFlags {
  readonly directory: boolean;
  readonly metrics: boolean;
  readonly gpu: boolean;
  readonly fsx: boolean;
  readonly instanceStorage: boolean;
  readonly storageScope: boolean;
}

export interface BranchFamily {
  readonly name: string;
  readonly blocks: readonly string[];
  readonly source: "bootstrap" | "config";
  readonly flags: BranchFlags;
  readonly metricsProvider?: "amazon_managed_prometheus" | "prometheus";
  /** Overrides `context.base_os`, which every distribution-specific block branches on. */
  readonly baseOs?: string;
  readonly templates: readonly string[];
}

/** Every value the bootstrap templates test `base_os` against. */
export const BASE_OS_VALUES: readonly string[] = [
  "amazonlinux2023",
  "rhel8",
  "rhel9",
  "rhel10",
  "rocky8",
  "rocky9",
  "rocky10",
  "ubuntu2204",
  "ubuntu2404",
  "ubuntu2604",
];

/** The component entry points a bootstrap package renders. */
export const COMPONENT_ENTRY_POINTS: readonly string[] = [
  "bastion-host/setup.sh.jinja2",
  "cluster-manager/install_app.sh.jinja2",
  "cluster-manager/setup.sh.jinja2",
  "compute-node-ami-builder/compute_node_ami_builder.sh.jinja2",
  "compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2",
  "compute-node-ami-builder/setup.sh.jinja2",
  "compute-node/compute_node.sh.jinja2",
  "compute-node/compute_node_post_reboot.sh.jinja2",
  "compute-node/setup.sh.jinja2",
  "dcv-broker/install_app.sh.jinja2",
  "dcv-broker/setup.sh.jinja2",
  "dcv-connection-gateway/install_app.sh.jinja2",
  "dcv-connection-gateway/setup.sh.jinja2",
  "dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2",
  "dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2",
  "dcv-host-ami-builder/setup.sh.jinja2",
  "openldap-server/setup.sh.jinja2",
  "scheduler/install_app.sh.jinja2",
  "scheduler/scheduler_post_reboot.sh.jinja2",
  "scheduler/setup.sh.jinja2",
  "virtual-desktop-controller/install_app.sh.jinja2",
  "virtual-desktop-controller/setup.sh.jinja2",
  "virtual-desktop-host-linux/configure_dcv_host.sh.jinja2",
  "virtual-desktop-host-linux/configure_dcv_host_post_reboot.sh.jinja2",
  "virtual-desktop-host-linux/setup.sh.jinja2",
  "virtual-desktop-host-windows/Configure.ps1.jinja2",
  "virtual-desktop-host-windows/ConfigureDCVHost.ps1.jinja2",
];

const disabled: BranchFlags = {
  directory: false,
  metrics: false,
  gpu: false,
  fsx: false,
  instanceStorage: false,
  storageScope: false,
};

const directoryTemplates = [
  "_templates/linux/join_directoryservice.jinja2",
  "virtual-desktop-host-linux/setup.sh.jinja2",
  "virtual-desktop-controller/setup.sh.jinja2",
  "scheduler/setup.sh.jinja2",
  "compute-node/compute_node.sh.jinja2",
  "virtual-desktop-host-windows/Configure.ps1.jinja2",
  "virtual-desktop-host-windows/ConfigureDCVHost.ps1.jinja2",
];

const metricsTemplates = [
  "virtual-desktop-host-linux/setup.sh.jinja2",
  "virtual-desktop-controller/setup.sh.jinja2",
  "scheduler/setup.sh.jinja2",
  "openldap-server/setup.sh.jinja2",
  "dcv-connection-gateway/setup.sh.jinja2",
  "dcv-broker/setup.sh.jinja2",
  "compute-node/compute_node.sh.jinja2",
  "compute-node-ami-builder/compute_node_ami_builder.sh.jinja2",
  "cluster-manager/setup.sh.jinja2",
  "bastion-host/setup.sh.jinja2",
];

const gpuTemplates = [
  "_templates/linux/gpu_drivers.jinja2",
  "_templates/linux/disable_nouveau_drivers.jinja2",
  "_templates/linux/dcv_server.jinja2",
  "virtual-desktop-host-linux/setup.sh.jinja2",
  "virtual-desktop-host-linux/configure_dcv_host.sh.jinja2",
  "dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2",
  "compute-node/compute_node_post_reboot.sh.jinja2",
];

const storageTemplates = [
  "_templates/linux/mount_shared_storage.jinja2",
  "_templates/windows/mount_shared_storage.jinja2",
  "virtual-desktop-host-linux/setup.sh.jinja2",
  "virtual-desktop-host-windows/Configure.ps1.jinja2",
  "virtual-desktop-controller/setup.sh.jinja2",
  "scheduler/setup.sh.jinja2",
  "compute-node/compute_node.sh.jinja2",
  "dcv-host-ami-builder/setup.sh.jinja2",
  "dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2",
  "compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2",
];

/**
 * Every component entry point crossed with every `base_os`, with the feature flags on so the
 * distribution-specific bodies inside the gated blocks render too.
 */
const baseOsFamilies: readonly BranchFamily[] = BASE_OS_VALUES.map((baseOs) => ({
  name: `base-os-${baseOs}`,
  blocks: [`every component entry point on ${baseOs}`],
  source: "bootstrap" as const,
  flags: { directory: true, metrics: true, gpu: true, fsx: true, instanceStorage: true, storageScope: true },
  baseOs,
  templates: [...COMPONENT_ENTRY_POINTS, "_templates/linux/gnome_online_accounts.jinja2"],
}));

export const BRANCH_FAMILIES: readonly BranchFamily[] = [
  {
    name: "directory",
    blocks: ["Linux and Windows directory join"],
    source: "bootstrap",
    flags: { ...disabled, directory: true },
    templates: directoryTemplates,
  },
  {
    name: "metrics",
    blocks: ["Prometheus provider setup and node exporter"],
    source: "bootstrap",
    flags: { ...disabled, metrics: true },
    templates: metricsTemplates,
  },
  {
    name: "metrics-amp-settings",
    blocks: ["managed time-series metrics settings"],
    source: "config",
    flags: { ...disabled, metrics: true },
    metricsProvider: "amazon_managed_prometheus",
    templates: ["metrics/settings.yml"],
  },
  {
    name: "metrics-prometheus-settings",
    blocks: ["external time-series metrics settings"],
    source: "config",
    flags: { ...disabled, metrics: true },
    metricsProvider: "prometheus",
    templates: ["metrics/settings.yml"],
  },
  {
    name: "gpu",
    blocks: ["GPU predicate, driver installation, and Nouveau disablement"],
    source: "bootstrap",
    flags: { ...disabled, gpu: true },
    templates: gpuTemplates,
  },
  {
    name: "fsx",
    blocks: ["parallel file-system client and mount paths"],
    source: "bootstrap",
    flags: { ...disabled, fsx: true, storageScope: true },
    templates: [
      ...storageTemplates,
      "compute-node/_templates/scratch_storage.jinja2",
      "compute-node/compute_node_post_reboot.sh.jinja2",
      "compute-node-ami-builder/compute_node_ami_builder.sh.jinja2",
    ],
  },
  {
    name: "instance-storage",
    blocks: ["Linux and Windows instance-storage setup"],
    source: "bootstrap",
    flags: { ...disabled, instanceStorage: true },
    templates: [
      "_templates/linux/instance_storage.jinja2",
      "_templates/windows/configure_instance_storage.jinja2",
      "virtual-desktop-host-linux/setup.sh.jinja2",
      "virtual-desktop-host-windows/Configure.ps1.jinja2",
    ],
  },
  {
    name: "storage-scope",
    blocks: ["storage-provider and shared-storage-scope predicates"],
    source: "bootstrap",
    flags: { ...disabled, storageScope: true },
    templates: storageTemplates,
  },
  {
    name: "nested-combination",
    blocks: ["nested directory, metrics, GPU, parallel file-system, and storage-scope bodies"],
    source: "bootstrap",
    flags: {
      directory: true,
      metrics: true,
      gpu: true,
      fsx: true,
      instanceStorage: true,
      storageScope: true,
    },
    templates: [
      "virtual-desktop-host-linux/setup.sh.jinja2",
      "virtual-desktop-host-linux/configure_dcv_host.sh.jinja2",
      "virtual-desktop-host-windows/Configure.ps1.jinja2",
      "compute-node/compute_node.sh.jinja2",
      "compute-node/compute_node_post_reboot.sh.jinja2",
      "compute-node-ami-builder/compute_node_ami_builder.sh.jinja2",
      "dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2",
    ],
  },
  ...baseOsFamilies,
];
