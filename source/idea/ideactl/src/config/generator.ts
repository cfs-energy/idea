/**
 * The config generator: `values.yml` -> rendered `config/<module_id>/*.yml` -> flat key/value
 * entries for the `<cluster>.cluster-settings` table.
 * Port of `ConfigGenerator.generate_config_from_templates`, `read_modules_from_files`,
 * `read_config_from_files` and `traverse_config` (`ideaadministrator/app/config_generator.py`).
 *
 * The rendered files are the operator-editable form and are never diffed textually: what has to
 * match Python byte for byte is the parsed result, so the YAML load has to agree with
 * `yaml.safe_load` on every scalar the templates emit.
 */

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import yaml from 'js-yaml';

import { GeneralException, isNullValue } from './cluster-config.ts';
import { jinjaEnv, renderTemplate, toYaml } from './jinja.ts';
import {
  buildContext,
  configTemplatesDirs,
  loadValuesFile,
  SUPPORTED_OS,
  type BuildContextOptions,
  type UserValues,
} from './values.ts';

export interface ModuleEntry {
  name: string;
  id: string;
  type: string;
  config_files: string[];
}

export interface ConfigEntry {
  key: string;
  value: unknown;
}

/**
 * `Utils.from_yaml` = PyYAML `safe_load`. The core schema is the closest js-yaml gets: it resolves
 * `~`/`null` and `true|True|false|False`, and unlike the default schema it leaves date-like and
 * sexagesimal-looking scalars as strings, which is what the templates' quoted values need.
 */
export function loadYaml(text: string): unknown {
  return yaml.load(text, { schema: yaml.CORE_SCHEMA });
}

export interface GenerateOptions extends BuildContextOptions {
  templatesDir?: string | string[];
}

const CONTAINER_MODULE: ModuleEntry = {
  name: 'ecs',
  id: 'ecs',
  type: 'stack',
  config_files: ['settings.yml'],
};

const MODULE_NAME_GLOBAL_SETTINGS = 'global-settings';

/** The two lines every module-set entry sits under. */
const MODULE_SETS_HEADER = 'module_sets:\n  default:\n';

/**
 * `idea.yml` in the administrator's tree does not list the container module, so it is spliced in
 * ahead of `cluster-manager`, where its deployment priority puts it. Ordering inside this list is
 * cosmetic (`deploymentOrder` sorts by priority) but it keeps the generated file readable.
 */
function withContainerModule(modules: ModuleEntry[]): ModuleEntry[] {
  if (modules.some((module) => module.id === CONTAINER_MODULE.id)) return modules;
  const before = modules.findIndex((module) => module.name === 'cluster-manager');
  if (before < 0) {
    throw new GeneralException('idea.yml lists no cluster-manager module to order ecs against');
  }
  return [...modules.slice(0, before), { ...CONTAINER_MODULE }, ...modules.slice(before)];
}

/**
 * Adds the container module's name-to-id mapping to the rendered default module set. The rendered
 * text is kept rather than reparsed and dumped so the operator's comments survive; the anchor is
 * the file's own two opening lines, and a template that no longer starts with them stops the run.
 */
function withContainerModuleSet(rendered: string): string {
  if (!rendered.startsWith(MODULE_SETS_HEADER)) {
    throw new GeneralException(
      `${MODULE_NAME_GLOBAL_SETTINGS}/settings.yml does not open with the default module set`,
    );
  }
  return rendered.replace(
    MODULE_SETS_HEADER,
    `${MODULE_SETS_HEADER}    ${CONTAINER_MODULE.id}:\n      module_id: ${CONTAINER_MODULE.id}\n`,
  );
}

/**
 * Renders every template into `configDir` and returns the module list from `idea.yml`.
 * Template lookup is by module NAME, output directory by module ID - they differ only for
 * `virtual-desktop-controller`, whose id is `vdc`.
 */
export function generateConfigFromTemplates(
  values: UserValues,
  configDir: string,
  options: GenerateOptions = {},
): ModuleEntry[] {
  const context = buildContext(values, options);
  const env = jinjaEnv(options.templatesDir ?? configTemplatesDirs());
  const renderContext = { ...context, utils: { to_yaml: toYaml } };
  const containers = context.enable_ecs === true;

  mkdirSync(configDir, { recursive: true });

  const ideaConfig = loadYaml(renderTemplate(env, 'idea.yml', renderContext)) as {
    modules: ModuleEntry[];
  };
  if (containers) ideaConfig.modules = withContainerModule(ideaConfig.modules);
  const modules = ideaConfig.modules;

  for (const module of modules) {
    for (const file of module.config_files) {
      let settings = renderTemplate(env, `${module.name}/${file}`, {
        ...renderContext,
        module_id: module.id,
        module_name: module.name,
        supported_base_os: SUPPORTED_OS,
      });
      if (containers && module.name === MODULE_NAME_GLOBAL_SETTINGS) {
        settings = withContainerModuleSet(settings);
      }
      const settingsFile = join(configDir, module.id, file);
      mkdirSync(dirname(settingsFile), { recursive: true });
      writeFileSync(settingsFile, settings);
    }
  }

  // idea.yml is written back as a normalised dump: no comments, no jinja, and it is this file
  // that `config update` and every later read parse.
  writeFileSync(join(configDir, 'idea.yml'), toYaml(ideaConfig));
  return modules;
}

