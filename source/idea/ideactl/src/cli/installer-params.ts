/**
 * The installer parameter flow declared in `resources/input_params/install_params.yml`.
 *
 * This reads the source declaration at runtime, preserves its section order and conditions, and
 * collects values through an injected prompt driver. Resource-dependent choices are supplied by
 * an injected provider so the flow remains deterministic in tests and replayed environments.
 */

import { readFileSync } from "node:fs";

import yaml from "js-yaml";

import { resourcePath } from "../config/values.ts";
import type { InstallerChoice, InstallerPromptType, InstallerQuestion, PromptDriver } from "./prompts.ts";

/** A resolved account identity inserted after the AWS account section. */
export interface InstallerIdentity {
  accountId: string;
  partition: string;
  dnsSuffix: string;
}

/** Details used for choice display and existing-resource validation. */
export interface InstallerSubnet {
  id: string;
  availabilityZone: string;
  isOutpost?: boolean;
}

/** Supplies choices that Python obtains from the selected AWS account. */
export interface InstallerChoiceProvider {
  choices?(name: string, values: Readonly<Record<string, unknown>>): InstallerChoice[];
  subnets?(vpcId: string): InstallerSubnet[];
  vpcCidrs?(): string[];
  clusterNames?(): string[];
}

/** Inputs for one run of either installer module. */
export interface InstallerRunOptions {
  driver: PromptDriver;
  identity(values: Readonly<Record<string, unknown>>): Promise<InstallerIdentity>;
  existingResources?: boolean;
  regenerate?: boolean;
  answers?: Readonly<Record<string, unknown>>;
  choices?: InstallerChoiceProvider;
  inputParamsFile?: string;
}

/** A validation failure that the interactive loop displays before asking again. */
export class InstallerValidationError extends Error {}

interface RawModule {
  name: string;
  sections: RawSection[];
}

interface RawSection {
  name: string;
  params: Array<{ name: string }>;
}

interface RawChoice {
  title?: string;
  value?: string;
  disabled?: boolean;
}

interface RawCondition {
  param?: string;
  eq?: unknown;
  contains?: unknown;
  and?: RawCondition[];
}

interface RawParam {
  name: string;
  title?: string;
  description?: string;
  param_type?: string;
  data_type?: string;
  multiple?: boolean;
  default?: unknown;
  choices?: RawChoice[];
  help_text?: string;
  validate?: { required?: boolean; regex?: string; min?: number; max?: number };
  when?: RawCondition;
  custom?: { defaults?: Record<string, string> };
}

interface RawSpec {
  SocaInputParamSpec?: {
    modules?: RawModule[];
    params?: RawParam[];
  };
}

const REQUIRED_MODULES: readonly InstallerChoice[] = [
  { title: "Global Settings (required)", value: "global-settings", disabled: true },
  { title: "Cluster (required)", value: "cluster", disabled: true },
  { title: "Analytics (required)", value: "analytics", disabled: true },
  { title: "Identity Provider (required)", value: "identity-provider", disabled: true },
  { title: "Directory Service (required)", value: "directoryservice", disabled: true },
  { title: "Shared Storage (required)", value: "shared-storage", disabled: true },
  { title: "Cluster Manager (required)", value: "cluster-manager", disabled: true },
];

/**
 * Every new cluster runs its control plane as container tasks, so the installer asks no question
 * about it and writes the key the generator splices the container module in from. There is no
 * second shape to choose: the per-module release archives a control-plane host downloads are no
 * longer produced.
 *
 * The key belongs in the values file rather than in the generator's default because regenerating
 * an existing cluster's configuration has to keep producing what that cluster already has.
 */
const CONTAINER_MODULE_VALUES_KEY = "enable_ecs";

/** Modules the container stack runs as tasks, which it names unconditionally. */
const CONTAINER_REQUIRED_MODULES = ["scheduler", "virtual-desktop-controller"];

/** How many times one question may be re-asked before the validation failure is raised instead. */
const MAX_PROMPT_ATTEMPTS = 50;

