/**
 * Shared fakes for the CLI tests.
 *
 * Nothing in these tests reaches AWS, the network, a CDK CLI, or the real `~/.idea`: every effect
 * comes through `Deps`, and `IDEA_USER_HOME` points at a temporary directory.
 */

import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import type {
  ChangeSetDescription,
  Deps,
  PromptChoice,
  StackDescription,
} from '../../src/cli/cdk-invoker.ts';
import type { ScanPage } from '../../src/config/cluster-config.ts';

export interface FakeDeps extends Deps {
  /** Every argv passed to `spawn`, in order. */
  spawns: string[][];
  /** Everything written to stdout and stderr. */
  stdout: string[];
  stderr: string[];
  /** `executeChangeSet` calls. Empty means the guard refused or there was nothing to do. */
  executed: Array<{ StackName: string; ChangeSetName: string }>;
  /** Objects written to S3, keyed `<bucket>/<key>`. */
  puts: Map<string, Uint8Array | string>;
  /** Milliseconds handed to `sleep`. */
  sleeps: number[];
  /** The `NextToken` of each `DescribeChangeSet` call, so a pagination loop can be observed. */
  describeChangeSetCalls: (string | undefined)[];
  /** Config writes, in order. */
  writes: Array<{ op: string; payload: unknown }>;
}

export interface FakeDepsOptions {
  changeSet?: ChangeSetDescription;
  /** Successive pages of a paginated `DescribeChangeSet`, indexed by the token handed back. */
  changeSetPages?: ChangeSetDescription[];
  stack?: StackDescription;
  /** Table name -> the rows a scan returns. */
  tables?: Record<string, Array<Record<string, unknown>>>;
  /** Exit code per spawn, consumed in order; the default is 0 for every spawn. */
  spawnExitCodes?: number[];
  /** Answers consumed in order by `prompt`. */
  answers?: Array<string | boolean>;
  httpStatus?: (url: string) => number;
  getObject?: (input: { Bucket: string; Key: string }) => string;
  now?: () => number;
  bootstrapContext?: Deps['bootstrapContext'];
}

/** A temporary `IDEA_USER_HOME`, restored by the returned function. */
export function withTempIdeaHome(): { home: string; restore: () => void } {
  const previous = process.env.IDEA_USER_HOME;
  const home = mkdtempSync(join(tmpdir(), 'ideactl-cli-home-'));
  process.env.IDEA_USER_HOME = home;
  return {
    home,
    restore: () => {
      if (previous === undefined) delete process.env.IDEA_USER_HOME;
      else process.env.IDEA_USER_HOME = previous;
      rmSync(home, { recursive: true, force: true });
    },
  };
}

export function fakeDeps(options: FakeDepsOptions = {}): FakeDeps {
  const spawns: string[][] = [];
  const stdout: string[] = [];
  const stderr: string[] = [];
  const executed: Array<{ StackName: string; ChangeSetName: string }> = [];
  const puts = new Map<string, Uint8Array | string>();
  const sleeps: number[] = [];
  const writes: Array<{ op: string; payload: unknown }> = [];
  const describeChangeSetCalls: (string | undefined)[] = [];
  const exitCodes = [...(options.spawnExitCodes ?? [])];
  const answers = [...(options.answers ?? [])];
  let clock = options.now === undefined ? 0 : undefined;

  const deps: FakeDeps = {
    spawns,
    stdout,
    stderr,
    executed,
    puts,
    sleeps,
    writes,
    describeChangeSetCalls,
    spawn: async (argv) => {
      spawns.push(argv);
      return exitCodes.shift() ?? 0;
    },
    scan: async (input): Promise<ScanPage> => ({ Items: options.tables?.[input.TableName] ?? [] }),
    cfn: {
      // `changeSetPages` models a paginated reply: the guard must follow every page, because a
      // replacement announced only on page two is exactly what it exists to refuse. The fake keys
      // off the token it is handed, so a loop that stops after the first call sees only page one.
      describeChangeSet: async (input) => {
        const pages = options.changeSetPages;
        if (pages !== undefined) {
          const index = input.NextToken === undefined ? 0 : Number(input.NextToken);
          const page = pages[index];
          if (page === undefined) throw new Error(`no change-set page for token ${String(input.NextToken)}`);
          describeChangeSetCalls.push(input.NextToken);
          return page;
        }
        return options.changeSet ?? { Status: 'CREATE_COMPLETE', Changes: [] };
      },
      executeChangeSet: async (input) => {
        executed.push(input);
      },
      describeStack: async () => options.stack ?? { StackStatus: 'UPDATE_COMPLETE', Outputs: [] },
    },
    s3: {
      putObject: async (input) => {
        puts.set(`${input.Bucket}/${input.Key}`, input.Body);
      },
      getObject: async (input) => {
        if (options.getObject === undefined) throw new Error('NoSuchKey');
        return options.getObject(input);
      },
    },
    configWriter: async () => ({
      syncModulesInDb: async (modules) => {
        writes.push({ op: 'syncModulesInDb', payload: modules });
      },
      syncClusterSettingsInDb: async (entries, overwrite) => {
        writes.push({ op: 'syncClusterSettingsInDb', payload: { entries, overwrite } });
      },
      setConfigEntry: async (key, value) => {
        writes.push({ op: 'setConfigEntry', payload: { key, value } });
      },
      deleteConfigEntries: async (prefix) => {
        writes.push({ op: 'deleteConfigEntries', payload: prefix });
      },
    }),
    accountId: async () => '123456789012',
    httpStatus: async (url) => options.httpStatus?.(url) ?? 200,
    sleep: async (ms) => {
      sleeps.push(ms);
      if (clock !== undefined) clock += ms;
    },
    now: options.now ?? (() => clock ?? 0),
    uuid: () => '00000000-0000-4000-8000-000000000000',
    out: (line) => stdout.push(line),
    err: (line) => stderr.push(line),
    prompt: async (_choice: PromptChoice) => answers.shift() ?? true,
    bootstrapContext: options.bootstrapContext,
  };
  return deps;
}

/** One `ResourceChange`, as `DescribeChangeSet` returns it. */
export function change(
  action: string,
  logicalId: string,
  resourceType: string,
  replacement?: string,
): { ResourceChange: { Action: string; LogicalResourceId: string; ResourceType: string; Replacement?: string } } {
  return {
    ResourceChange: {
      Action: action,
      LogicalResourceId: logicalId,
      ResourceType: resourceType,
      ...(replacement === undefined ? {} : { Replacement: replacement }),
    },
  };
}

/** A `<cluster>.modules` scan row. */
export function moduleRow(
  moduleId: string,
  name: string,
  type: string,
  status = 'not-deployed',
): Record<string, unknown> {
  return { module_id: moduleId, name, type, status, stack_name: `sample-cluster-${moduleId}`, version: '26.09.0' };
}
