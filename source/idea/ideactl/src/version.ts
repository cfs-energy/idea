import { readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// IDEA_VERSION.txt sits at the repository root in a checkout and next to dist/ in the image.
export function ideaVersion(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  for (const candidate of [join(here, '..', 'IDEA_VERSION.txt'), join(here, '..', '..', '..', '..', 'IDEA_VERSION.txt')]) {
    if (existsSync(candidate)) return readFileSync(candidate, 'utf8').trim();
  }
  return process.env.IDEA_VERSION ?? 'unknown';
}