const OPTIONAL_MODULES: readonly InstallerChoice[] = [
  { title: "Metrics and Monitoring", value: "metrics" },
  { title: "Scale-out Computing on AWS (SOCA) for HPC", value: "scheduler" },
  { title: "Enterprise Virtual Desktop Infrastructure (eVDI)", value: "virtual-desktop-controller" },
  { title: "Bastion Host", value: "bastion-host" },
];

const METRICS_PROVIDERS: readonly InstallerChoice[] = [
  { title: "AWS CloudWatch", value: "cloudwatch" },
  { title: "Amazon Managed Service for Prometheus", value: "amazon_managed_prometheus" },
  { title: "Datadog agent (DogStatsD)", value: "dogstatsd" },
  { title: "Custom Prometheus Server", value: "prometheus" },
];

/** Parses the declaration with runtime shape checks instead of trusting YAML data. */
function loadSpec(file: string): { modules: RawModule[]; params: Map<string, RawParam> } {
  const parsed = yaml.load(readFileSync(file, "utf-8"));
  if (!isRecord(parsed) || !isRecord(parsed["SocaInputParamSpec"])) {
    throw new InstallerValidationError(`invalid installer parameter declaration: ${file}`);
  }
  const root = parsed["SocaInputParamSpec"] as RawSpec["SocaInputParamSpec"];
  if (!Array.isArray(root?.modules) || !Array.isArray(root.params)) {
    throw new InstallerValidationError(`invalid installer parameter declaration: ${file}`);
  }
  const modules = root.modules.filter(isModule);
  const params = new Map(root.params.filter(isParam).map((param) => [param.name, param]));
  if (modules.length !== root.modules.length || params.size !== root.params.length) {
    throw new InstallerValidationError(`invalid installer parameter declaration: ${file}`);
  }
  return { modules, params };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isModule(value: unknown): value is RawModule {
  return isRecord(value) && typeof value.name === "string" && Array.isArray(value.sections) &&
    value.sections.every(isSection);
}

function isSection(value: unknown): value is RawSection {
  return isRecord(value) && typeof value.name === "string" && Array.isArray(value.params) &&
    value.params.every((param) => isRecord(param) && typeof param.name === "string");
}

function isParam(value: unknown): value is RawParam {
  return isRecord(value) && typeof value.name === "string";
}

function isEmpty(value: unknown): boolean {
  return value === undefined || value === null ||
    (typeof value === "string" && value.trim() === "") ||
    (Array.isArray(value) && value.length === 0);
}

function yamlBoolean(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return value;
  if (typeof value !== "string") return undefined;
  if (["yes", "y", "true", "1", "on"].includes(value.toLowerCase())) return true;
  if (["no", "n", "false", "0", "off"].includes(value.toLowerCase())) return false;
  return undefined;
}

function conditionMatches(condition: RawCondition | undefined, values: Readonly<Record<string, unknown>>): boolean {
  if (condition === undefined) return true;
  if (Array.isArray(condition.and)) return condition.and.every((item) => conditionMatches(item, values));
  if (typeof condition.param !== "string") return false;
  const value = values[condition.param];
  if (condition.contains !== undefined) {
    return Array.isArray(value) && value.some((item) => item === condition.contains);
  }
  if (condition.eq !== undefined) return value === condition.eq;
  return false;
}

function promptType(param: RawParam): InstallerPromptType {
  if (param.param_type === "text" || param.param_type === "select" ||
    param.param_type === "checkbox" || param.param_type === "confirm") {
    return param.param_type;
  }
  throw new InstallerValidationError(`unsupported prompt type for ${param.name}: ${String(param.param_type)}`);
}

function declaredChoices(param: RawParam, options: InstallerRunOptions, values: Readonly<Record<string, unknown>>): InstallerChoice[] {
  const dynamic = options.choices?.choices?.(param.name, values);
  if (dynamic !== undefined) return dynamic;
  if (param.name === "enabled_modules") return [...REQUIRED_MODULES, ...OPTIONAL_MODULES];
  if (param.name === "metrics_provider") return [...METRICS_PROVIDERS];
  return (param.choices ?? []).flatMap((choice) =>
    typeof choice.value === "string"
      ? [{ title: choice.title ?? choice.value, value: choice.value, disabled: choice.disabled }]
      : [],
  );
}

function defaultValue(param: RawParam, choices: readonly InstallerChoice[], values: Readonly<Record<string, unknown>>): unknown {
  if (param.name === "aws_region" && isRecord(param.custom?.defaults)) {
    const partition = values["aws_partition"];
    if (typeof partition === "string") return param.custom.defaults[partition];
  }
  if (param.default === "$first") return choices.find((choice) => choice.disabled !== true)?.value;
  if (param.param_type === "confirm") return yamlBoolean(param.default) ?? param.default;
  return param.default;
}

function toBoolean(value: unknown, name: string): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const normalised = value.trim().toLowerCase();
    if (["yes", "y", "true", "1", "on"].includes(normalised)) return true;
    if (["no", "n", "false", "0", "off"].includes(normalised)) return false;
  }
  throw new InstallerValidationError(`${name} must be a boolean`);
}

