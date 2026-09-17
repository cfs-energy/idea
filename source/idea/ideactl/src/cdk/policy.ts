/**
 * Renders IAM policy documents from `resources/policies/*.yml` Jinja templates.
 *
 * Templates receive a `context` with cluster values, config, ARNs, and utilities.
 * Facades map snake_case names to camelCase members and bind keyword arguments.
 */

import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { load } from 'js-yaml';
import type nunjucks from 'nunjucks';

import { ArnBuilder } from '../config/arn-builder.ts';
import type { ClusterConfig } from '../config/cluster-config.ts';
import { jinjaEnv, renderTemplate, toYaml } from '../config/jinja.ts';

export interface PolicyVars {
  config: ClusterConfig;
  /** `Policy(module_id=...)`; only cluster-manager.yml, compute-node.yml and scheduler.yml read it. */
  moduleId?: string;
  /** `Policy(vars=SocaAnyPayload(...))`, reached as `context.vars.<name>`. */
  vars?: Record<string, unknown>;
  /** Override for the `resources/policies` directory; defaults to the packaged one. */
  policiesDir?: string;
}

const HERE = dirname(fileURLToPath(import.meta.url));

/** The package `resources/` directory: beside `src/` in a checkout, beside `dist/src/` in a build. */
export function resourcesDir(): string {
  const resources = join(HERE, '..', '..', 'resources');
  if (!existsSync(resources)) throw new Error(`resources directory not found: ${resources}`);
  return resources;
}

/** Binds trailing keyword arguments collected by the renderer. */
function bindKeywords(args: unknown[], params?: readonly string[]): unknown[] {
  const last = args[args.length - 1] as Record<string, unknown> | undefined;
  if (last === null || typeof last !== 'object' || last.__keywords !== true) return args;
  const positional = args.slice(0, -1);
  for (const [name, value] of Object.entries(last)) {
    if (name === '__keywords') continue;
    const index = params?.indexOf(name) ?? -1;
    if (index < 0) throw new Error(`unexpected keyword argument '${name}'`);
    while (positional.length < index) positional.push(undefined);
    positional[index] = value;
  }
  return positional;
}

const snakeToCamel = (name: string): string => name.replace(/_([a-z0-9])/g, (_match, c: string) => c.toUpperCase());

/** Keyword parameter order for the ARN builder member called with keywords. */
const ARN_KEYWORDS: Record<string, readonly string[]> = {
  get_arn: ['service', 'resource', 'aws_account_id', 'aws_region'],
};

function snakeCaseFacade(target: object, keywords: Record<string, readonly string[]>): object {
  return new Proxy(Object.create(null) as Record<string, unknown>, {
    get(_holder, property) {
      if (typeof property !== 'string') return undefined;
      const member = (target as Record<string, unknown>)[snakeToCamel(property)];
      if (typeof member !== 'function') return member;
      return (...args: unknown[]) => member.apply(target, bindKeywords(args, keywords[property]));
    },
  });
}

/** Exposes configuration getters to templates. */
function configFacade(config: ClusterConfig): object {
  const split = (args: unknown[]) => {
    const last = args[args.length - 1] as Record<string, unknown> | undefined;
    const keywords = last !== null && typeof last === 'object' && last.__keywords === true ? last : {};
    const positional = keywords === last ? args.slice(0, -1) : args;
    return {
      key: positional[0] as string,
      fallback: positional.length > 1 ? positional[1] : keywords.default,
      options: { required: keywords.required === true },
    };
  };
  return {
    get_string: (...args: unknown[]) => {
      const { key, fallback, options } = split(args);
      return config.getString(key, fallback as string, options);
    },
    get_bool: (...args: unknown[]) => {
      const { key, fallback, options } = split(args);
      return config.getBool(key, fallback as boolean, options);
    },
    get_int: (...args: unknown[]) => {
      const { key, fallback, options } = split(args);
      return config.getInt(key, fallback as number, options);
    },
    get_list: (...args: unknown[]) => {
      const { key, fallback, options } = split(args);
      const list = config.getList(key, fallback as unknown[], options);
      // Empty lists must be falsy in template conditions and iterate zero times.
      return list === undefined || list.length === 0 ? undefined : list;
    },
    get_module_id: (moduleName: string) => config.moduleId(moduleName),
    is_module_enabled: (moduleName: string) => config.isModuleEnabled(moduleName),
  };
}

const envCache = new Map<string, nunjucks.Environment>();

function policyEnv(policiesDir: string): nunjucks.Environment {
  let env = envCache.get(policiesDir);
  if (env === undefined) {
    env = jinjaEnv(policiesDir);
    envCache.set(policiesDir, env);
  }
  return env;
}

/** Builds the context object used to render policy templates. */
export function policyContext(policyVars: PolicyVars): object {
  const { config } = policyVars;
  return {
    cluster_name: config.getString('cluster.cluster_name'),
    module_id: policyVars.moduleId ?? null,
    aws_region: config.getString('cluster.aws.region', undefined, { required: true }),
    aws_dns_suffix: config.getString('cluster.aws.dns_suffix', undefined, { required: true }),
    aws_partition: config.getString('cluster.aws.partition', undefined, { required: true }),
    aws_account_id: config.getString('cluster.aws.account_id', undefined, { required: true }),
    config: configFacade(config),
    arns: snakeCaseFacade(new ArnBuilder(config), ARN_KEYWORDS),
    vars: policyVars.vars ?? {},
    utils: { to_yaml: toYaml },
  };
}

/** Renders and parses a policy document for `PolicyDocument.fromJson`. */
export function renderPolicy(policyName: string, policyVars: PolicyVars): object {
  const dir = policyVars.policiesDir ?? join(resourcesDir(), 'policies');
  const text = renderTemplate(policyEnv(dir), policyName, { context: policyContext(policyVars) });
  try {
    // Include the template name in the parser error.
    return load(text, { filename: policyName }) as object;
  } catch (error) {
    // Report numbered source content and preserve the parser error.
    const numbered = text.split('\n').map((line, index) => `${String(index + 1).padStart(5)}: ${line}`);
    console.error(`failed to decode policy json: ${policyName} - ${String(error)}. Content:\n${numbered.join('\n')}`);
    throw error;
  }
}
