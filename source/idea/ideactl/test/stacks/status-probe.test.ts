/**
 * The endpoint probe behind `check-cluster-status` replaces `requests.get(url, verify=False)`, and
 * two of that call's defaults have to be supplied by hand.
 *
 * A request with no User-Agent is answered 403 by the cluster's external load balancer, so a probe
 * that sends none reports every healthy endpoint as failing. And the analytics dashboard answers
 * `/_dashboards/` with a 302 to `/_dashboards/app/home`, so a probe that does not follow redirects
 * reports a working dashboard as failing. Both were observed on a live cluster whose target groups
 * were all healthy and whose endpoints answered 200 to curl.
 *
 * The server here stands in for that behaviour: one path refuses a request with no User-Agent, one
 * path redirects once. A probe that gets both right sees 200 on both.
 */

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createServer, type Server } from 'node:https';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { liveHttpStatus, PROBE_USER_AGENT } from '../../src/cli/commands/status.ts';

/** A throwaway certificate for a loopback listener, generated per run rather than committed. */
function selfSignedCertificate(dir: string): { key: string; cert: string } | undefined {
  const key = join(dir, 'key.pem');
  const cert = join(dir, 'cert.pem');
  const result = spawnSync(
    'openssl',
    [
      'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
      '-keyout', key, '-out', cert,
      '-days', '1', '-subj', '/CN=localhost',
      '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
    ],
    { stdio: 'ignore' },
  );
  if (result.status !== 0) return undefined;
  return { key: readFileSync(key, 'utf-8'), cert: readFileSync(cert, 'utf-8') };
}

function listen(server: Server): Promise<number> {
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      resolve(typeof address === 'object' && address !== null ? address.port : 0);
    });
  });
}

test('the probe sends a User-Agent and follows a redirect', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'ideactl-probe-'));
  try {
    const credentials = selfSignedCertificate(dir);
    if (credentials === undefined) {
      t.skip('openssl is not available to generate a loopback certificate');
      return;
    }

    const seenUserAgents: Array<string | undefined> = [];
    const server = createServer({ key: credentials.key, cert: credentials.cert }, (req, res) => {
      seenUserAgents.push(req.headers['user-agent']);
      // The load balancer's behaviour: no User-Agent, no answer.
      if (req.headers['user-agent'] === undefined || req.headers['user-agent'] === '') {
        res.writeHead(403).end();
        return;
      }
      if (req.url === '/_dashboards/') {
        res.writeHead(302, { location: '/_dashboards/app/home' }).end();
        return;
      }
      res.writeHead(200, { 'content-type': 'application/json' }).end('{"success":true}');
    });

    const port = await listen(server);
    try {
      const base = `https://127.0.0.1:${String(port)}`;

      assert.equal(
        await liveHttpStatus(`${base}/cluster-manager/healthcheck`),
        200,
        'a probe with no User-Agent is answered 403 by the load balancer',
      );
      assert.equal(
        await liveHttpStatus(`${base}/_dashboards/`),
        200,
        'the dashboard answers a 302 to its home page, which the probe has to follow',
      );

      assert.ok(seenUserAgents.length >= 3, 'the redirect should have produced a third request');
      for (const agent of seenUserAgents) {
        assert.ok(
          agent !== undefined && agent !== '',
          `every probe request carries a User-Agent; saw ${JSON.stringify(agent)}`,
        );
      }
      assert.match(PROBE_USER_AGENT, /^ideactl\/\d+\.\d+\.\d+/);
    } finally {
      server.close();
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('the probe stops following redirects rather than looping', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'ideactl-probe-loop-'));
  try {
    const credentials = selfSignedCertificate(dir);
    if (credentials === undefined) {
      t.skip('openssl is not available to generate a loopback certificate');
      return;
    }
    let requests = 0;
    const server = createServer({ key: credentials.key, cert: credentials.cert }, (req, res) => {
      requests += 1;
      res.writeHead(302, { location: '/again' }).end();
    });
    const port = await listen(server);
    try {
      const status = await liveHttpStatus(`https://127.0.0.1:${String(port)}/again`);
      assert.equal(status, 302, 'a redirect loop ends by returning the last redirect status');
      assert.ok(requests <= 31, `the probe made ${String(requests)} requests, which is not bounded`);
    } finally {
      server.close();
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