function normaliseAnswer(value: unknown, param: RawParam): unknown {
  const type = promptType(param);
  if (type === "confirm") return toBoolean(value, param.name);
  if (type === "checkbox") {
    if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter((item) => item !== "");
    if (typeof value === "string") return value.split(",").map((item) => item.trim()).filter((item) => item !== "");
    throw new InstallerValidationError(`${param.name} must be a list`);
  }
  if (param.data_type === "int") {
    if (typeof value === "number" && Number.isInteger(value)) return value;
    if (typeof value === "string" && /^[+-]?\d+$/.test(value.trim())) return Number.parseInt(value.trim(), 10);
    throw new InstallerValidationError(`${param.name} must be an integer`);
  }
  if (typeof value === "string") return value.trim();
  if (value === undefined || value === null) return value;
  return String(value);
}

function validateDeclaredRules(value: unknown, param: RawParam, choices: readonly InstallerChoice[]): void {
  const rules = param.validate;
  if (yamlBoolean(rules?.required) === true && isEmpty(value)) {
    throw new InstallerValidationError(`${param.title ?? param.name} is required`);
  }
  if (typeof rules?.regex === "string" && typeof value === "string" && !new RegExp(rules.regex).test(value)) {
    throw new InstallerValidationError(`${param.title ?? param.name} is invalid`);
  }
  if (typeof value === "number") {
    if (rules?.min !== undefined && value < rules.min) throw new InstallerValidationError(`${param.title ?? param.name} must be at least ${rules.min}`);
    if (rules?.max !== undefined && value > rules.max) throw new InstallerValidationError(`${param.title ?? param.name} must be at most ${rules.max}`);
  }
  if ((param.param_type === "select" || param.param_type === "checkbox") && choices.length > 0 && !isEmpty(value)) {
    const selected = Array.isArray(value) ? value : [value];
    for (const item of selected) {
      const choice = choices.find((entry) => entry.value === item);
      if (choice === undefined || choice.disabled === true) throw new InstallerValidationError(`${param.title ?? param.name} has an invalid selection`);
    }
  }
}

function validateCidr(value: string): void {
  for (const token of value.split(",")) {
    const cidr = token.trim();
    const match = /^(\d{1,3}(?:\.\d{1,3}){3})(?:\/(\d{1,2}))?$/.exec(cidr);
    if (match === null) throw new InstallerValidationError(`CIDR Value: ${cidr} is invalid. Please enter a valid CIDR block`);
    const octets = match[1].split(".").map((part) => Number.parseInt(part, 10));
    const prefix = match[2] === undefined ? 32 : Number.parseInt(match[2], 10);
    if (octets.some((octet) => octet > 255) || prefix > 32) {
      throw new InstallerValidationError(`CIDR Value: ${cidr} is invalid. Please enter a valid CIDR block`);
    }
    const address = (((octets[0] ?? 0) << 24) >>> 0) + ((octets[1] ?? 0) << 16) + ((octets[2] ?? 0) << 8) + (octets[3] ?? 0);
    const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
    if (((address & mask) >>> 0) !== address) throw new InstallerValidationError(`CIDR Value: ${cidr} is invalid. Please enter a valid CIDR block`);
  }
}

