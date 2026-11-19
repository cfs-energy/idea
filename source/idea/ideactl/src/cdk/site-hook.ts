/**
 * Site hook checksums (`<module>.*site_hook_sha256`): empty, or a SHA-256 as 64 hex characters,
 * case ignored. A set value pins the hook; a wrong-shaped one is refused at deploy rather than
 * reaching hosts, where it would stop every bootstrap with site-hook-mismatch.
 */

import { Annotations } from 'aws-cdk-lib';
import type { IConstruct } from 'constructs';
import type { ClusterConfig } from '../config/cluster-config.ts';

export const HOST_SITE_HOOK_KEYS = {
  scheduler: ['scheduler.compute_node_site_hook_sha256', 'scheduler.ami_builder_site_hook_sha256'],
  'virtual-desktop-controller': [
    'virtual-desktop-controller.dcv_host_site_hook_sha256',
    'virtual-desktop-controller.ami_builder_site_hook_sha256',
  ],
} as const;

/**
 * A row written under the module NAME (`virtual-desktop-controller.…`) where the module id differs
 * (`vdc` by default) is read by nothing: `config set` writes keys literally and every reader maps
 * the name to the id. Returned so the deploy can say so.
 */
export function unreadSiteHookRow(config: Pick<ClusterConfig, 'getString' | 'getRealKey'>, key: string): string | undefined {
  const moduleName = key.split('.')[0]!;
  if (config.getRealKey(key) === key) return undefined;
  const literal = config.getString(key, '', { moduleId: moduleName });
  return literal === '' || literal === undefined ? undefined : config.getRealKey(key);
}

/**
 * The setting's value, trimmed and lowercased as every host compares it, after refusing anything
 * but empty or 64 hex characters.
 */
export function siteHookDigest(config: Pick<ClusterConfig, 'getString'>, key: string): string {
  const value = (config.getString(key, '') ?? '').trim().toLowerCase();
  if (value !== '' && !/^[0-9a-f]{64}$/.test(value)) {
    throw new Error(`${key} must be empty or a 64-character hex SHA-256, got ${JSON.stringify(value)}`);
  }
  return value;
}

/** Checks each setting's shape and warns, at synth, about a row set under a form nothing reads. */
export function checkSiteHookSettings(config: Pick<ClusterConfig, 'getString' | 'getRealKey'>, scope: IConstruct, keys: readonly string[]): void {
  for (const key of keys) {
    siteHookDigest(config, key);
    const readKey = unreadSiteHookRow(config, key);
    if (readKey === undefined) continue;
    const message = `${key} is set, but nothing reads that row: the module id is ${readKey.split('.')[0]}. Set ${readKey} instead.`;
    Annotations.of(scope).addWarningV2('idea:siteHookRowUnread', message);
    console.error(`warning: ${message}`);
  }
}
