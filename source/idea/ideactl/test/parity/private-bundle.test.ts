import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { load } from "js-yaml";
import { fileURLToPath } from "node:url";
import test from "node:test";

test("private bundle accepts data and rejects traversal, links, code and checksum changes", () => {
  const script = fileURLToPath(new URL("../../scripts/unpack-private-parity.py", import.meta.url));
  execFileSync("python3", ["-c", `
import hashlib, importlib.util, io, pathlib, tarfile, tempfile
spec = importlib.util.spec_from_file_location('bundle', ${JSON.stringify(script)})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory() as root:
    root = pathlib.Path(root)
    archive = root / 'bundle.tar.gz'
    for name, kind, valid in [
        ('tools/parity/live/test.json', tarfile.REGTYPE, True),
        ('tools/parity/fixtures/test.json', tarfile.REGTYPE, True),
        ('docs/port/requests/security-fix.md', tarfile.REGTYPE, True),
        ('../escape', tarfile.REGTYPE, False),
        ('tools/parity/live/../../../src/main.ts', tarfile.REGTYPE, False),
        ('/tmp/escape', tarfile.REGTYPE, False),
        ('src/main.ts', tarfile.REGTYPE, False),
        ('tools/parity/live/link', tarfile.SYMTYPE, False),
        ('tools/parity/live/link', tarfile.LNKTYPE, False),
    ]:
        with tarfile.open(archive, 'w:gz') as tar:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = 'target' if kind != tarfile.REGTYPE else ''
            member.size = 2 if kind == tarfile.REGTYPE else 0
            tar.addfile(member, io.BytesIO(b'{}') if kind == tarfile.REGTYPE else None)
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as destination:
            try:
                module.unpack(archive, checksum, destination)
            except ValueError:
                assert not valid, name
            else:
                assert valid, name
                assert (pathlib.Path(destination) / name).read_bytes() == b'{}'
        try:
            module.unpack(archive, '0' * 64, root)
        except ValueError:
            pass
        else:
            raise AssertionError('checksum accepted')
`], { stdio: "pipe", timeout: 10_000 });
});


test("a failed private fetch fails closed, removes data and never prints diagnostics", () => {
  const scratch = mkdtempSync(join(tmpdir(), "private-parity-check-"));
  try {
    const bin = join(scratch, "bin");
    mkdirSync(bin);
    mkdirSync(join(scratch, "tools/parity/live"), { recursive: true });
    writeFileSync(join(scratch, "tools/parity/live/capture.json"), "private data");
    writeFileSync(join(bin, "aws"), '#!/bin/sh\necho PRIVATE_DIAGNOSTIC >&2\nexit 7\n', { mode: 0o700 });
    const script = fileURLToPath(new URL("../../scripts/private-parity.sh", import.meta.url));
    const result = spawnSync("bash", [script], {
      cwd: scratch,
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, RUNNER_TEMP: scratch, IDEACTL_PARITY_SHA256: "a".repeat(64) },
      encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.match(result.stdout, /Private parity failed/);
    assert.doesNotMatch(result.stdout + result.stderr, /PRIVATE_DIAGNOSTIC|private data/);
    assert.equal(existsSync(join(scratch, "tools/parity/live")), false);
    assert.equal(existsSync(join(scratch, "ideactl-private-parity")), false);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
});

test("private workflow restricts access to upstream main and keeps data off public artifacts", () => {
  const text = readFileSync(new URL("../../../../../.github/workflows/ideactl_checks.yaml", import.meta.url), "utf8");
  const workflow = load(text) as { jobs: Record<string, { if: string; permissions?: Record<string, string>; steps: { uses?: string; run?: string }[] }> };
  const job = workflow.jobs["private-parity"];
  assert.match(job.if, /github.repository == 'cfs-energy\/idea'/);
  assert.match(job.if, /github.ref == 'refs\/heads\/main'/);
  assert.match(job.if, /github.event_name == 'push' \|\| github.event_name == 'workflow_dispatch'/);
  assert.match(job.if, /vars.IDEACTL_PRIVATE_PARITY == '1'/);
  assert.equal(job.permissions?.["id-token"], "write");
  assert.ok(job.steps.some((step) => step.run === "bash scripts/private-parity.sh"));
  assert.ok(job.steps.every((step) => !/upload-artifact|actions\/cache/.test(step.uses ?? "")));
});
