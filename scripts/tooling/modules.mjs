import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pythonEnv, root, run, source } from './common.mjs';

const version = readFileSync(join(root, 'IDEA_VERSION.txt'), 'utf8').trim();
const libraries = ['data-model', 'sdk'];
const services = ['scheduler', 'cluster-manager', 'virtual-desktop-controller'];
const all = [...libraries, ...services, 'dcv-connection-gateway'];
const [action, requested = 'all', ...args] = process.argv.slice(2);
if (!['clean', 'build', 'package', 'apispec'].includes(action)) throw new Error(`Unknown action: ${action}`);
if (requested !== 'all' && requested !== 'make-all-archive' && !all.includes(requested)) throw new Error(`Unknown module: ${requested}`);
const directory = (kind, name) => join(root, kind, `idea-${name}${kind === 'dist' ? `-${version}` : ''}`);
const remove = path => rmSync(path, { recursive: true, force: true });
function copy(from, to) {
  if (existsSync(from)) cpSync(from, to, { recursive: true });
}
function apispec(name, options) {
  run('python', ['-m', 'ideasdk.api_schema', name, ...options],
    { env: { ...pythonEnv, IDEA_PROJECT_ROOT: root } });
}
function clean(name) {
  const project = join(source, `idea-${name}`);
  remove(join(project, 'src/dist'));
  remove(join(project, `src/idea_${name.replaceAll('-', '_')}.egg-info`));
  if (process.env.IDEA_SKIP_WEB_BUILD !== '1') remove(join(project, 'webapp/build'));
  for (const kind of ['build', 'dist']) {
    remove(directory(kind, name));
    remove(`${directory(kind, name)}.tar.gz`);
  }
}
function build(name) {
  const project = join(source, `idea-${name}`);
  const output = directory('build', name);
  remove(output);
  mkdirSync(output, { recursive: true });
  if (name === 'dcv-connection-gateway') {
    copy(project, join(output, 'static_resources'));
    return;
  }
  const src = join(project, 'src');
  const meta = readdirSync(src).find(file => file.endsWith('_meta'));
  const metaFile = join(src, meta, '__init__.py');
  writeFileSync(metaFile, readFileSync(metaFile, 'utf8').replace(/^__version__\s*=.*$/m, `__version__ = '${version}'`));
  remove(join(src, 'dist'));
  run('uv', ['build', '--sdist', '--out-dir', join(src, 'dist'), src], { env: pythonEnv });
  const archives = readdirSync(join(src, 'dist')).filter(file => file.endsWith('.tar.gz'));
  if (archives.length !== 1) throw new Error(`Expected one source archive for ${name}`);
  copy(join(src, 'dist', archives[0]), join(output, `idea-${name}-lib.tar.gz`));
  if (!existsSync(join(output, `idea-${name}-lib.tar.gz`))) throw new Error(`Missing source archive for ${name}`);
  const web = join(project, 'webapp');
  if (existsSync(web)) {
    const manifest = join(web, 'package.json');
    writeFileSync(manifest, readFileSync(manifest, 'utf8').replace(/"version": "[^"]+"/, `"version": "${version}"`));
    const envFile = join(web, '.env');
    let env = readFileSync(envFile, 'utf8');
    const setting = `REACT_APP_IDEA_RELEASE_VERSION="${version}"`;
    env = env.includes('REACT_APP_IDEA_RELEASE_VERSION=') ? env.replace(/^REACT_APP_IDEA_RELEASE_VERSION=.*$/m, setting) : `${env}\n${setting}\n`;
    writeFileSync(envFile, env);
    if (process.env.IDEA_SKIP_WEB_BUILD !== '1') {
      run('yarn', ['install', '--frozen-lockfile'], { cwd: web });
      run('yarn', ['build'], { cwd: web });
    }
    if (!existsSync(join(web, 'build'))) throw new Error('Web build is missing');
    copy(join(web, 'build'), join(output, 'webapp'));
  }
  for (const item of ['resources', 'config']) copy(join(project, item), join(output, item));
  if (existsSync(join(project, 'resources'))) copy(join(source, 'idea-bootstrap'), join(output, 'resources/bootstrap'));
  if (services.includes(name)) apispec(name, ['--output-file', join(output, 'resources/api/openapi.yml')]);
}
function pack(name) {
  const output = directory('dist', name);
  remove(output);
  mkdirSync(output, { recursive: true });
  if (name !== 'dcv-connection-gateway') {
    copy(join(root, 'requirements/runtime.txt'), join(output, 'requirements.txt'));
    for (const library of libraries) {
      build(library);
      copy(directory('build', library), output);
    }
  }
  if (!existsSync(directory('build', name))) throw new Error(`Build ${name} before packaging`);
  copy(directory('build', name), output);
  copy(join(source, `idea-${name}`, 'install'), output);
  run('tar', ['-czf', `${output}.tar.gz`, '-C', output, '.']);
  console.log(`Created ${output}.tar.gz`);
}
function archive() {
  const output = join(root, 'dist', `all-${version}`);
  remove(output);
  mkdirSync(output, { recursive: true });
  for (const file of readdirSync(join(root, 'dist'))) {
    if (file.includes(version) && file.endsWith('.tar.gz') && file !== `all-${version}.tar.gz`) copy(join(root, 'dist', file), join(output, file));
  }
  run('tar', ['-czf', `${output}.tar.gz`, '-C', output, '.']);
  console.log(`Created ${output}.tar.gz`);
}
if (action === 'apispec') {
  if (requested === 'all') {
    const targetIndex = args.indexOf('--target-dir');
    for (const name of services) apispec(name, targetIndex < 0 ? [] : ['--output-file', join(args[targetIndex + 1], `${name}.openapi.yml`)]);
  } else apispec(requested, args);
} else if (action === 'package' && requested === 'make-all-archive') archive();
else {
  const names = requested === 'all' ? (action === 'package' ? [...services, 'dcv-connection-gateway'] : all)
    : requested === 'virtual-desktop-controller' ? [requested, 'dcv-connection-gateway'] : [requested];
  for (const name of names) ({ clean, build, package: pack })[action](name);
  if (requested === 'all' && action === 'package') archive();
  if (requested === 'all' && action === 'clean') {
    for (const path of ['build/open-source', 'deployment/open-source', 'deployment/global-s3-assets', 'dist']) remove(join(root, path));
    if (existsSync(join(root, 'build'))) {
      for (const file of readdirSync(join(root, 'build'))) if (file.startsWith('idea_')) remove(join(root, 'build', file));
    }
  }
}
console.log(`${action} ${requested} complete`);
