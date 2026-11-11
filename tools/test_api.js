// Exercise the real dashboard API helper with bounded fetch/body reads and no shop writes.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'lib', 'dashboard.html'), 'utf8');
const apiSource = html.slice(html.indexOf('  async function api(path, body, method) {'),
  html.indexOf('  let offline = false;'));
assert.ok(apiSource.startsWith('  async function api(path, body, method) {'));

function harness(fetchImpl) {
  const timers = new Map(), requests = [], offline = [], redirects = [];
  let nextTimer = 0;
  class FormData {}
  const context = vm.createContext({AbortController, FormData,
    fetch: (...args) => { requests.push(args); return fetchImpl(...args); },
    setTimeout: (fn, ms) => { const id = ++nextTimer; timers.set(id, {fn, ms}); return id; },
    clearTimeout: id => timers.delete(id),
    setOffline: state => offline.push(state),
    location: {pathname: '/', search: '?tab=batches', replace: url => redirects.push(url)},
    encodeURIComponent});
  vm.runInContext(apiSource, context);
  return {call: (...args) => context.api(...args), requests, offline, redirects, FormData,
    timers,
    expire() {
      assert.equal(timers.size, 1, 'Exactly one deadline must cover the whole request');
      [...timers.values()][0].fn();
    },
    timeout() { return [...timers.values()][0]?.ms; }};
}

const response = (data, status = 200) => ({ok: status >= 200 && status < 300,
  status, json: async () => data});
const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };

async function main() {
  {
    const page = harness(async () => response({ok: true}));
    const result = await page.call('api/job?since=0&job_id=test');
    assert.equal(result.ok, true);
    assert.equal(page.requests.length, 1);
    assert.equal(page.requests[0][1].credentials, 'same-origin');
    assert.equal(page.requests[0][1].signal.aborted, false);
    assert.equal(page.timers.size, 0, 'Successful requests must clear their deadline');
    assert.deepEqual(page.offline, [false]);
  }

  {
    const page = harness(() => new Promise(() => {}));
    const pending = page.call('/api/upload/start', {batch: 'synthetic-only'});
    assert.equal(page.timeout(), 20000);
    page.expire();
    const result = await pending;
    assert.equal(result.timeout, true);
    assert.equal(result.request_uncertain, true);
    assert.match(result.error, /Mở log và kiểm tra tác vụ trước khi thử lại/);
    assert.equal(page.requests[0][1].method, 'POST');
    assert.equal(page.requests[0][1].signal.aborted, true);
    assert.equal(page.requests.length, 1, 'Timed-out writes must never be automatically retried');
    assert.equal(page.timers.size, 0);
    assert.deepEqual(page.offline, [true]);
  }

  {
    let finishBody;
    const page = harness(async () => ({status: 200, ok: true,
      json: () => new Promise(resolve => { finishBody = resolve; })}));
    const pending = page.call('api/info');
    await flush();
    assert.equal(typeof finishBody, 'function');
    assert.equal(page.timeout(), 20000, 'Receiving headers must not remove the JSON body deadline');
    page.expire();
    const result = await pending;
    assert.equal(result.timeout, true);
    assert.equal(result.request_uncertain, false);
    assert.equal(page.requests[0][1].signal.aborted, true);
    finishBody({ok: true});
    await flush();
    assert.deepEqual(page.offline, [true], 'A late body must not restore connection after timeout');
    assert.equal(page.requests.length, 1);
    assert.equal(page.timers.size, 0);
  }

  for (const endpoint of ['api/browser/status', 'api/batches', '/api/job?since=5']) {
    const page = harness(() => new Promise(() => {}));
    const pending = page.call(endpoint);
    assert.equal(page.timeout(), 20000, endpoint + ' must remain responsive');
    page.expire();
    await pending;
  }

  for (const [endpoint, upload] of [['api/campaigns?refresh=1', false], ['api/inputs', true]]) {
    const page = harness(() => new Promise(() => {}));
    const form = upload ? new page.FormData() : undefined;
    const pending = page.call(endpoint, form);
    assert.equal(page.timeout(), 180000, 'Slow reads and file transfers retain their longer deadline');
    if (upload) assert.equal(page.requests[0][1].body, form);
    page.expire();
    const result = await pending;
    assert.equal(result.request_uncertain, upload);
    assert.equal(page.requests.length, 1);
  }

  {
    const page = harness(async () => { throw new TypeError('network down'); });
    const result = await page.call('api/settings', {store_slug: 'synthetic'});
    assert.equal(result.timeout, false);
    assert.equal(result.request_uncertain, true, 'A disconnected write can also have reached the server');
    assert.equal(page.requests.length, 1);
    assert.equal(page.timers.size, 0);
    assert.deepEqual(page.offline, [true]);
  }

  {
    const page = harness(async () => ({status: 502, ok: false,
      json: async () => { throw new SyntaxError('HTML gateway response'); }}));
    const result = await page.call('api/job');
    assert.match(result.error, /HTTP 502, không phải JSON/);
    assert.deepEqual(page.offline, [false], 'A completed HTTP response is not a network timeout');
    assert.equal(page.timers.size, 0);
  }

  {
    const page = harness(async () => response({auth_required: true}, 401));
    const result = await page.call('api/info');
    assert.equal(result.auth_required, true);
    assert.deepEqual(page.redirects, ['/login?next=%2F%3Ftab%3Dbatches']);
    assert.equal(page.timers.size, 0, 'Authentication redirects must clear the deadline too');
    assert.deepEqual(page.offline, [false]);
  }

  {
    const page = harness(async () => ({status: 200, ok: true,
      json: async () => { throw new TypeError('connection reset during body'); }}));
    const result = await page.call('api/upload/start', {});
    assert.equal(result.request_uncertain, true);
    assert.deepEqual(page.offline, [true]);
    assert.equal(page.requests.length, 1);
    assert.equal(page.timers.size, 0);
  }

  console.log('Dashboard API deadline checks passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
