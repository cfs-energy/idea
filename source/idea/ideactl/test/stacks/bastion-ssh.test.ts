import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { jinjaEnv, renderTemplate } from '../../src/config/jinja.ts';

const bootstrap = join(import.meta.dirname, '../../../idea-bootstrap');

test('host bastion writes key-only SSH settings before distro defaults, idempotently', () => {
  assert.ok(readFileSync(join(bootstrap, 'bastion-host/setup.sh.jinja2'), 'utf8').includes("include '_templates/linux/bastion_ssh.jinja2'"));
  const dir = mkdtempSync(join(tmpdir(), 'bastion-ssh-'));
  try {
    const config = join(dir, 'sshd_config');
    writeFileSync(config, 'Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication yes\nMatch User example\n  MaxAuthTries 6\n');
    const script = renderTemplate(jinjaEnv(bootstrap), '_templates/linux/bastion_ssh.jinja2', {})
      .replaceAll('/etc/ssh/sshd_config', config)
      // BSD sed requires an explicit empty backup suffix.
      .replace('sed -i ', process.platform === 'darwin' ? "sed -i '' " : 'sed -i ');
    const run = () => execFileSync('bash', ['-e', '-c', `sshd() { test "$1" = -t; }; systemctl() { :; };\n${script}`]);
    run();
    const written = readFileSync(config, 'utf8');
    run();
    assert.equal(readFileSync(config, 'utf8'), written);
    const global = written.split('Include ')[0]!;
    for (const line of ['PasswordAuthentication no', 'KbdInteractiveAuthentication no', 'UsePAM yes', 'PubkeyAuthentication yes', 'LoginGraceTime 30', 'MaxAuthTries 6']) {
      assert.ok(global.split('\n').includes(line), line);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('host bastion keeps the working config and sshd running when validation fails', () => {
  const dir = mkdtempSync(join(tmpdir(), 'bastion-ssh-'));
  try {
    const config = join(dir, 'sshd_config');
    const original = 'AuthenticationMethods publickey,keyboard-interactive\nPasswordAuthentication yes\n';
    writeFileSync(config, original);
    const script = renderTemplate(jinjaEnv(bootstrap), '_templates/linux/bastion_ssh.jinja2', {}).replaceAll('/etc/ssh/sshd_config', config);
    // no -e, as the host setup runs; sshd -t rejects the candidate, a restart would be recorded
    const out = execFileSync('bash', ['-c', `restarted=no; sshd() { return 1; }; systemctl() { restarted=yes; };\n${script}\necho "restarted=$restarted"`], { stdio: ['ignore', 'pipe', 'pipe'] }).toString();
    assert.match(out, /restarted=no/);
    assert.equal(readFileSync(config, 'utf8'), original);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
