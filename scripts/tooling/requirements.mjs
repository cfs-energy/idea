import { cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseArgs } from 'node:util';
import { root, run } from './common.mjs';

export function checkLocks(directory, trackedDirectory) {
  let drift = false;
  for (const file of readdirSync(directory).filter(name => name.endsWith('.txt'))) {
    if (!existsSync(join(directory, file.replace(/\.txt$/, '.in')))) throw new Error(`Orphan lock: ${file}`);
    const tracked = join(trackedDirectory, file);
    if (!existsSync(tracked) || readFileSync(tracked, 'utf8') !== readFileSync(join(directory, file), 'utf8')) {
      drift = true;
      try { run('diff', ['-u', tracked, join(directory, file)]); } catch { /* diff reports drift with exit 1 */ }
    }
  }
  if (drift) throw new Error('Requirements locks differ');
}

export function compileRequirements() {
  const { values } = parseArgs({ options: {
    check: { type: 'boolean' }, container: { type: 'boolean' },
    name: { type: 'string' }, upgrade: { type: 'boolean' },
    package: { type: 'string' }, 'package-name': { type: 'string' },
  } });
  const config = readFileSync(join(root, 'mise.toml'), 'utf8');
  // Keep pip-tools' existing Linux lock format, including its generated command header.
  const python = config.match(/^python = "([^"]+)"/m)[1];
  const compilerVersion = config.match(/"pypi:pip-tools" = \{ version = "([^"]+)"/)[1];
  const click = config.match(/with = \["(click==[^"]+)"\]/)[1];
  const temporary = values.check ? mkdtempSync(join(tmpdir(), 'idea-locks-')) : null;
  const directory = temporary ? join(temporary, 'requirements') : join(root, 'requirements');
  try {
    if (temporary) cpSync(join(root, 'requirements'), directory, { recursive: true });
    let inputs = readdirSync(directory).filter(name => name.endsWith('.in')).sort();
    if (values.name) {
      const name = values.name.replace(/\.(in|txt)$/, '');
      if (!inputs.includes(`${name}.in`)) throw new Error(`Unknown requirements: ${values.name}`);
      inputs = [`${name}.in`];
    }
    const commands = inputs.map(input => {
      const args = ['--strip-extras', '--no-emit-index-url', `--output-file=${input.replace(/\.in$/, '.txt')}`];
      if (values.upgrade) args.push('--upgrade');
      if (values.package || values['package-name']) args.push('--upgrade-package', values.package ?? values['package-name']);
      return [...args, input];
    });
    if (process.platform !== 'linux' || values.container) {
      for (const args of commands) {
        run('docker', ['run', '--rm', '-v', `${directory}:/requirements`, '-w', '/requirements',
          `python:${python}-slim`, 'sh', '-ec',
          'pip install -q "$1" "$2"; shift 2; exec pip-compile "$@"',
          'sh', `pip-tools==${compilerVersion}`, click, ...args]);
      }
    } else {
      const compiler = run('mise', ['which', 'pip-compile'], { stdio: 'pipe', encoding: 'utf8' }).trim();
      for (const args of commands) run(compiler, args, { cwd: directory });
    }
    if (values.check) {
      checkLocks(directory, join(root, 'requirements'));
      console.log('Requirements locks unchanged (byte-for-byte).');
    }
  } finally {
    if (temporary) rmSync(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] === new URL(import.meta.url).pathname) compileRequirements();
