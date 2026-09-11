import { createHash } from 'node:crypto';

// Port of aws-cdk-lib/core/lib/private/uniqueid.ts (2.265.0). The construct path below the stack
// goes in, the CloudFormation logical id comes out. Any deviation is a template diff.

const HIDDEN_FROM_HUMAN_ID = 'Resource';
const HIDDEN_ID = 'Default';
const PATH_SEP = '/';
const HASH_LEN = 8;
const MAX_HUMAN_LEN = 240;
const MAX_ID_LEN = 255;

export function makeUniqueId(pathComponents: string[]): string {
  const components = pathComponents.filter((x) => x !== HIDDEN_ID);
  if (components.length === 0) {
    throw new Error('Unable to calculate a unique id for an empty set of components');
  }
  // The reference refuses unresolved tokens before hashing; a construct id built from a token would
  // otherwise hash the placeholder text.
  const unresolved = components.filter((c) => c.includes('${Token['));
  if (unresolved.length > 0) {
    throw new Error(`ID components may not include unresolved tokens: ${unresolved.join(',')}`);
  }

  // Single component: no hash, so `Default` siblings collide loudly rather than silently.
  if (components.length === 1) {
    const candidate = removeNonAlphanumeric(components[0]);
    if (candidate.length <= MAX_ID_LEN) {
      return candidate;
    }
  }

  const hash = pathHash(components);
  const human = removeDupes(components)
    .filter((x) => x !== HIDDEN_FROM_HUMAN_ID)
    .map(removeNonAlphanumeric)
    .join('')
    .slice(0, MAX_HUMAN_LEN);
  return human + hash;
}

function pathHash(path: string[]): string {
  return createHash('md5').update(path.join(PATH_SEP), 'utf8').digest('hex').slice(0, HASH_LEN).toUpperCase();
}

function removeNonAlphanumeric(s: string): string {
  return s.replace(/[^A-Za-z0-9]/g, '');
}

/** Drops a component when the previous one already ends with it (not just exact duplicates). */
function removeDupes(path: string[]): string[] {
  const ret: string[] = [];
  for (const component of path) {
    if (ret.length === 0 || !ret[ret.length - 1].endsWith(component)) {
      ret.push(component);
    }
  }
  return ret;
}
