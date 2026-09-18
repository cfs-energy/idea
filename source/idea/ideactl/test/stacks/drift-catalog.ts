/**
 * Synthetic operator edits covering every upgrade merge class, including the two
 * loss classes the production table happens not to carry today.
 */

import type { ConfigEntry } from "../../src/config/generator.ts";
import type { ModuleInfo } from "../../src/config/cluster-config.ts";
import type { StackSettingsPlan } from "../../src/config/upgrade-drift.ts";

export const CLUSTER = "idea-drift";
export const REGION = "us-east-2";
export const BASE_OS = "amazonlinux2023";
export const RELEASE_AMI = "ami-0release000000001";
export const OPERATOR_AMI = "ami-0operator00000001";
export const BUILT_AMI = "ami-0built00000000001";
export const BUILT_AMI_NAME = "idea-compute-node-sample";

export interface OperatorEdit {
  id: string;
  /** Analysis / preview action class, or a named subclass of PRESERVE_DRIFT. */
  editClass: string;
  key: string;
  operatorValue: unknown;
  predictedSurvive: boolean;
  /**
   * When false, the key is absent before the upgrade so Phase 2b can ADD it.
   * Default is to write the operator value onto the seeded table.
   */
  seed?: boolean;
  note: string;
}

/** Regenerated configuration the upgrade would sync after Phase 1. */
export const GENERATED: ConfigEntry[] = [
  { key: "global-settings.package_config.dcv.version", value: "2025.0" },
  { key: "global-settings.custom_tags", value: [] },
  { key: "global-settings.type_value", value: 1 },
  { key: "global-settings.same", value: [] },
  { key: "cluster.locale", value: "en_US" },
  { key: "cluster.timezone", value: "America/New_York" },
  { key: "cluster.ses.enabled", value: false },
  { key: "cluster.ses.sender_email", value: "admin@example.invalid" },
  { key: "cluster.brand_new", value: true },
  { key: "cluster-manager.web_portal.title", value: "Integrated Digital Engineering on AWS" },
  { key: "cluster-manager.logging.profile", value: "production" },
  { key: "cluster-manager.bedrock.enabled", value: false },
  { key: "cluster-manager.bedrock.model_ids", value: [] },
  { key: "cluster-manager.ec2.autoscaling.instance_type", value: "m6i.large" },
  { key: "cluster-manager.ec2.autoscaling.base_os", value: BASE_OS },
  { key: "cluster-manager.ec2.autoscaling.instance_ami", value: RELEASE_AMI },
  { key: "directoryservice.ldap_base", value: "dc=idea,dc=local" },
  { key: "directoryservice.name", value: "idea.local" },
  { key: "directoryservice.instance_type", value: "m6i.large" },
  { key: "scheduler.instance_type", value: "m6i.large" },
  { key: "scheduler.instance_ami", value: RELEASE_AMI },
  { key: "scheduler.base_os", value: BASE_OS },
  { key: "scheduler.compute_node_ami", value: RELEASE_AMI },
  { key: "scheduler.compute_node_os", value: BASE_OS },
  { key: "analytics.opensearch.data_node_instance_type", value: "m5.large.search" },
  { key: "analytics.opensearch.domain_name", value: "sample-domain" },
  { key: "vdc.server.usb_remotization", value: [] },
  { key: "vdc.controller.autoscaling.instance_type", value: "m6i.large" },
  { key: "vdc.dcv_broker.autoscaling.instance_type", value: "m6i.large" },
  { key: "vdc.dcv_connection_gateway.autoscaling.instance_type", value: "m6i.large" },
  { key: "bastion-host.instance_type", value: "m6i.large" },
  { key: "cluster.iam.ec2_managed_policy_arns", value: [] },
  { key: "ecs.datadog.enabled", value: true },
  { key: "metrics.provider", value: "dogstatsd" },
  { key: "metrics.dogstatsd.url", value: "unix:///var/run/datadog/dsd.socket" },
];

/**
 * Every operator edit class the demonstration seeds.
 *
 * `predictedSurvive` is the analysis prediction for the operator value after
 * Phases 2, 2b, 3, and a selected cluster-manager stack Update.
 */