function filterValue(value: unknown, param: RawParam, values: Record<string, unknown>): unknown {
  if (param.name === "cluster_name") {
    const token = String(value ?? "").trim().toLowerCase().replace(/^idea-/, "");
    return token === "" ? "" : `idea-${token}`;
  }
  if (param.name === "client_ip") {
    return String(value).split(",").map((entry) => entry.trim()).filter((entry) => entry !== "")
      .map((entry) => entry.includes("/") ? entry : `${entry}/32`);
  }
  if (param.name === "prefix_list_ids") {
    return String(value).split(",").map((entry) => entry.trim()).filter((entry) => entry !== "");
  }
  if (param.name === "vpc_id") values["use_existing_vpc"] = true;
  if (param.name === "existing_apps_fs_id") values["use_existing_apps_fs"] = true;
  if (param.name === "existing_data_fs_id") values["use_existing_data_fs"] = true;
  if (param.name === "opensearch_domain_endpoint") values["use_existing_opensearch_cluster"] = true;
  if (param.name === "directory_id") values["use_existing_directory_service"] = true;
  return value;
}

function validateCustomRules(
  rawValue: unknown,
  value: unknown,
  param: RawParam,
  values: Readonly<Record<string, unknown>>,
  options: InstallerRunOptions,
): void {
  if (param.name === "cluster_name" && typeof value === "string") {
    if (value === "idea-other") throw new InstallerValidationError(`Invalid ClusterName: ${value}. "other" is a reserved keyword and cannot be used in ClusterName.`);
    if (value.replace(/^idea-/, "").includes("idea")) throw new InstallerValidationError(`Invalid ClusterName: ${value}. "${value}" contains value "idea" and is not allowed.`);
    if (value.length < 8 || value.length > 11) throw new InstallerValidationError(`ClusterName: (${value}) length must be between 8 and 11 characters. Current: ${value.length}`);
    if (options.regenerate !== true && options.choices?.clusterNames?.().includes(value)) {
      throw new InstallerValidationError(`Cluster: (${value}) already exists and is in use.`);
    }
  }
  if (param.name === "client_ip" && typeof rawValue === "string") validateCidr(rawValue);
  if (param.name === "vpc_cidr_block" && typeof value === "string" && options.regenerate !== true &&
    options.choices?.vpcCidrs?.().includes(value)) {
    throw new InstallerValidationError(`VPC CIDR Block: ${value} is already used by an existing VPC. Please enter a different CIDR block to avoid IP Address conflicts between VPCs.`);
  }
  if (param.name === "enabled_modules" && Array.isArray(value)) {
    // The container stack builds one service per control-plane role and reads each role's module
    // id, so a selection without these fails at synthesis with a module lookup rather than with
    // anything an operator can act on.
    const missing = CONTAINER_REQUIRED_MODULES.filter((module) => !value.includes(module));
    if (missing.length > 0) {
      throw new InstallerValidationError(
        `The container control plane runs these modules as tasks, so they are part of every new cluster. Missing: ${missing.join(", ")}`,
      );
    }
  }
  if (param.name === "existing_resources" && Array.isArray(value) &&
    !value.includes("subnets:public") && !value.includes("subnets:private")) {
    throw new InstallerValidationError("Either one of [Subnets: Public, Subnets: Private] is required");
  }
  if ((param.name === "private_subnet_ids" || param.name === "public_subnet_ids") && Array.isArray(value)) {
    const vpcId = values["vpc_id"];
    const subnets = typeof vpcId === "string" ? options.choices?.subnets?.(vpcId) : undefined;
    if (subnets !== undefined) {
      const selected = subnets.filter((subnet) => value.includes(subnet.id));
      const isOutpost = selected.some((subnet) => subnet.isOutpost === true);
      const zones = new Set<string>();
      for (const subnet of selected) {
        if (!isOutpost && zones.has(subnet.availabilityZone)) throw new InstallerValidationError("Multiple subnet selection from the same Availability Zone is not supported unless using AWS Outposts.");
        zones.add(subnet.availabilityZone);
      }
      const otherName = param.name === "private_subnet_ids" ? "public_subnet_ids" : "private_subnet_ids";
      const other = values[otherName];
      if (Array.isArray(other) && selected.some((subnet) => other.includes(subnet.id))) {
        throw new InstallerValidationError(`SubnetId is already selected as part of ${otherName.replace(/_/g, " ")} selection.`);
      }
      if (param.name === "private_subnet_ids" && selected.length < 2 && !isOutpost) {
        throw new InstallerValidationError("Minimum 2 subnet selections are required to ensure high availability.");
      }
    }
  }
}

