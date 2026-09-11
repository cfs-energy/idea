/**
 * Builds rendered bootstrap archives for EC2 UserData and uploads them to the
 * cluster bucket. Deployment-id-qualified keys cause host replacement.
 */

import { PutObjectCommand } from "@aws-sdk/client-s3";
import {
  chmodSync,
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, relative } from "node:path";
import { gzipSync } from "node:zlib";

import { jinjaEnv, renderTemplate } from "../config/jinja.ts";

export interface BootstrapPackageBuildOptions {
  /** The root of the `idea-bootstrap` source tree. */
  sourceDirectory: string;
  /** Archive and rendered-directory basename, excluding `.tar.gz`. */
  targetPackageBasename: string;
  /** Component directories to include. This array is mutated when `common` is auto-added. */
  components: string[];
  /** The single template variable visible to the bootstrap source tree. */
  context: object;
  /** Deployment directory. A system temporary directory is used when absent. */
  tmpDir?: string;
  /** Re-render an existing rendered directory instead of archiving it again. */
  forceBuild?: boolean;
  /** Skips implicit `common` inclusion for Windows. */
  baseOs?: string;
  logger?: (message: string) => void;
}

export interface BootstrapPackageUploadClient {
  send(command: PutObjectCommand): Promise<unknown>;
}

export interface UploadBootstrapPackageOptions {
  client: BootstrapPackageUploadClient;
  clusterS3Bucket: string;
  archiveFile: string;
  logger?: (message: string) => void;
}

export interface UploadReleasePackageOptions {
  client: BootstrapPackageUploadClient;
  clusterS3Bucket: string;
  packageDistDir: string;
  packageName: string;
  upload?: boolean;
  logger?: (message: string) => void;
}

export interface BuildAndUploadBootstrapPackageOptions extends BootstrapPackageBuildOptions {
  client: BootstrapPackageUploadClient;
  clusterS3Bucket: string;
  upload?: boolean;
}

export interface BootstrapPackageUris {
  bootstrapPackageUri?: string;
  controllerBootstrapPackageUri?: string;
  dcvBrokerBootstrapPackageUri?: string;
  dcvConnectionGatewayPackageUri?: string;
}

export interface BootstrapPackagePlan {
  basename: string;
  components: string[];
  contextParameter: string;
}

interface TarEntry {
  archiveName: string;
  path: string;
  isDirectory: boolean;
}

interface TarHeaderValues {
  name: string;
  mode: number;
  uid: number;
  gid: number;
  size: number;
  mtime: number;
  type: string;
}

const TAR_BLOCK_SIZE = 512;

/** Invalid bootstrap package input. */
export class BootstrapPackageError extends Error {}

/** Missing release archive. */
export class PackageNotFoundError extends Error {}

/**
 * Returns names for each host role's rendered bootstrap tree. The caller
 * supplies its module id because configuration can rename module ids.
 */
export function bootstrapPackageBasenames(moduleId: string, deploymentId: string): {
  standard: string;
  controller: string;
  dcvBroker: string;
  dcvConnectionGateway: string;
} {
  return {
    standard: `bootstrap-${moduleId}-${deploymentId}`,
    controller: `bootstrap-${moduleId}-controller-${deploymentId}`,
    dcvBroker: `bootstrap-${moduleId}-dcv-broker-${deploymentId}`,
    dcvConnectionGateway: `bootstrap-${moduleId}-dcv-connection-gateway-${deploymentId}`,
  };
}

/** Per-module build inputs used by `upload-packages` and deploy. The builder prepends `common` when needed. */
export function bootstrapPackagePlans(
  moduleName: string,
  moduleId: string,
  deploymentId: string,
  directoryServiceProvider?: string,
): BootstrapPackagePlan[] {
  const names = bootstrapPackageBasenames(moduleId, deploymentId);
  if (moduleName === "directoryservice") {
    // Only OpenLDAP creates a directoryservice host. The AD providers deploy the stack with no package.
    return directoryServiceProvider === "openldap"
      ? [{ basename: names.standard, components: ["common", "openldap-server"], contextParameter: "bootstrap_package_uri" }]
      : [];
  }
  if (moduleName === "cluster-manager") {
    return [{ basename: names.standard, components: ["cluster-manager"], contextParameter: "bootstrap_package_uri" }];
  }
  if (moduleName === "scheduler") {
    return [{ basename: names.standard, components: ["scheduler"], contextParameter: "bootstrap_package_uri" }];
  }
  if (moduleName === "bastion-host") {
    return [{ basename: names.standard, components: ["common", "bastion-host"], contextParameter: "bootstrap_package_uri" }];
  }
  if (moduleName === "virtual-desktop-controller") {
    return [
      {
        basename: names.controller,
        components: ["virtual-desktop-controller"],
        contextParameter: "controller_bootstrap_package_uri",
      },
      {
        basename: names.dcvBroker,
        components: ["dcv-broker"],
        contextParameter: "dcv_broker_bootstrap_package_uri",
      },
      {
        basename: names.dcvConnectionGateway,
        components: ["dcv-connection-gateway"],
        contextParameter: "dcv_connection_gateway_package_uri",
      },
    ];
  }
  return [];
}