export const EDITS: OperatorEdit[] = [
  {
    id: "global-custom-tags",
    editClass: "GLOBAL_CHANGE",
    key: "global-settings.custom_tags",
    operatorValue: ["Key=Owner,Value=ops"],
    predictedSurvive: false,
    note: "Wholesale prefix delete. Generated empty list wins.",
  },
  {
    id: "global-version-string",
    editClass: "GLOBAL_CHANGE",
    key: "global-settings.package_config.dcv.version",
    operatorValue: "2024.0",
    predictedSurvive: false,
    note: "Wholesale prefix delete. Regenerated package version wins.",
  },
  {
    id: "global-type-change",
    editClass: "GLOBAL_TYPE_CHANGE",
    key: "global-settings.type_value",
    operatorValue: "1",
    predictedSurvive: false,
    note: "Prefix rewrite recreates the row with the generated number type.",
  },
  {
    id: "global-operator-only",
    editClass: "GLOBAL_REMOVE",
    key: "global-settings.operator_only",
    operatorValue: "keep me",
    predictedSurvive: false,
    note: "Custom global row is deleted and has no generated replacement.",
  },
  {
    id: "global-same",
    editClass: "GLOBAL_REWRITE_SAME",
    key: "global-settings.same",
    operatorValue: [],
    predictedSurvive: true,
    note: "Value matches generated, so it comes back. Version restarts at 1.",
  },
  {
    id: "generated-missing",
    editClass: "ADD",
    key: "cluster.brand_new",
    operatorValue: true,
    predictedSurvive: true,
    seed: false,
    note: "Generated key absent from the table. Phase 2b inserts the generated value.",
  },
  {
    id: "locale-encoding",
    editClass: "PRESERVE_DRIFT",
    key: "cluster.locale",
    operatorValue: "en_US.UTF-8",
    predictedSurvive: true,
    note: "Add-only sync skips an existing non-global key.",
  },
  {
    id: "portal-title",
    editClass: "PRESERVE_DRIFT",
    key: "cluster-manager.web_portal.title",
    operatorValue: "Sample Engineering Portal",
    predictedSurvive: true,
    note: "Portal title is generated, not a Phase 3 or selected stack key.",
  },
  {
    id: "logging-profile",
    editClass: "PRESERVE_DRIFT",
    key: "cluster-manager.logging.profile",
    operatorValue: "debug",
    predictedSurvive: true,
    note: "Logging profile is add-only. Production-shaped surviving edit.",
  },
  {
    id: "integration-enabled",
    editClass: "PRESERVE_DRIFT",
    key: "cluster-manager.bedrock.enabled",
    operatorValue: true,
    predictedSurvive: true,
    note: "Integration flag lives in the table and is not rewritten.",
  },
  {
    id: "integration-models",
    editClass: "PRESERVE_DRIFT",
    key: "cluster-manager.bedrock.model_ids",
    operatorValue: ["example.model-a", "example.model-b"],
    predictedSurvive: true,
    note: "Model list is add-only. Production-shaped surviving edit.",
  },
  {
    id: "mail-enabled",
    editClass: "PRESERVE_DRIFT",
    key: "cluster.ses.enabled",
    operatorValue: true,
    predictedSurvive: true,
    note: "Mail sending switched on. Add-only keeps it.",
  },
  {
    id: "mail-sender",
    editClass: "PRESERVE_DRIFT",
    key: "cluster.ses.sender_email",
    operatorValue: "operator@example.invalid",
    predictedSurvive: true,
    note: "Sender address is add-only. Production-shaped surviving edit.",
  },
  {
    id: "directory-ldap",
    editClass: "PRESERVE_DRIFT",
    key: "directoryservice.ldap_base",
    operatorValue: "dc=example,dc=invalid",
    predictedSurvive: true,
    note: "Directory path pointing at example.invalid rather than the placeholder.",
  },
  {
    id: "directory-name",
    editClass: "PRESERVE_DRIFT",
    key: "directoryservice.name",
    operatorValue: "ad.example.invalid",
    predictedSurvive: true,
    note: "Directory short name. Add-only keeps it.",
  },
  {
    id: "instance-scheduler",
    editClass: "PRESERVE_DRIFT",
    key: "scheduler.instance_type",
    operatorValue: "m5.2xlarge",
    predictedSurvive: true,
    note: "Larger than default. Phase 3 only rewrites the exact value m6i.large.",
  },
  {
    id: "instance-vdc-controller",
    editClass: "PRESERVE_DRIFT",
    key: "vdc.controller.autoscaling.instance_type",
    operatorValue: "m5.4xlarge",
    predictedSurvive: true,
    note: "Larger than default. Not the Phase 3 trigger value.",
  },
  {
    id: "instance-vdc-broker",
    editClass: "PRESERVE_DRIFT",
    key: "vdc.dcv_broker.autoscaling.instance_type",
    operatorValue: "r5.xlarge",
    predictedSurvive: true,
    note: "Larger than default. Not the Phase 3 trigger value.",
  },
  {
    id: "instance-vdc-gateway",
    editClass: "PRESERVE_DRIFT",
    key: "vdc.dcv_connection_gateway.autoscaling.instance_type",
    operatorValue: "c5.2xlarge",
    predictedSurvive: true,
    note: "Larger than default. Not the Phase 3 trigger value.",
  },
  {
    id: "instance-directory",
    editClass: "PRESERVE_DRIFT",
    key: "directoryservice.instance_type",
    operatorValue: "m5.xlarge",
    predictedSurvive: true,
    note: "Larger than default. Not the Phase 3 trigger value.",
  },
  {
    id: "instance-bastion",
    editClass: "PRESERVE_DRIFT",
    key: "bastion-host.instance_type",
    operatorValue: "r5.2xlarge",
    predictedSurvive: true,
    note: "Larger than default. Not the Phase 3 trigger value.",
  },
  {
    id: "retained-m6i-default",
    editClass: "PHASE3_OVERWRITE",
    key: "cluster-manager.ec2.autoscaling.instance_type",
    operatorValue: "m6i.large",
    predictedSurvive: false,
    note: "Retained old default. Phase 3 writes m7i.large when the region offers it.",
  },
  {
    id: "opensearch-old-default",
    editClass: "PHASE3_OVERWRITE",
    key: "analytics.opensearch.data_node_instance_type",
    operatorValue: "m5.large.search",
    predictedSurvive: false,
    note: "Retained old OpenSearch default. Phase 3 writes m7g.large.search when offered.",
  },
  {
    id: "machine-image",
    editClass: "PHASE3_OVERWRITE",
    key: "scheduler.instance_ami",
    operatorValue: OPERATOR_AMI,
    predictedSurvive: false,
    note: "Unrecognised custom image. Phase 3 writes the release AMI.",
  },
  {
    id: "metrics-provider-cutover",
    editClass: "PROVIDER_CUTOVER",
    key: "metrics.provider",
    operatorValue: "cloudwatch",
    predictedSurvive: false,
    note: "The values file moves metrics to the agent daemon. Written after Phase 3; never gated as drift.",
  },
  {
    id: "compute-built-image",
    editClass: "PRESERVE_DRIFT",
    key: "scheduler.compute_node_ami",
    operatorValue: BUILT_AMI,
    predictedSurvive: true,
    note: "Built compute image newer than the release image is the Phase 3 keep exception.",
  },
  {
    id: "type-null-vs-list",
    editClass: "PRESERVE_TYPE_DRIFT",
    key: "vdc.server.usb_remotization",
    operatorValue: null,
    predictedSurvive: true,
    note: "Existence skip does not compare types. NULL stays NULL.",
  },
  {
    id: "type-null-iam",
    editClass: "PRESERVE_TYPE_DRIFT",
    key: "cluster.iam.ec2_managed_policy_arns",
    operatorValue: null,
    predictedSurvive: true,
    note: "Generated [] does not replace an existing NULL.",
  },
  {
    id: "orphan-subnet",
    editClass: "ORPHAN_PRESERVED",
    key: "scheduler.job_provisioning.preferred_subnet_id",
    operatorValue: "subnet-00000001",
    predictedSurvive: true,
    note: "Table-only non-global row. Full sync never deletes.",
  },
  {
    id: "orphan-cidr",
    editClass: "ORPHAN_PRESERVED",
    key: "cluster.network.vpc_cidr_block",
    operatorValue: "192.0.2.0/24",
    predictedSurvive: true,
    note: "Stale generated-branch row. RFC 5737 documentation prefix.",
  },
  {
    id: "stack-overwrite-selected",
    editClass: "STACK_OVERWRITE",
    key: "cluster-manager.external_alb_dns",
    operatorValue: "operator.example.invalid",
    predictedSurvive: false,
    note: "Selected stack Update writes every current settings-map key.",
  },
  {
    id: "stack-delete-selected",
    editClass: "STACK_DELETE",
    key: "cluster-manager.removed_output",
    operatorValue: "operator-removed",
    predictedSurvive: false,
    note: "Key is in the old settings map and omitted from the new map, so the handler deletes it.",
  },
  {
    id: "stack-unselected",
    editClass: "UNSELECTED_STACK_PRESERVED",
    key: "metrics.external_alb_dns",
    operatorValue: "metrics-operator.example.invalid",
    predictedSurvive: true,
    note: "Unselected stack. Phase 4 does not run its custom resource. The preview is silent because it is owned but not selected.",
  },
];