/** Convenience wrapper: read `values.yml` from disk and generate. */
export function generateConfig(
  valuesFile: string,
  configDir: string,
  options: GenerateOptions = {},
): ModuleEntry[] {
  return generateConfigFromTemplates(loadValuesFile(valuesFile), configDir, options);
}

/**
 * Python indexes and spreads the parsed documents directly, so an empty or non-mapping file stops
 * the run with a `TypeError`. Silently treating one as `{}` would let an operator who truncated a
 * settings file push a config that is missing a whole module's keys.
 */
function asMapping(value: unknown, file: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new GeneralException(`${file}: expected a YAML mapping, got ${describeYaml(value)}`);
  }
  return value as Record<string, unknown>;
}

function describeYaml(value: unknown): string {
  if (value === null || value === undefined) return 'an empty document';
  if (Array.isArray(value)) return 'a list';
  return `a ${typeof value}`;
}

export function readModulesFromFiles(configDir: string): ModuleEntry[] {
  const file = join(configDir, 'idea.yml');
  const ideaConfig = asMapping(loadYaml(readFileSync(file, 'utf-8')), file);
  if (!Array.isArray(ideaConfig.modules)) {
    throw new GeneralException(`${file}: expected a modules list`);
  }
  return ideaConfig.modules as ModuleEntry[];
}

/**
 * The generated tree as one dict keyed by module id. A module's files are merged shallowly with
 * the later file winning - only `cluster` has two files, and their top-level keys are disjoint.
 */
export function readConfigFromFiles(configDir: string): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const module of readModulesFromFiles(configDir)) {
    let moduleSettings: Record<string, unknown> = {};
    for (const file of module.config_files) {
      const settingsFile = join(configDir, module.id, file);
      const settings = asMapping(loadYaml(readFileSync(settingsFile, 'utf-8')), settingsFile);
      moduleSettings = { ...moduleSettings, ...settings };
    }
    config[module.id] = moduleSettings;
  }
  return config;
}

/**
 * Flattens a nested config into dotted `key` / `value` entries.
 *
 * The null normalisation happens BEFORE the dict test, exactly as in Python: an empty dict is
 * therefore emitted as one NULL leaf rather than recursed into. An empty list stays a list, so it
 * reaches DynamoDB as `L` and reads back as `[]` instead of the default.
 */
export function traverseConfig(
  entries: ConfigEntry[],
  prefix: string,
  config: Record<string, unknown>,
  filterKeyPrefix?: string,
): void {
  for (const key of Object.keys(config)) {
    if (key.includes('.') || key.includes(':')) {
      throw new GeneralException(
        `Config key name: ${key} under: ${prefix} cannot contain a dot(.), colon(:) or comma(,)`,
      );
    }

    let value = config[key];
    if (isNullValue(value)) value = null;

    const pathPrefix = prefix.length > 0 ? `${prefix}.${key}` : key;

    if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
      traverseConfig(entries, pathPrefix, value as Record<string, unknown>, filterKeyPrefix);
    } else {
      if (filterKeyPrefix !== undefined && filterKeyPrefix.length > 0) {
        if (!pathPrefix.startsWith(filterKeyPrefix)) continue;
      }
      entries.push({ key: pathPrefix, value });
    }
  }
}

/** `convert_config_to_key_value_pairs`: the generated tree as flat entries, in template order. */
export function convertConfigToKeyValuePairs(
  configDir: string,
  keyPrefix?: string,
): ConfigEntry[] {
  const entries: ConfigEntry[] = [];
  traverseConfig(entries, '', readConfigFromFiles(configDir), keyPrefix);
  return entries;
}

/** The same entries as a plain object, which is the shape the golden fixtures are captured in. */
export function flattenConfigDir(configDir: string, keyPrefix?: string): Record<string, unknown> {
  const flat: Record<string, unknown> = {};
  for (const entry of convertConfigToKeyValuePairs(configDir, keyPrefix)) {
    flat[entry.key] = entry.value;
  }
  return flat;
}
