/**
 * Port of `check-cluster-status`, `list-modules` and `show-connection-info`
 * (`app_main.py:1301-1601`).
 */

import { request } from 'node:https';

import type { Command } from 'commander';

import { ClusterConfig, isEmpty, type ModuleInfo } from '../../config/cluster-config.ts';
import { ExitWithCode, type Deps } from '../cdk-invoker.ts';
import { ideaVersion } from '../../version.ts';
import { renderTable } from './config.ts';

const MODULE_TYPE_APP = 'app';

/**
 * `check_cluster_status`'s endpoint list: the analytics dashboard, then one `/healthcheck` per app
 * module, in modules-table order.
 */
export function statusEndpoints(config: ClusterConfig): Array<{ name: string; endpoint: string }> {
  const clusterEndpoint = config.getClusterExternalEndpoint();
  const endpoints: Array<{ name: string; endpoint: string }> = [];
  for (const module of config.modules()) {
    if (module.name === 'analytics') {
      endpoints.push({ name: 'OpenSearch Service Dashboard', endpoint: `${clusterEndpoint}/_dashboards/` });
    } else if (module.type === MODULE_TYPE_APP) {
      endpoints.push({
        name: module.title ?? module.name,
        endpoint: `${clusterEndpoint}/${module.module_id}/healthcheck`,
      });
    }
  }
  return endpoints;
}

/** Requests without a User-Agent are answered 403 by the load balancer. */
export const PROBE_USER_AGENT = `ideactl/${ideaVersion()}`;

/** `requests.get` follows up to 30 redirects; the dashboard endpoint answers one. */
const MAX_PROBE_REDIRECTS = 30;

/**
 * The cluster's own certificate is usually self-signed, so this does not verify it. That matches
 * `requests.get(url, verify=False)`.
 *
 * Two of that call's defaults have to be supplied by hand here. It sends a User-Agent, and the
 * external load balancer answers 403 when a request carries none, so a probe without one reports
 * every healthy endpoint as failing. It also follows redirects, and the analytics dashboard answers
 * `/_dashboards/` with a 302 to `/_dashboards/app/home`, so a probe that stops at the first response
 * reports a working dashboard as failing.
 */
export const liveHttpStatus = (url: string, redirectsLeft = MAX_PROBE_REDIRECTS): Promise<number> =>
  new Promise((resolve) => {
    const req = request(
      url,
      { rejectUnauthorized: false, method: 'GET', headers: { 'user-agent': PROBE_USER_AGENT } },
      (response) => {
        response.resume();
        const status = response.statusCode ?? 0;
        const location = response.headers.location;
        if (status >= 300 && status < 400 && location !== undefined && redirectsLeft > 0) {
          resolve(liveHttpStatus(new URL(location, url).toString(), redirectsLeft - 1));
          return;
        }
        resolve(status);
      },
    );
    req.on('error', () => resolve(0));
    req.end();
  });

export interface CheckStatusOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  wait?: boolean;
  waitTimeout?: number;
  debug?: boolean;
  moduleSet: string;
}

/** Loops every 60 s while `--wait`; exits 1 when any endpoint is still failing. */
export async function checkClusterStatus(deps: Deps, options: CheckStatusOptions): Promise<number> {
  const config = await ClusterConfig.fromDynamoDb(options.clusterName, options.awsRegion, {
    moduleSet: options.moduleSet,
    scan: deps.scan,
  });
  const clusterEndpoint = config.getClusterExternalEndpoint();
  const endpoints = statusEndpoints(config);

  const endTime = deps.now() + (options.waitTimeout ?? 900) * 1000;
  let failCount = 0;
  let currentTime = deps.now();

  while (currentTime < endTime) {
    deps.out(`checking endpoint status for cluster: ${options.clusterName}, url: ${clusterEndpoint} ...`);
    failCount = 0;
    const rows: string[][] = [];
    for (const endpoint of endpoints) {
      const status = await deps.httpStatus(endpoint.endpoint);
      if (options.debug === true) deps.out(`${endpoint.endpoint} - ${status}`);
      const success = status === 200;
      if (!success) failCount += 1;
      rows.push([endpoint.name, endpoint.endpoint, success ? 'SUCCESS' : 'FAIL']);
    }
    deps.out(renderTable(['Module', 'Endpoint', 'Status'], rows));

    if (options.wait !== true) break;
    if (failCount === 0) break;
    deps.out('failed to verify all cluster endpoints. wait ... (Press Ctrl + C to exit) ');
    await deps.sleep(60_000);
    currentTime = deps.now();
  }

  if (options.wait === true && currentTime >= endTime) {
    deps.err(
      "check endpoint status timed-out. please verify your cluster's External ALB Security Group " +
        'configuration and check correct ingress rules have been configured.',
    );
  }
  if (failCount > 0) throw new ExitWithCode(1);
  return failCount;
}

/** `list_modules`: Title / Name / Module ID / Type / Stack Name / Version / Status. */
export function modulesTable(modules: ModuleInfo[]): string {
  return renderTable(
    ['Title', 'Name', 'Module ID', 'Type', 'Stack Name', 'Version', 'Status'],
    modules.map((module) => [
      module.title ?? '',
      module.name,
      module.module_id,
      module.type,
      module.stack_name ?? '-',
      module.version ?? '-',
      module.status ?? '',
    ]),
  );
}