/** Release archives uploaded by `upload-packages` for each module. */
export function releasePackageNames(moduleName: string, releaseVersion: string): string[] {
  const packageNameByModule: Record<string, string[]> = {
    "cluster-manager": ["idea-cluster-manager"],
    scheduler: ["idea-scheduler"],
    "virtual-desktop-controller": ["idea-virtual-desktop-controller", "idea-dcv-connection-gateway"],
  };
  return (packageNameByModule[moduleName] ?? []).map((packageName) => `${packageName}-${releaseVersion}.tar.gz`);
}

/** The S3 URI returned after a bootstrap archive is uploaded. */
export function bootstrapPackageUri(clusterS3Bucket: string, archiveFile: string): string {
  return `s3://${clusterS3Bucket}/idea/bootstrap/${basename(archiveFile)}`;
}

/** Returns the release-package URI, whether the package is uploaded or not. */
export function releasePackageUri(clusterS3Bucket: string, packageName: string): string {
  return `s3://${clusterS3Bucket}/idea/releases/${packageName}`;
}

/** Returns context arguments in insertion order. Host modules use one URI and eVDI uses three role-specific URIs. */
export function bootstrapContextParameterArgs(uris: BootstrapPackageUris): string[] {
  const values: Array<[string, string | undefined]> = [
    ["bootstrap_package_uri", uris.bootstrapPackageUri],
    ["controller_bootstrap_package_uri", uris.controllerBootstrapPackageUri],
    ["dcv_broker_bootstrap_package_uri", uris.dcvBrokerBootstrapPackageUri],
    ["dcv_connection_gateway_package_uri", uris.dcvConnectionGatewayPackageUri],
  ];
  return values
    .filter((entry): entry is [string, string] => entry[1] !== undefined)
    .map(([key, value]) => `-c ${key}=${value}`);
}

/**
 * Recursively copies a direct child directory, preserving source metadata while
 * leaving the component directory as the newly created destination directory.
 */
function copyTree(source: string, target: string): void {
  mkdirSync(target, { recursive: true });
  for (const entry of readdirSync(source, { withFileTypes: true })) {
    const sourcePath = join(source, entry.name);
    const targetPath = join(target, entry.name);
    if (entry.isDirectory()) {
      copyTree(sourcePath, targetPath);
      const stats = statSync(sourcePath);
      chmodSync(targetPath, stats.mode);
      utimesSync(targetPath, stats.atime, stats.mtime);
      continue;
    }
    copyFileSync(sourcePath, targetPath);
    const stats = statSync(sourcePath);
    chmodSync(targetPath, stats.mode);
    utimesSync(targetPath, stats.atime, stats.mtime);
  }
}

/** Archives sorted directory entries depth first. */
function tarEntries(root: string): TarEntry[] {
  const entries: TarEntry[] = [{ archiveName: ".", path: root, isDirectory: true }];
  const visit = (directory: string): void => {
    const children = readdirSync(directory, { withFileTypes: true });
    children.sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));
    for (const entry of children) {
      const path = join(directory, entry.name);
      const archiveName = `./${relative(root, path)}`;
      const isDirectory = entry.isDirectory();
      entries.push({ archiveName, path, isDirectory });
      if (isDirectory) visit(path);
    }
  };
  visit(root);
  return entries;
}

function writeString(target: Buffer, value: string, offset: number, length: number): void {
  Buffer.from(value, "utf8").copy(target, offset, 0, length);
}

function writeOctal(target: Buffer, value: number, offset: number, length: number): void {
  const encoded = value.toString(8).padStart(length - 1, "0").slice(-(length - 1));
  writeString(target, `${encoded}\0`, offset, length);
}

