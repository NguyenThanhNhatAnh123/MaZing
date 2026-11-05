/* Offline regression tests for the production bridge and client-side shop launcher. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const extension = path.join(root, 'chrome-extension');
const read = name => fs.readFileSync(path.join(extension, name), 'utf8');
const response = (status, body) => ({status, ok: status >= 200 && status < 300,
  json: async () => body});
const tick = () => new Promise(resolve => setImmediate(resolve));

function storageArea(initial = {}) {
  const data = {...initial};
  return {data, get: async () => ({...data}), set: async value => Object.assign(data, value),
    remove: async keys => { for (const key of [].concat(keys)) delete data[key]; }};
}

function serverContext() {
  const context = vm.createContext({URL});
  vm.runInContext(read('server.js') + '\nthis.server = BridgeServer;', context);
  return context;
}

function backgroundContext() {
  const session = storageArea(), local = storageArea(), requests = [];
  let listener;
  const chrome = {storage: {session, local},
    tabs: {query: async () => [{id: 21, url: 'https://dashboard.mayzing.com/dashboard'}],
      get: async () => ({id: 21, url: 'https://dashboard.mayzing.com/dashboard'})},
    scripting: {executeScript: async () => [{result: {ready: true}}]},
    action: {setBadgeText: async () => {}},
    runtime: {id: 'test-extension', getManifest: () => ({version: '1.2.0'}),
      onMessage: {addListener: handler => { listener = handler; }}}};
  const context = vm.createContext({chrome, URL, AbortController,
    setInterval: () => 1, clearInterval: () => {},
    fetch: async (url, options) => {
      requests.push({url, options});
      if (url.endsWith('/poll')) return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => {
          const error = new Error('Aborted'); error.name = 'AbortError'; reject(error);
        });
      });
      return response(200, url.endsWith('/pair') ? {token: 'device-one-token'} : {ok: true});
    }});
  context.importScripts = (...names) => names.forEach(name => vm.runInContext(read(name), context));
  vm.runInContext(read('background.js'), context);
  const send = message => new Promise(resolve => listener(message, {id: 'test-extension'}, resolve));
  return {chrome, session, local, requests, send};
}

async function testServer() {
  const {server} = serverContext();
  for (const value of ['https://mz.eagleglobal2023.com', 'https://mz.eagleglobal2023.com/',
    'https://mz.eagleglobal2023.com:443/'])
    assert.equal(server.normalize(value), 'https://mz.eagleglobal2023.com');
  assert.equal(server.normalize('http://127.0.0.1:8780/'), 'http://127.0.0.1:8780');
  assert.equal(server.normalize('http://localhost:9000/'), 'http://localhost:9000');
  for (const value of ['', 'not a URL', 'http://mz.eagleglobal2023.com',
    'https://mz.eagleglobal2023.com:8443', 'https://mz.eagleglobal2023.com.evil.test',
    'https://evil.test', 'https://localhost:8780', 'http://192.168.1.1:8780',
    'https://user:pass@mz.eagleglobal2023.com', 'https://mz.eagleglobal2023.com/api',
    'https://mz.eagleglobal2023.com?token=x', 'https://mz.eagleglobal2023.com/#x'])
    assert.throws(() => server.normalize(value), undefined, value);
  assert.equal((await server.readResponse(response(200, {token: 'abc'}))).token, 'abc');
  await assert.rejects(server.readResponse(response(403, {error: 'Wrong origin'})), /HTTP 403: Wrong origin/);
  await assert.rejects(server.readResponse({status: 502, ok: false, json: async () => {
    throw new SyntaxError('HTML');
  }}), /HTTP 502, không phải JSON/);
  await assert.rejects(server.readResponse(response(200, null)), /không hợp lệ/);
}

async function testPairing() {
  const app = backgroundContext();
  await tick();
  const rejected = await app.send({type: 'connect', server: 'https://evil.test', code: 'wrong'});
  assert.equal(rejected.ok, false);
  assert.equal(app.requests.length, 0);
  const result = await app.send({type: 'connect', server: 'https://mz.eagleglobal2023.com', code: '  ABC123  '});
  assert.equal(result.ok, true);
  await tick();
  assert.equal(app.local.data.bridgeServer, 'https://mz.eagleglobal2023.com');
  assert.equal(app.session.data.connection.token, 'device-one-token');
  const pair = app.requests.find(request => request.url.endsWith('/pair'));
  assert.equal(pair.options.credentials, 'omit');
  assert.equal(pair.options.redirect, 'error');
  assert.equal(JSON.parse(pair.options.body).code, 'ABC123');
  const poll = app.requests.find(request => request.url.endsWith('/poll'));
  assert.equal(poll.options.headers.Authorization, 'Bearer device-one-token');
  assert.equal((await app.send({type: 'disconnect'})).ok, true);
  await tick();
  assert.equal(app.session.data.connection, undefined);
  assert.equal(app.local.data.bridgeServer, 'https://mz.eagleglobal2023.com');
}

async function testPopup() {
  const elements = Object.fromEntries(['status', 'connect', 'disconnect', 'server', 'code'].map(id => [id, {value: '', textContent: ''}]));
  const local = storageArea({bridgeServer: 'http://localhost:9000'}), session = storageArea();
  const context = vm.createContext({URL, document: {getElementById: id => elements[id]},
    chrome: {storage: {session, local}, runtime: {sendMessage: async () => ({ok: true})}}});
  vm.runInContext(read('server.js'), context);
  vm.runInContext(read('popup.js'), context);
  await tick();
  assert.equal(elements.server.value, 'http://localhost:9000');
  elements.server.value = 'https://mz.eagleglobal2023.com/';
  await elements.server.onchange();
  assert.equal(local.data.bridgeServer, 'https://mz.eagleglobal2023.com');
  assert.equal(elements.server.value, 'https://mz.eagleglobal2023.com');
  session.data.connection = {server: 'http://127.0.0.1:8780'};
  await vm.runInContext('refresh()', context);
  assert.equal(elements.server.value, 'http://127.0.0.1:8780');
  assert.equal(elements.server.disabled, true);
}

function testManifestAndDashboard() {
  const manifest = JSON.parse(read('manifest.json'));
  assert.equal(manifest.version, '1.2.0');
  assert.deepEqual(manifest.permissions, ['activeTab', 'scripting', 'storage']);
  assert.deepEqual(manifest.host_permissions, ['https://mz.eagleglobal2023.com/*',
    'http://127.0.0.1/*', 'http://localhost/*']);
  const html = fs.readFileSync(path.join(root, 'lib', 'dashboard.html'), 'utf8');
  assert.match(html, /<a[^>]*id="btn-login"[^>]*href="https:\/\/dashboard\.mayzing\.com\/dashboard"[^>]*target="_blank"/);
  assert.doesNotMatch(html, /api\("api\/login\/start"/);
  assert.match(html, /credentials: "same-origin"/);
  for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))
    new vm.Script(match[1], {filename: 'dashboard-inline.js'});
}

(async () => {
  await testServer(); await testPairing(); await testPopup(); testManifestAndDashboard();
  process.stdout.write('Extension production bridge tests passed.\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
