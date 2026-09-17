/**
 * What this wave stops emitting, applied to a deployed template so a test can hold it against a
 * synthesis: the retired resources, the settings row that named one of them, and the secret that
 * now reads its value off the user pool client instead of a `Custom::GetOAuthCredentials`.
 *
 * The declaration is the one the parity gate itemises from, so a retirement added there and
 * forgotten here is not possible. The new secret reference is derived from the deployed template
 * alone: the retired resource names the client it read in its own `ClientId`, so nothing is copied
 * across from the synthesis being checked.
 */

import { retiredLogicalIds, retiredSettingKey } from '../../tools/parity/intended-drift.ts';

type Json = Record<string, any>;

const OAUTH_CREDENTIALS_TYPE = 'Custom::GetOAuthCredentials';
const CLIENT_SECRET_ATTRIBUTE = 'ClientSecret';

/** The deployed template with this branch's retirements applied. */
export function withRetirements<T extends Json>(stack: string, template: T): T {
  const copy = structuredClone(template) as Json;
  const resources = (copy.Resources ?? {}) as Json;

  const clientOfCredentials = new Map<string, string>();
  for (const [id, resource] of Object.entries(resources)) {
    if ((resource as Json)?.Type !== OAUTH_CREDENTIALS_TYPE) continue;
    const client = (resource as Json).Properties?.ClientId?.Ref;
    if (typeof client === 'string') clientOfCredentials.set(id, client);
  }
  for (const resource of Object.values(resources)) {
    const getAtt = (resource as Json)?.Properties?.SecretString?.['Fn::GetAtt'];
    if (!Array.isArray(getAtt) || getAtt[1] !== CLIENT_SECRET_ATTRIBUTE) continue;
    const client = clientOfCredentials.get(getAtt[0] as string);
    if (client !== undefined) getAtt[0] = client;
  }

  for (const id of retiredLogicalIds(stack, copy as never)) delete resources[id];

  const settingKey = retiredSettingKey(stack);
  if (settingKey !== undefined) {
    for (const resource of Object.values(resources)) {
      if ((resource as Json)?.Type !== 'Custom::ClusterSettings') continue;
      delete (resource as Json).Properties?.settings?.[settingKey];
    }
  }
  return copy as T;
}
