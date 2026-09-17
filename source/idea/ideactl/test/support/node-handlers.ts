/**
 * The handler change applied to thirteen deployed functions: the implementation is the TypeScript
 * port under `src/lambda`, so the function runs `index.handler` on Node 22 and carries the one
 * `AwsSolutions-L1` suppression that names that runtime. The logical id, the function name and
 * every other property are unchanged, which is what makes it an in-place update.
 *
 * A test that holds a synthesized template against a deployed one runs the deployed side through
 * here, exactly as it does for the retain policy. It then still asserts everything else resource
 * for resource. The itemised gate is in `tools/parity/intended-drift.ts`, which reads the same
 * table and fails if a difference stops being produced.
 */

import { NODE_HANDLERS } from '../../tools/parity/node-handlers.ts';

type Json = Record<string, any>;

export const NODE_RUNTIME = 'nodejs22.x';
export const NODE_LAMBDA_HANDLER = 'index.handler';
export const NODE_NAG_SUPPRESSION: [string, string] = [
  'AwsSolutions-L1',
  'Node 22 is the runtime the deploy tool is built and tested with.',
];

/** The deployed template with the ported handlers moved to Node, for comparison against a synthesis. */
export function withNodeHandlers<T extends Json>(template: T): T {
  const copy = structuredClone(template) as Json;
  const resources = (copy.Resources ?? {}) as Json;
  for (const handler of NODE_HANDLERS) {
    const resource = resources[handler.logicalId] as Json | undefined;
    if (resource === undefined || resource.Type !== 'AWS::Lambda::Function') continue;
    resource.Properties.Runtime = NODE_RUNTIME;
    resource.Properties.Handler = NODE_LAMBDA_HANDLER;
    if (resource.Metadata?.cdk_nag?.rules_to_suppress === undefined) continue;
    resource.Metadata.cdk_nag.rules_to_suppress = [
      { reason: NODE_NAG_SUPPRESSION[1], id: NODE_NAG_SUPPRESSION[0] },
    ];
  }
  return copy as T;
}