/** `get_session_manager_url`; the console host differs per partition. */
export function sessionManagerUrl(awsPartition: string, awsRegion: string, instanceId: string): string {
  let consolePrefix = `${awsRegion}.`;
  let consoleSuffix = '.aws.amazon.com';
  if (awsPartition === 'aws-cn') {
    consolePrefix = '';
    consoleSuffix = '.amazonaws.cn';
  } else if (awsPartition === 'aws-us-gov') {
    consolePrefix = '';
    consoleSuffix = '.amazonaws-us-gov.com';
  }
  return `https://${consolePrefix}console${consoleSuffix}/systems-manager/session-manager/${instanceId}?region=${awsRegion}`;
}

/**
 * `show_connection_info`: only deployed modules contribute, and the entries print in the hardcoded
 * weight order (portal, bastion ssh, bastion session manager, analytics).
 */
export function connectionInfo(
  config: ClusterConfig,
  awsRegion: string,
): Array<{ key: string; value: string; weight: number }> {
  const entries: Array<{ key: string; value: string; weight: number }> = [];
  const clusterEndpoint = config.getClusterExternalEndpoint();
  if (isEmpty(clusterEndpoint)) return entries;

  for (const module of config.modules()) {
    if (module.status !== 'deployed') continue;
    if (module.name === 'cluster-manager') {
      entries.push({ key: 'Web Portal', value: clusterEndpoint, weight: 0 });
    } else if (module.name === 'analytics') {
      entries.push({ key: 'Analytics Dashboard', value: `${clusterEndpoint}/_dashboards`, weight: 3 });
    } else if (module.name === 'bastion-host') {
      const keyPairName = config.getString('cluster.network.ssh_key_pair');
      const ipAddress =
        config.getString(`${module.module_id}.public_ip`) ?? config.getString(`${module.module_id}.private_ip`);
      if (!isEmpty(ipAddress)) {
        // Read for its refusal: a bastion with no base_os is a broken module row, not a default.
        config.getString(`${module.module_id}.base_os`, undefined, { required: true });
        entries.push({
          key: 'Bastion Host (SSH Access)',
          // Every supported base OS uses the same login user.
          value: `ssh -i ~/.ssh/${keyPairName}.pem ec2-user@${ipAddress as string}`,
          weight: 1,
        });
      }
      const instanceId = config.getString(`${module.module_id}.instance_id`);
      if (!isEmpty(instanceId)) {
        const partition = config.getString('cluster.aws.partition', undefined, { required: true }) as string;
        entries.push({
          key: 'Bastion Host (Session Manager URL)',
          value: sessionManagerUrl(partition, awsRegion, instanceId as string),
          weight: 2,
        });
      }
    }
  }
  entries.sort((a, b) => a.weight - b.weight);
  return entries;
}

export function registerStatusCommands(program: Command, deps: Deps): void {
  program
    .command('check-cluster-status')
    .description('check status for all applicable cluster endpoints')
    .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
    .requiredOption('--aws-region <aws-region>', 'AWS Region')
    .option('--aws-profile <aws-profile>', 'AWS Profile Name')
    .option('--wait', 'Wait until all cluster endpoints are healthy.')
    .option('--wait-timeout <seconds>', 'Wait timeout in seconds. Default: 900 (15 mins)', (value) => Number.parseInt(value, 10), 900)
    .option('--debug', 'Print debug messages')
    .option('--module-set <module-set>', 'Name of the ModuleSet. Default: default', 'default')
    .action(async (options: CheckStatusOptions) => {
      await checkClusterStatus(deps, options);
    });

  program
    .command('list-modules')
    .description('list all modules for a cluster')
    .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
    .requiredOption('--aws-region <aws-region>', 'AWS Region')
    .option('--aws-profile <aws-profile>', 'AWS Profile Name')
    .action(async (options: { clusterName: string; awsRegion: string }) => {
      const config = await ClusterConfig.fromDynamoDb(options.clusterName, options.awsRegion, { scan: deps.scan });
      deps.out(modulesTable(config.modules()));
    });

  program
    .command('show-connection-info')
    .description('print cluster connection information')
    .requiredOption('--cluster-name <cluster-name>', 'Cluster Name')
    .requiredOption('--aws-region <aws-region>', 'AWS Region')
    .option('--aws-profile <aws-profile>', 'AWS Profile Name')
    .option('--module-set <module-set>', 'Name of the ModuleSet. Default: default', 'default')
    .action(async (options: { clusterName: string; awsRegion: string; moduleSet: string }) => {
      const config = await ClusterConfig.fromDynamoDb(options.clusterName, options.awsRegion, {
        moduleSet: options.moduleSet,
        scan: deps.scan,
      });
      const entries = connectionInfo(config, options.awsRegion);
      if (entries.length === 0) {
        deps.err(`No connection information found for cluster: ${options.clusterName}. Is the cluster deployed?`);
        return;
      }
      for (const entry of entries) deps.out(`${entry.key}: ${entry.value}`);
    });
}