/** Writes the common ustar header shape used by Python's PAX writer. */
function tarHeader(values: TarHeaderValues): Buffer {
  const header = Buffer.alloc(TAR_BLOCK_SIZE);
  writeString(header, values.name, 0, 100);
  writeOctal(header, values.mode, 100, 8);
  writeOctal(header, values.uid, 108, 8);
  writeOctal(header, values.gid, 116, 8);
  writeOctal(header, values.size, 124, 12);
  writeOctal(header, values.mtime, 136, 12);
  header.fill(0x20, 148, 156);
  writeString(header, values.type, 156, 1);
  writeString(header, "ustar\0", 257, 6);
  writeString(header, "00", 263, 2);
  const checksum = header.reduce((total, byte) => total + byte, 0);
  writeString(header, `${checksum.toString(8).padStart(6, "0")}\0 `, 148, 8);
  return header;
}

/**
 * Formats a POSIX extended-header record, including its byte length prefix.
 */
function paxRecord(key: string, value: string): string {
  const body = `${key}=${value}\n`;
  let length = Buffer.byteLength(body, "utf8") + 2;
  while (true) {
    const record = `${length} ${body}`;
    const actualLength = Buffer.byteLength(record, "utf8");
    if (actualLength === length) return record;
    length = actualLength;
  }
}

/** Matches Python's float formatting for archive modification times. */
function paxMtime(mtimeMs: number): string {
  const seconds = mtimeMs / 1000;
  return Number.isInteger(seconds) ? `${seconds}.0` : String(seconds);
}

/** Appends a tar member header, its contents, and required block padding. */
function appendTarMember(blocks: Buffer[], header: Buffer, contents: Buffer): void {
  blocks.push(header, contents);
  const padding = (TAR_BLOCK_SIZE - (contents.length % TAR_BLOCK_SIZE)) % TAR_BLOCK_SIZE;
  if (padding > 0) blocks.push(Buffer.alloc(padding));
}

/**
 * Creates a gzip-compressed POSIX PAX archive compatible with Python's
 * `shutil.make_archive(..., "gztar", ...)` member stream.
 */
function createTarGz(directory: string, archiveFile: string): void {
  const blocks: Buffer[] = [];
  for (const entry of tarEntries(directory)) {
    const archiveName = entry.isDirectory ? `${entry.archiveName}/` : entry.archiveName;
    const stats = lstatSync(entry.path);
    const attributes = [
      ...(Buffer.byteLength(archiveName, "utf8") > 100 ? [paxRecord("path", archiveName)] : []),
      paxRecord("mtime", paxMtime(stats.mtimeMs)),
    ];
    const extendedHeaderContents = Buffer.from(attributes.join(""), "utf8");
    appendTarMember(
      blocks,
      tarHeader({
        name: "././@PaxHeader",
        mode: 0,
        uid: 0,
        gid: 0,
        size: extendedHeaderContents.length,
        mtime: 0,
        type: "x",
      }),
      extendedHeaderContents,
    );
    appendTarMember(
      blocks,
      tarHeader({
        name: archiveName,
        mode: stats.mode & 0o7777,
        uid: stats.uid,
        gid: stats.gid,
        size: entry.isDirectory ? 0 : stats.size,
        mtime: Math.floor(stats.mtimeMs / 1000),
        type: entry.isDirectory ? "5" : "0",
      }),
      entry.isDirectory ? Buffer.alloc(0) : readFileSync(entry.path),
    );
  }
  blocks.push(Buffer.alloc(TAR_BLOCK_SIZE * 2));
  writeFileSync(archiveFile, gzipSync(Buffer.concat(blocks), { level: 9 }));
}

/**
 * Renders immediate `.jinja2` component files, copies every other immediate
 * child, omits `_templates`, then archives the rendered directory with `.` as
 * the tar root.
 */
export class BootstrapPackageBuilder {
  private readonly options: BootstrapPackageBuildOptions;

  constructor(options: BootstrapPackageBuildOptions) {
    if (options.components.length === 0) {
      throw new BootstrapPackageError("components[] is required.");
    }
    if (!options.components.includes("common")) {
      if (options.baseOs === undefined || !options.baseOs.toLowerCase().includes("windows")) {
        options.components.unshift("common");
      }
    }
    this.options = options;
  }

