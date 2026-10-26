import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

export const root = fileURLToPath(new URL('../../', import.meta.url));
export const source = join(root, 'source/idea');
export const pythonEnv = {
  ...process.env,
  PYTHONPATH: ['idea-data-model', 'idea-sdk', 'idea-test-utils', 'idea-scheduler',
    'idea-cluster-manager', 'idea-virtual-desktop-controller'].map(name => join(source, name, 'src')).join(':'),
};
export function run(command, args, options = {}) {
  if (command === 'python' && process.env.VIRTUAL_ENV) command = join(process.env.VIRTUAL_ENV, 'bin/python');
  return execFileSync(command, args, { cwd: root, stdio: 'inherit', ...options });
}
