import { join } from 'node:path';
import { parseArgs } from 'node:util';
import { pythonEnv, source, run } from './common.mjs';

const component = process.argv[2];
const packages = { 'data-model': 'ideadatamodel', sdk: 'ideasdk', scheduler: 'ideascheduler', 'cluster-manager': 'ideaclustermanager',
  'virtual-desktop-controller': 'ideavirtualdesktopcontroller' };
if (!packages[component]) throw new Error(`Unknown suite: ${component}`);
const { values, positionals } = parseArgs({ args: process.argv.slice(3), allowPositionals: true, options: {
  keywords: { type: 'string', short: 'k' }, params: { type: 'string', multiple: true, short: 'p' },
  'capture-output': { type: 'boolean', short: 'c' }, 'cov-report': { type: 'string' },
} });
const args = ['-m', 'pytest', '-v', '--disable-warnings'];
for (const param of values.params ?? []) {
  const split = param.indexOf('=');
  args.push(`--${split < 0 ? param : param.slice(0, split)}`);
  if (split >= 0) args.push(param.slice(split + 1));
}
if (values.keywords) args.push('-k', values.keywords);
if (values['capture-output']) args.push('--capture=tee-sys');
if (values['cov-report']) args.push('--cov', packages[component], '--cov-report', values['cov-report']);
args.push(...positionals);
const cwd = join(source, `idea-${component}`, 'tests');
run(process.env.VIRTUAL_ENV ? join(process.env.VIRTUAL_ENV, 'bin/python') : 'python', args,
  { cwd, env: { ...pythonEnv, PYTHONPATH: `${pythonEnv.PYTHONPATH}:${cwd}` } });
