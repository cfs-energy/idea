/**
 * The certificate custom resources this branch keeps for one more release, defused: a Node handler
 * whose Delete does nothing, and `Retain` on both policies so nothing can destroy a certificate a
 * load balancer or a running host is serving.
 *
 * A test that holds a synthesized template against a deployed one runs the deployed side through
 * here, and then still asserts every other policy resource for resource. The set of resources
 * comes from the parity gate's own declaration, so the two cannot come to disagree.
 */

import {
  certificateLogicalIds,
  settingsLookup,
  withCertificateSettings,
} from '../../tools/parity/intended-drift.ts';

type Json = Record<string, any>;

/** The deployed template with the certificate resources retained, for comparison with a synthesis. */
export function withRetainedCertificates<T extends Json>(template: T): T {
  const copy = structuredClone(template) as Json;
  const resources = (copy.Resources ?? {}) as Json;
  for (const id of certificateLogicalIds(copy as never)) {
    (resources[id] as Json).DeletionPolicy = 'Retain';
    (resources[id] as Json).UpdateReplacePolicy = 'Retain';
  }
  return copy as T;
}

/**
 * The deployed template with every certificate attribute replaced by the captured settings row it
 * was published as, which is where the synthesis reads it from now. `settingsFile` is the captured
 * `<cluster>.cluster-settings` scan the stack is synthesized from.
 */
export function withCertificateRows<T extends Json>(template: T, settingsFile: string): T {
  return withCertificateSettings(template as never, settingsLookup(settingsFile)) as unknown as T;
}
