import { spawnSync } from 'node:child_process';

let status = 0;
for (const suite of ['data-model', 'sdk', 'cluster-manager', 'scheduler', 'virtual-desktop-controller', 'web-portal']) {
  const args = suite === 'web-portal' ? [] : process.argv.slice(2);
  const result = spawnSync('mise', ['run', `test:${suite}`, ...args], { stdio: 'inherit' });
  if (result.error) console.error(result.error.message);
  if (result.status !== 0) status = 1;
}
process.exitCode = status;