/** Runs the selected YAML module and returns the values map that Python writes as `values.yml`. */
export async function collectInstallerValues(options: InstallerRunOptions): Promise<Record<string, unknown>> {
  const declaration = loadSpec(options.inputParamsFile ?? resourcePath("input_params/install_params.yml"));
  const moduleName = options.existingResources === true ? "install-idea-using-existing-resources" : "install-idea";
  const module = declaration.modules.find((entry) => entry.name === moduleName);
  if (module === undefined) throw new InstallerValidationError(`installer module not found: ${moduleName}`);

  const providedAnswers = options.answers ?? {};
  const values: Record<string, unknown> = {
    _regenerate: options.regenerate === true,
    [CONTAINER_MODULE_VALUES_KEY]: true,
  };
  for (const section of module.sections) {
    for (const reference of section.params) {
      const name = reference.name;
      const param = declaration.params.get(name);
      if (param === undefined) throw new InstallerValidationError(`installer parameter not found: ${name}`);
      if (!conditionMatches(param.when, values)) continue;

      const choices = declaredChoices(param, options, values);
      const fallback = defaultValue(param, choices, values);
      const question: InstallerQuestion = {
        name,
        title: param.title ?? name,
        description: param.description ?? "",
        promptType: promptType(param),
        multiple: param.multiple === true,
        defaultValue: fallback,
        choices,
        helpText: param.help_text === null ? undefined : param.help_text,
      };

      for (let attempt = 1; ; attempt += 1) {
        try {
          const answered = Object.hasOwn(providedAnswers, name) ? providedAnswers[name] : await options.driver.ask(question);
          const normalised = normaliseAnswer(answered === undefined ? fallback : answered, param);
          validateDeclaredRules(normalised, param, choices);
          const validationValue = param.name === "cluster_name"
            ? filterValue(normalised, param, {})
            : normalised;
          validateCustomRules(answered === undefined ? fallback : answered, validationValue, param, values, options);
          values[name] = filterValue(normalised, param, values);
          break;
        } catch (error) {
          if (Object.hasOwn(providedAnswers, name)) throw error;
          // Re-asking is for a person who can correct the answer. A driver that cannot be
          // corrected returns the same answer forever, and an unbounded retry turns that into a
          // hang with no output rather than the validation message. The cap is far above what
          // anyone types at one question.
          if (attempt >= MAX_PROMPT_ATTEMPTS) throw error;
          options.driver.report(error instanceof Error ? error.message : String(error));
        }
      }
    }
    if (section.name === "aws-account") {
      const identity = await options.identity(values);
      values["aws_partition"] = identity.partition;
      values["aws_account_id"] = identity.accountId;
      values["aws_dns_suffix"] = identity.dnsSuffix;
    }
  }
  return values;
}