export const MODULES: ModuleInfo[] = [
  { module_id: "cluster-manager", name: "cluster-manager", type: "app", status: "deployed" },
  { module_id: "scheduler", name: "scheduler", type: "app", status: "deployed" },
  { module_id: "vdc", name: "virtual-desktop-controller", type: "app", status: "deployed" },
  { module_id: "analytics", name: "analytics", type: "stack", status: "deployed" },
  { module_id: "directoryservice", name: "directoryservice", type: "stack", status: "deployed" },
  { module_id: "bastion-host", name: "bastion-host", type: "stack", status: "deployed" },
  { module_id: "metrics", name: "metrics", type: "stack", status: "deployed" },
];

export const SELECTED_STACK: StackSettingsPlan = {
  moduleId: "cluster-manager",
  selected: true,
  previous: {
    external_alb_dns: "deployed.example.invalid",
    removed_output: "old-output",
  },
  target: {
    external_alb_dns: "stack.example.invalid",
  },
};

export const UNSELECTED_STACK: StackSettingsPlan = {
  moduleId: "metrics",
  selected: false,
  previous: {
    external_alb_dns: "metrics-deployed.example.invalid",
  },
  target: {
    external_alb_dns: "metrics-stack.example.invalid",
  },
};

/** Keys written with source=stack before the operator overlay. */
export const STACK_SEED: Array<{ key: string; value: unknown }> = [
  { key: "cluster-manager.external_alb_dns", value: "deployed.example.invalid" },
  { key: "cluster-manager.removed_output", value: "old-output" },
  { key: "metrics.external_alb_dns", value: "metrics-deployed.example.invalid" },
];
