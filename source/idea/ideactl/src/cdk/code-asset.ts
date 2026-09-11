/**
 * Locates Lambda code assets.
 *
 * A Lambda asset root is a directory whose top level holds the handler package and the shared
 * commons package, plus any third-party dependencies the handler imports:
 *
 *   <asset root>/<package>/handler.py
 *   <asset root>/idea_lambda_commons/...
 *   <asset root>/<installed dependencies>
 *
 * The handler string is `<package>.handler.handler`, so the package has to be a directory inside
 * the asset root. `dist/resources/lambda_functions/<package>` is the source of one package only,
 * which is why it cannot be handed to `lambda.Code.fromAsset` as it stands.
 *
 * Two asset roots are supported:
 *
 *   1. `dist/resources/lambda_assets/<package>`, built once by `scripts/build-lambda-zips.sh`;
 *   2. an on-demand build under `~/.idea/build/lambda/<package>/pkg`, assembled from
 *      `dist/resources/lambda_functions`.
 */

import { createHash } from 'node:crypto';
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

const COMMONS_PACKAGE = 'idea_lambda_commons';
const CHECKSUM_FILE = 'source.checksum.sha';
const BUILD_DIR = 'pkg';

/** Finds `dist/resources` for source and built execution. */
export function distResourcesDir(): string {
  const candidates = [join(HERE, '..', '..', 'resources'), join(HERE, '..', '..', 'dist', 'resources')];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (found === undefined) {
    throw new Error(`dist resources directory not found; looked in ${candidates.join(', ')}`);
  }
  return found;
}

/** `~/.idea/build/lambda`, or under `IDEA_USER_HOME`. */
function lambdaBuildDir(): string {
  const home = process.env.IDEA_USER_HOME ?? join(homedir(), '.idea');
  return join(home, 'build', 'lambda');
}

/** Content checksum over one or more directories: sorted relative paths plus file bytes. */
function checksumForDirs(dirs: string[]): string {
  const hash = createHash('sha256');
  for (const dir of dirs) {
    const walk = (current: string, prefix: string): void => {
      for (const entry of readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
        const full = join(current, entry.name);
        const relative = prefix === '' ? entry.name : `${prefix}/${entry.name}`;
        if (entry.isDirectory()) {
          walk(full, relative);
          continue;
        }
        hash.update(relative);
        hash.update(readFileSync(full));
      }
    };
    hash.update(dir);
    if (existsSync(dir) && statSync(dir).isDirectory()) walk(dir, '');
  }
  return hash.digest('hex');
}

/** The interpreter used to install a handler's third-party dependencies. */
function pythonBin(): string | undefined {
  const candidates = [process.env.PYTHON, 'python3.13', 'python3'].filter(
    (candidate): candidate is string => candidate !== undefined && candidate !== '',
  );
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ['--version'], { stdio: 'ignore' });
    if (probe.status === 0) return candidate;
  }
  return undefined;
}

export class IdeaCodeAsset {
  readonly lambdaPackageName: string;

  constructor(lambdaPackageName: string) {
    this.lambdaPackageName = lambdaPackageName;
  }

  /** `IdeaCodeAsset.lambda_handler`. */
  get lambdaHandler(): string {
    return `${this.lambdaPackageName}.handler.handler`;
  }

  /** The directory handed to `lambda.Code.fromAsset`. */
  assetPath(): string {
    const resources = distResourcesDir();
    const prebuilt = join(resources, 'lambda_assets', this.lambdaPackageName);
    if (existsSync(prebuilt)) return prebuilt;

    const functions = join(resources, 'lambda_functions');
    const source = join(functions, this.lambdaPackageName);
    if (!existsSync(source)) {
      throw new Error(
        `lambda package not found: ${this.lambdaPackageName}; looked in ${prebuilt}, ${source}`,
      );
    }
    return this.buildLambda(functions, source);
  }

  /**
   * Assembles the asset root the handler string needs, reusing the previous build while the
   * sources are unchanged. A handler with a `requirements.txt` needs a Python interpreter with
   * pip; without one the build refuses instead of producing an asset that cannot import.
   */
  private buildLambda(functionsDir: string, sourceDir: string): string {
    const commonsDir = join(functionsDir, COMMONS_PACKAGE);
    if (!existsSync(commonsDir)) {
      throw new Error(`lambda commons package not found: ${commonsDir}`);
    }
    const buildRoot = join(lambdaBuildDir(), this.lambdaPackageName);
    const pkg = join(buildRoot, BUILD_DIR);
    const checksumPath = join(buildRoot, CHECKSUM_FILE);
    const checksum = checksumForDirs([sourceDir, commonsDir]);

    if (existsSync(pkg) && existsSync(checksumPath) && readFileSync(checksumPath, 'utf-8').trim() === checksum) {
      return pkg;
    }

    rmSync(buildRoot, { recursive: true, force: true });
    mkdirSync(pkg, { recursive: true });
    cpSync(commonsDir, join(pkg, COMMONS_PACKAGE), { recursive: true });
    cpSync(sourceDir, join(pkg, this.lambdaPackageName), { recursive: true });

    const requirements = join(pkg, this.lambdaPackageName, 'requirements.txt');
    if (existsSync(requirements)) {
      const moved = join(pkg, 'requirements.txt');
      renameSync(requirements, moved);
      const python = pythonBin();
      if (python === undefined) {
        rmSync(buildRoot, { recursive: true, force: true });
        throw new Error(
          `lambda package ${this.lambdaPackageName} has dependencies in requirements.txt and no ` +
            'python interpreter was found to install them. Build the assets once with ' +
            'scripts/build-lambda-zips.sh, or set PYTHON to an interpreter with pip.',
        );
      }
      const install = spawnSync(
        python,
        [
          '-m',
          'pip',
          'install',
          '-r',
          'requirements.txt',
          '--platform',
          'manylinux2014_x86_64',
          '--only-binary=:all:',
          '--target',
          '.',
          '--upgrade',
        ],
        { cwd: pkg, stdio: 'inherit' },
      );
      if (install.status !== 0) {
        rmSync(buildRoot, { recursive: true, force: true });
        throw new Error(
          `failed to install dependencies for lambda package ${this.lambdaPackageName} ` +
            `(${python} -m pip exited ${String(install.status)}). Build the assets once with ` +
            'scripts/build-lambda-zips.sh.',
        );
      }
    }

    writeFileSync(checksumPath, `${checksum}\n`);
    return pkg;
  }
}
