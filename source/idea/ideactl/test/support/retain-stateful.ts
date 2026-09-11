/**
 * The one policy change applied to every stack: `UpdateReplacePolicy: Retain` on every
 * stateful resource, so an update that forces a replacement orphans the old resource rather than
 * deleting it. `DeletionPolicy` is untouched.
 *
 * A test that holds a synthesized template against a deployed one runs the deployed side through
 * here. It then still asserts everything else resource for resource, including every
 * `DeletionPolicy` and every policy on a resource that is not stateful.
 *
 * This helper shares its predicate with the code it is checking, so it cannot notice a type
 * dropped from that predicate. The assertions that can are in `test/retain-stateful`, which
 * carries its own list.
 */

import { isStatefulType } from '../../src/cdk/stateful.ts';

type Json = Record<string, any>;

/** The deployed template with the retain policy applied, for comparison against a synthesis. */
export function withRetainedStateful<T extends Json>(template: T): T {
  const copy = structuredClone(template) as Json;
  for (const resource of Object.values((copy.Resources ?? {}) as Json)) {
    if (isStatefulType((resource as Json)?.Type)) (resource as Json).UpdateReplacePolicy = 'Retain';
  }
  return copy as T;
}

/** What a resource of this type carries now, given what the deployed template carried. */
export function expectedUpdateReplace(resourceType: string, deployed: string | undefined): string | undefined {
  return isStatefulType(resourceType) ? 'Retain' : deployed;
}
