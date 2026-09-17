/**
 * Upload failure, overwrite, and concurrent put behaviour of the package
 * publisher. The client is injected. There is no live AWS call.
 */

import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { PutObjectCommand } from "@aws-sdk/client-s3";

import {
  type BootstrapPackageUploadClient,
  PackageNotFoundError,
  buildAndUploadBootstrapPackage,
  uploadBootstrapPackage,
  uploadReleasePackage,
} from "../../src/cli/bootstrap-package.ts";
import { withWorkdirAsync } from "./bootstrap-helpers.ts";

interface StoredObject {
  bucket: string;
  key: string;
  body: Buffer;
  extras: Record<string, unknown>;
}

/** In-memory bucket that records every PutObject. */
class MemoryBucket implements BootstrapPackageUploadClient {
  readonly objects = new Map<string, Buffer>();
  readonly puts: StoredObject[] = [];
  failNext = false;
  /** When set, the body is truncated then the put throws, modelling a non-atomic transport. */
  truncateThenFail = false;
  delayMs = 0;

  async send(command: PutObjectCommand): Promise<unknown> {
    if (this.delayMs > 0) {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, this.delayMs);
      });
    }
    const bucket = command.input.Bucket ?? "";
    const key = command.input.Key ?? "";
    const raw = command.input.Body;
    const body = raw instanceof Uint8Array ? Buffer.from(raw) : Buffer.from(String(raw ?? ""));
    const extras: Record<string, unknown> = {};
    if (command.input.ContentMD5 !== undefined) extras.ContentMD5 = command.input.ContentMD5;
    if (command.input.ChecksumSHA256 !== undefined) extras.ChecksumSHA256 = command.input.ChecksumSHA256;
    if (command.input.IfNoneMatch !== undefined) extras.IfNoneMatch = command.input.IfNoneMatch;
    if (command.input.IfMatch !== undefined) extras.IfMatch = command.input.IfMatch;

    if (this.truncateThenFail) {
      this.objects.set(`${bucket}/${key}`, body.subarray(0, Math.min(4, body.length)));
      this.truncateThenFail = false;
      throw new Error("socket hang up");
    }
    if (this.failNext) {
      this.failNext = false;
      throw new Error("upload failed");
    }
    const stored: StoredObject = { bucket, key, body, extras };
    this.puts.push(stored);
    this.objects.set(`${bucket}/${key}`, body);
    return {};
  }
}

test("a failed put leaves an existing older package in place and does not retry", async () => {
  await withWorkdirAsync(async (directory) => {
    const archiveFile = join(directory, "bootstrap-scheduler-deployment.tar.gz");
    writeFileSync(archiveFile, "new-package-bytes");
    const client = new MemoryBucket();
    await uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile });
    client.objects.set(
      "sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz",
      Buffer.from("old-package-bytes"),
    );
    client.failNext = true;

    await assert.rejects(
      () => uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile }),
      /upload failed/,
    );
    assert.equal(
      client.objects.get("sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz")?.toString(),
      "old-package-bytes",
    );
    assert.equal(client.puts.length, 1);
  });
});

test("a second upload of the same key replaces the older package and keeps the URI", async () => {
  await withWorkdirAsync(async (directory) => {
    const archiveFile = join(directory, "bootstrap-scheduler-deployment.tar.gz");
    const client = new MemoryBucket();
    writeFileSync(archiveFile, "old-package-bytes");
    const first = await uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile });
    writeFileSync(archiveFile, "new-package-bytes");
    const second = await uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile });

    assert.equal(first, second);
    assert.equal(first, "s3://sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz");
    assert.equal(
      client.objects.get("sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz")?.toString(),
      "new-package-bytes",
    );
    assert.equal(client.puts.length, 2);
    assert.deepEqual(client.puts[0]?.extras, {});
    assert.deepEqual(client.puts[1]?.extras, {});
  });
});

