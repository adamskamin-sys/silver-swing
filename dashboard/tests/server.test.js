/**
 * Dashboard server smoke tests. Uses node:test (built-in, no extra deps) and
 * Node's http client to hit the app on an ephemeral port.
 *
 * Covers: session gate on /api/*, /api/status reads real store JSON,
 * /api/trades reads JSONL, dev-mode (no password) bypasses auth.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';

import { makeApp } from '../server.js';

async function tmpFile(name, contents) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'sw-dash-'));
  const p = path.join(dir, name);
  await fs.writeFile(p, contents);
  return p;
}

function listen(app) {
  return new Promise((resolve) => {
    const server = app.listen(0, () => {
      resolve({ server, port: server.address().port });
    });
  });
}

function req(port, method, urlPath, { body, cookie } = {}) {
  return new Promise((resolve, reject) => {
    const opts = {
      port, method, path: urlPath,
      headers: {
        'content-type': 'application/json',
        ...(cookie ? { cookie } : {}),
      },
    };
    const r = http.request(opts, (res) => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve({
        status: res.statusCode,
        cookie: res.headers['set-cookie'],
        body: (() => { try { return JSON.parse(Buffer.concat(chunks).toString()); } catch { return null; } })(),
      }));
    });
    r.on('error', reject);
    if (body !== undefined) r.write(JSON.stringify(body));
    r.end();
  });
}

// ---- unauthenticated /api/* is blocked when password is set ---------------

test('protected routes 401 without session', async () => {
  const storePath = await tmpFile('store.json', '{}');
  const app = makeApp({ storePath, password: 'secret', sessionSecret: 'testsecret' });
  const { server, port } = await listen(app);
  try {
    const r = await req(port, 'GET', '/api/status');
    assert.equal(r.status, 401);
  } finally { server.close(); }
});

// ---- correct password → session → /api/status accessible ------------------

test('login + session grants access to status', async () => {
  const store = { adam: { 'SLR-27AUG26-CDE': { config: { core_qty: 10 }, state: { state: 'ARMED_SELL', cycles: 3 } } } };
  const storePath = await tmpFile('store.json', JSON.stringify(store));
  const app = makeApp({ storePath, password: 'secret', sessionSecret: 'testsecret' });
  const { server, port } = await listen(app);
  try {
    const login = await req(port, 'POST', '/login', { body: { password: 'secret' } });
    assert.equal(login.status, 200);
    assert.equal(login.body.ok, true);
    const cookie = login.cookie?.[0]?.split(';')[0];
    assert.ok(cookie, 'expected a session cookie');

    const status = await req(port, 'GET', '/api/status', { cookie });
    assert.equal(status.status, 200);
    assert.deepEqual(status.body.store.adam['SLR-27AUG26-CDE'].state.state, 'ARMED_SELL');
    assert.equal(status.body.store.adam['SLR-27AUG26-CDE'].state.cycles, 3);
  } finally { server.close(); }
});

// ---- wrong password → 401 -------------------------------------------------

test('wrong password rejects', async () => {
  const storePath = await tmpFile('store.json', '{}');
  const app = makeApp({ storePath, password: 'secret', sessionSecret: 'testsecret' });
  const { server, port } = await listen(app);
  try {
    const r = await req(port, 'POST', '/login', { body: { password: 'nope' } });
    assert.equal(r.status, 401);
  } finally { server.close(); }
});

// ---- dev mode (no password) → auth bypassed -------------------------------

test('dev mode: no password required', async () => {
  const storePath = await tmpFile('store.json', JSON.stringify({ adam: { X: { state: {} } } }));
  const app = makeApp({ storePath, password: null, sessionSecret: 'testsecret' });
  const { server, port } = await listen(app);
  try {
    const r = await req(port, 'GET', '/api/status');
    assert.equal(r.status, 200);
    assert.ok(r.body.store.adam);
  } finally { server.close(); }
});

// ---- /api/session tells the client whether auth is required ---------------

test('session endpoint reports auth_required', async () => {
  const storePath = await tmpFile('store.json', '{}');

  const withAuth = makeApp({ storePath, password: 'secret', sessionSecret: 'testsecret' });
  const s1 = await listen(withAuth);
  try {
    const r = await req(s1.port, 'GET', '/api/session');
    assert.equal(r.body.auth_required, true);
    assert.equal(r.body.authed, false);
  } finally { s1.server.close(); }

  const noAuth = makeApp({ storePath, password: null, sessionSecret: 'testsecret' });
  const s2 = await listen(noAuth);
  try {
    const r = await req(s2.port, 'GET', '/api/session');
    assert.equal(r.body.auth_required, false);
  } finally { s2.server.close(); }
});

// ---- missing store file returns empty view --------------------------------

test('missing store returns empty store', async () => {
  const app = makeApp({ storePath: '/tmp/definitely-does-not-exist.json', password: null });
  const { server, port } = await listen(app);
  try {
    const r = await req(port, 'GET', '/api/status');
    assert.equal(r.status, 200);
    assert.deepEqual(r.body.store, {});
  } finally { server.close(); }
});

// ---- /api/trades tails the JSONL trade log --------------------------------

test('trade log tail returns most recent events', async () => {
  const trades = [
    '{"ts":1,"event_type":"order_placed"}',
    '{"ts":2,"event_type":"order_filled"}',
    '{"ts":3,"event_type":"halt"}',
  ].join('\n') + '\n';
  const tradesPath = await tmpFile('trades.jsonl', trades);
  const app = makeApp({ storePath: '/nowhere', tradeLogPath: tradesPath, password: null });
  const { server, port } = await listen(app);
  try {
    const r = await req(port, 'GET', '/api/trades?n=2');
    assert.equal(r.status, 200);
    assert.equal(r.body.events.length, 2);
    assert.equal(r.body.events[1].event_type, 'halt');
  } finally { server.close(); }
});

// ---- logout clears session ------------------------------------------------

test('logout clears session', async () => {
  const storePath = await tmpFile('store.json', '{}');
  const app = makeApp({ storePath, password: 'secret', sessionSecret: 'testsecret' });
  const { server, port } = await listen(app);
  try {
    const login = await req(port, 'POST', '/login', { body: { password: 'secret' } });
    const cookie = login.cookie[0].split(';')[0];

    // Verify session works
    const ok = await req(port, 'GET', '/api/status', { cookie });
    assert.equal(ok.status, 200);

    await req(port, 'POST', '/logout', { cookie });
    const after = await req(port, 'GET', '/api/status', { cookie });
    assert.equal(after.status, 401);
  } finally { server.close(); }
});