  build(): string {
    const tmpDirectory = this.options.tmpDir ?? mkdtempSync(join(tmpdir(), "tmp"));
    mkdirSync(tmpDirectory, { recursive: true });
    const targetDirectory = join(tmpDirectory, this.options.targetPackageBasename);
    const archiveFile = `${targetDirectory}.tar.gz`;

    if (existsSync(targetDirectory)) {
      if (this.options.forceBuild === true) {
        this.log(`deleting existing directory: ${targetDirectory} ...`);
        rmSync(targetDirectory, { recursive: true });
      } else {
        this.log(
          `found existing bootstrap directory: ${targetDirectory}. use force_build=True to rebuild the bootstrap package.`,
        );
        if (existsSync(archiveFile)) rmSync(archiveFile);
        createTarGz(targetDirectory, archiveFile);
        return archiveFile;
      }
    }

    const environment = jinjaEnv(this.options.sourceDirectory);
    for (const component of readdirSync(this.options.sourceDirectory, { withFileTypes: true })) {
      if (component.name === "_templates" || !this.options.components.includes(component.name)) continue;
      const sourceComponentDirectory = join(this.options.sourceDirectory, component.name);
      const targetComponentDirectory = join(targetDirectory, component.name);
      mkdirSync(targetComponentDirectory, { recursive: true });

      for (const file of readdirSync(sourceComponentDirectory, { withFileTypes: true })) {
        if (file.name === "_templates") continue;
        const sourceFile = join(sourceComponentDirectory, file.name);
        if (file.name.endsWith(".jinja2")) {
          const targetFile = join(targetComponentDirectory, file.name.replace(".jinja2", ""));
          const content = renderTemplate(environment, `${component.name}/${file.name}`, {
            context: this.options.context,
          });
          this.log(`rendered template: ${targetFile}`);
          writeFileSync(targetFile, content);
        } else if (file.isDirectory()) {
          const targetFile = join(targetComponentDirectory, file.name);
          this.log(`copied directory: ${targetFile}`);
          copyTree(sourceFile, targetFile);
        } else {
          const targetFile = join(targetComponentDirectory, file.name);
          this.log(`copied file: ${targetFile}`);
          copyFileSync(sourceFile, targetFile);
          const stats = statSync(sourceFile);
          chmodSync(targetFile, stats.mode);
          utimesSync(targetFile, stats.atime, stats.mtime);
        }
      }
    }

    createTarGz(targetDirectory, archiveFile);
    return archiveFile;
  }

  private log(message: string): void {
    this.options.logger?.(message);
  }
}

/** Upload a rendered bootstrap package to `idea/bootstrap/`, then return its S3 URI. */
export async function uploadBootstrapPackage(options: UploadBootstrapPackageOptions): Promise<string> {
  const key = `idea/bootstrap/${basename(options.archiveFile)}`;
  const uri = `s3://${options.clusterS3Bucket}/${key}`;
  options.logger?.(`uploading bootstrap package ${uri} ...`);
  await options.client.send(
    new PutObjectCommand({
      Bucket: options.clusterS3Bucket,
      Key: key,
      Body: readFileSync(options.archiveFile),
    }),
  );
  return uri;
}

/** Builds the archive. When `upload` is false, returns undefined. */
export async function buildAndUploadBootstrapPackage(
  options: BuildAndUploadBootstrapPackageOptions,
): Promise<string | undefined> {
  const archiveFile = new BootstrapPackageBuilder(options).build();
  if (options.upload === false) return undefined;
  return uploadBootstrapPackage({
    client: options.client,
    clusterS3Bucket: options.clusterS3Bucket,
    archiveFile,
    logger: options.logger,
  });
}

/** Uploads a release package to `idea/releases/` after checking that it exists. */
export async function uploadReleasePackage(options: UploadReleasePackageOptions): Promise<string> {
  const packageFile = join(options.packageDistDir, options.packageName);
  if (!existsSync(packageFile)) {
    throw new PackageNotFoundError(`package not found: ${packageFile}`);
  }
  const key = `idea/releases/${basename(packageFile)}`;
  const uri = `s3://${options.clusterS3Bucket}/${key}`;
  if (options.upload !== false) {
    options.logger?.(`uploading release package: ${uri} ...`);
    await options.client.send(
      new PutObjectCommand({
        Bucket: options.clusterS3Bucket,
        Key: key,
        Body: readFileSync(packageFile),
      }),
    );
  }
  return uri;
}
