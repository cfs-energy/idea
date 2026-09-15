/**
 * Locates Lambda code assets.
 *
 * Every handler is a Node handler: `src/lambda/<package>/index.ts` in the source tree, or
 * `dist/src/lambda/<package>/index.js` in a build. A package with neither is an error.
 *
 * An asset root holds one bundled `index.mjs`, so the handler string is `index.handler`. Two are
 * supported:
 *
 *   1. `dist/resources/lambda_assets/<package>`, built once at image build time by
 *      `scripts/build-lambda-bundles.mjs`;
 *   2. an on-demand build under `~/.idea/build/lambda/<package>/pkg`.
 */

import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import * as lambda from 'aws-cdk-lib/aws-lambda';

const HERE = dirname(fileURLToPath(import.meta.url));

const CHECKSUM_FILE = 'source.checksum.sha';
const BUILD_DIR = 'pkg';

/** The handler sources ported to Node, beside the running code: `src/lambda` or `dist/src/lambda`. */
export const NODE_LAMBDA_DIR = join(HERE, '..', 'lambda');
/** The package under `src/lambda` that every handler imports and that is not a handler itself. */
export const NODE_COMMONS_PACKAGE = 'commons';
/** The file name a Node asset root holds, which is what makes `index.handler` resolve. */
const NODE_BUNDLE_FILE = 'index.mjs';

/** Bundled handlers are ESM; this lets a transitive CommonJS dependency still call `require`. */
const NODE_BUNDLE_BANNER =
  'import { createRequire as __ideaCreateRequire } from "node:module"; const require = __ideaCreateRequire(import.meta.url);';

/** The entry point of a Node handler package, source tree or build tree, when it has one. */
export function nodeLambdaEntryPoint(lambdaPackageName: string): string | undefined {
  return [join(NODE_LAMBDA_DIR, lambdaPackageName, 'index.ts'), join(NODE_LAMBDA_DIR, lambdaPackageName, 'index.js')].find(
    (candidate) => existsSync(candidate),
  );
}

/** Every handler package under `src/lambda`, the shared commons package aside. */
export function nodeLambdaPackages(): string[] {
  return readdirSync(NODE_LAMBDA_DIR, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name !== NODE_COMMONS_PACKAGE)
    .map((entry) => entry.name)
    .filter((name) => nodeLambdaEntryPoint(name) !== undefined)
    .sort();
}

interface EsbuildApi {
  buildSync(options: Record<string, unknown>): unknown;
}

/**
 * esbuild, required at the moment it is needed. It is a build dependency: the image builds the
 * assets and then prunes it, so a synth there reads the prebuilt bundle and never gets here.
 */
function esbuild(lambdaPackageName: string): EsbuildApi {
  try {
    return createRequire(import.meta.url)('esbuild') as EsbuildApi;
  } catch {
    throw new Error(
      `lambda package ${lambdaPackageName} has no prebuilt bundle and esbuild is not installed to build one. ` +
        'Run npm run build, which writes dist/resources/lambda_assets.',
    );
  }
}

/** Bundles one Node handler into `<outDir>/index.mjs`, dependencies included. */
export function bundleNodeLambda(lambdaPackageName: string, outDir: string): void {
  const entryPoint = nodeLambdaEntryPoint(lambdaPackageName);
  if (entryPoint === undefined) {
    throw new Error(`lambda package ${lambdaPackageName} has no index.ts or index.js under ${NODE_LAMBDA_DIR}`);
  }
  mkdirSync(outDir, { recursive: true });
  esbuild(lambdaPackageName).buildSync({
    entryPoints: [entryPoint],
    outfile: join(outDir, NODE_BUNDLE_FILE),
    // esbuild's worker keeps the working directory of the first build. Synth runs in a scratch
    // directory that is deleted afterwards, so the bundler is given one that outlives it.
    absWorkingDir: NODE_LAMBDA_DIR,
    bundle: true,
    platform: 'node',
    target: 'node22',
    format: 'esm',
    minify: false,
    sourcemap: false,
    banner: { js: NODE_BUNDLE_BANNER },
  });
}

/** The resource tree beside the running code: `<package>/resources`, or `dist/resources` in a build. */
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

export class IdeaCodeAsset {
  readonly lambdaPackageName: string;

  constructor(lambdaPackageName: string) {
    this.lambdaPackageName = lambdaPackageName;
    if (nodeLambdaEntryPoint(lambdaPackageName) === undefined) {
      throw new Error(
        `lambda package not found: ${lambdaPackageName}; looked for ` +
          `${join(NODE_LAMBDA_DIR, lambdaPackageName, 'index.ts')} and ` +
          `${join(NODE_LAMBDA_DIR, lambdaPackageName, 'index.js')}`,
      );
    }
  }

  get runtime(): lambda.Runtime {
    return lambda.Runtime.NODEJS_22_X;
  }

  /** `IdeaCodeAsset.lambda_handler`. */
  get lambdaHandler(): string {
    return 'index.handler';
  }

  /** The directory handed to `lambda.Code.fromAsset`. */
  assetPath(): string {
    const prebuilt = join(distResourcesDir(), 'lambda_assets', this.lambdaPackageName);
    if (existsSync(prebuilt)) return prebuilt;
    return this.buildNodeLambda();
  }

  /**
   * Bundles the handler on demand, reusing the previous bundle while the sources are unchanged.
   * The commons package is in the checksum because every handler imports it.
   */
  private buildNodeLambda(): string {
    const buildRoot = join(lambdaBuildDir(), this.lambdaPackageName);
    const pkg = join(buildRoot, BUILD_DIR);
    const checksumPath = join(buildRoot, CHECKSUM_FILE);
    const checksum = checksumForDirs([
      join(NODE_LAMBDA_DIR, this.lambdaPackageName),
      join(NODE_LAMBDA_DIR, NODE_COMMONS_PACKAGE),
    ]);

    if (existsSync(pkg) && existsSync(checksumPath) && readFileSync(checksumPath, 'utf-8').trim() === checksum) {
      return pkg;
    }

    rmSync(buildRoot, { recursive: true, force: true });
    bundleNodeLambda(this.lambdaPackageName, pkg);
    writeFileSync(checksumPath, `${checksum}\n`);
    return pkg;
  }
}