test("two overlapping uploads of the same key both succeed and leave one complete object", async () => {
  await withWorkdirAsync(async (directory) => {
    mkdirSync(join(directory, "left"));
    mkdirSync(join(directory, "right"));
    const leftFile = join(directory, "left", "bootstrap-shared.tar.gz");
    const rightFile = join(directory, "right", "bootstrap-shared.tar.gz");
    writeFileSync(leftFile, "left-complete-archive");
    writeFileSync(rightFile, "right-complete-archive");
    const client = new MemoryBucket();
    client.delayMs = 15;

    const uris = await Promise.all([
      uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile: leftFile }),
      uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile: rightFile }),
    ]);

    assert.deepEqual(uris, [
      "s3://sample-bucket/idea/bootstrap/bootstrap-shared.tar.gz",
      "s3://sample-bucket/idea/bootstrap/bootstrap-shared.tar.gz",
    ]);
    const current = client.objects.get("sample-bucket/idea/bootstrap/bootstrap-shared.tar.gz");
    assert.ok(current);
    const text = current.toString();
    assert.ok(text === "left-complete-archive" || text === "right-complete-archive");
    assert.equal(client.objects.size, 1);
    assert.equal(client.puts.length, 2);
  });
});

test("a mid-put failure on a non-atomic transport leaves truncated bytes and is not cleaned up", async () => {
  await withWorkdirAsync(async (directory) => {
    const archiveFile = join(directory, "bootstrap-scheduler-deployment.tar.gz");
    writeFileSync(archiveFile, "full-package-archive");
    const client = new MemoryBucket();
    client.truncateThenFail = true;

    await assert.rejects(
      () => uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile }),
      /socket hang up/,
    );
    assert.equal(
      client.objects.get("sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz")?.toString(),
      "full",
    );
    assert.equal(client.puts.length, 0);
  });
});

test("PutObject is sent without a checksum or compare-and-swap header", async () => {
  await withWorkdirAsync(async (directory) => {
    const archiveFile = join(directory, "bootstrap-scheduler-deployment.tar.gz");
    writeFileSync(archiveFile, "package-bytes");
    const client = new MemoryBucket();
    await uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile });
    assert.deepEqual(client.puts[0]?.extras, {});
  });
});

test("a missing release package fails before any put", async () => {
  await withWorkdirAsync(async (directory) => {
    const client = new MemoryBucket();
    await assert.rejects(
      () =>
        uploadReleasePackage({
          client,
          clusterS3Bucket: "sample-bucket",
          packageDistDir: directory,
          packageName: "idea-scheduler-26.09.0.tar.gz",
        }),
      PackageNotFoundError,
    );
    assert.equal(client.puts.length, 0);
  });
});

test("an older package under a different deployment basename is left in place", async () => {
  await withWorkdirAsync(async (directory) => {
    const older = join(directory, "bootstrap-scheduler-old.tar.gz");
    const newer = join(directory, "bootstrap-scheduler-new.tar.gz");
    writeFileSync(older, "older-archive");
    writeFileSync(newer, "newer-archive");
    const client = new MemoryBucket();
    await uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile: older });
    await uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile: newer });
    assert.equal(client.objects.get("sample-bucket/idea/bootstrap/bootstrap-scheduler-old.tar.gz")?.toString(), "older-archive");
    assert.equal(client.objects.get("sample-bucket/idea/bootstrap/bootstrap-scheduler-new.tar.gz")?.toString(), "newer-archive");
    assert.equal(client.objects.size, 2);
  });
});

test("skipping upload returns no URI and does not put", async () => {
  await withWorkdirAsync(async (directory) => {
    mkdirSync(join(directory, "source", "app"), { recursive: true });
    writeFileSync(join(directory, "source", "app", "setup.sh"), "echo ok\n", "utf8");
    const client = new MemoryBucket();
    const uri = await buildAndUploadBootstrapPackage({
      sourceDirectory: join(directory, "source"),
      targetPackageBasename: "bootstrap-skip",
      components: ["app"],
      context: {},
      tmpDir: directory,
      client,
      clusterS3Bucket: "sample-bucket",
      upload: false,
    });
    assert.equal(uri, undefined);
    assert.equal(client.puts.length, 0);
  });
});

test("a thrown put does not return a URI the stack could bake into user data", async () => {
  await withWorkdirAsync(async (directory) => {
    const archiveFile = join(directory, "bootstrap-scheduler-deployment.tar.gz");
    writeFileSync(archiveFile, "package-bytes");
    const client = new MemoryBucket();
    client.failNext = true;
    await assert.rejects(
      () => uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile }),
      /upload failed/,
    );
    assert.equal(client.puts.length, 0);
  });
});
