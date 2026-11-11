// Exercise the actual dashboard polling function without a browser or a real shop.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'lib', 'dashboard.html'), 'utf8');
const pollSource = html.slice(html.indexOf('  async function pollJob() {'),
  html.indexOf('  setInterval(pollJob, 1500);'));
const renderSource = html.slice(html.indexOf('  function renderBatches() {'),
  html.indexOf('  async function saveUploadOptions() {'));
const bindSource = html.slice(html.indexOf('  function bindBatch(p, b) {'),
  html.indexOf('  $("#btn-publish").onclick ='));
assert.ok(pollSource.startsWith('  async function pollJob() {'));

function harness(api) {
  const nodes = new Map();
  function $(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {textContent: '', style: {},
      lastElementChild: {textContent: ''}, scrollTop: 0, clientHeight: 200, scrollHeight: 200});
    return nodes.get(selector);
  }
  const context = vm.createContext({api, $, encodeURIComponent,
    renderBrowserState() {}, renderBatches() {}, loadInputs() {}});
  vm.runInContext(`let jobSince = 0, jobId = "", jobPolling = false, lastFinishedJob = "";
    let jobRunning = false, lastRunning = false, view = "input";
    const JOB_LABELS = {};
    ${pollSource}`, context);
  return {poll: () => context.pollJob(), log: () => $('#drawer-log').textContent,
    cursor: () => vm.runInContext('jobSince', context)};
}

function snapshot(jobId, total, lines) {
  return {job_id: jobId, total_lines: total, lines, running: false,
    kind: '', result: {}, progress: [0, 0], started: 100};
}

async function main() {
  for (const total of [2, 3]) {
    const responses = [snapshot('old', 2, ['old first', 'old second']),
      snapshot('new', total, Array.from({length: total}, (_, i) => 'new ' + i))];
    const requests = [];
    const page = harness(async url => { requests.push(url); return responses.shift(); });
    await page.poll();
    await page.poll();
    assert.equal(page.log(), Array.from({length: total}, (_, i) => 'new ' + i).join('\n'));
    assert.equal(page.cursor(), total);
    assert.match(requests[1], /since=2&job_id=old$/);
  }

  {
    let calls = 0, finish;
    const pending = new Promise(resolve => { finish = resolve; });
    const page = harness(() => { calls++; return pending; });
    const firstPoll = page.poll();
    await page.poll();
    assert.equal(calls, 1, 'A slow poll must not overlap a second request');
    finish(snapshot('job', 1, ['one event']));
    await firstPoll;
    assert.equal(page.log(), 'one event');
  }

  {
    let calls = 0;
    const page = harness(async () => ++calls === 1 ? {error: 'temporarily offline'} :
      snapshot('job', 1, ['after reconnect']));
    await page.poll();
    await page.poll();
    assert.equal(page.log(), 'after reconnect', 'An error must release the single-poll guard');
  }

  {
    const box = {innerHTML: ''};
    const context = vm.createContext({$: selector => selector === '#batches' ? box : {style: {}},
      $$: () => [], esc: value => value});
    vm.runInContext(`let INFO = {browser: {automation: false}}, jobRunning = false;
      const batchStartErrors = new Map();
      const BATCHES = [{batch: "test", counts: {pending: 15, done: 0, error: 0}, rows: []}];
      ${renderSource}
      renderBatches();`, context);
    assert.ok(box.innerHTML.includes('Kết nối Chrome → Bật tự động upload.'));
    vm.runInContext('INFO.browser.automation = true; renderBatches();', context);
    assert.ok(box.innerHTML.includes('Chờ upload. Bấm Upload → Bắt đầu upload.'));
    vm.runInContext('jobRunning = true; renderBatches();', context);
    assert.ok(!box.innerHTML.includes('Chờ upload.'));
  }

  for (const response of [{error: 'Upload chưa được xác nhận', request_uncertain: true},
    {error: 'Kết nối lại Chrome', automation_required: true, browser: {automation: false}}]) {
    const nodes = new Map(), calls = [];
    let confirm, opened = 0, polled = 0, browserRendered = 0;
    const $ = selector => {
      if (!nodes.has(selector)) nodes.set(selector, {innerHTML: '', style: {}, checked: true});
      return nodes.get(selector);
    };
    const context = vm.createContext({$, $$: () => [], esc: value => value,
      api: async (url, body) => { calls.push({url, body}); return response; },
      modal: (html, onOk) => { confirm = onOk; }, toast() {},
      showLog: () => { opened++; }, pollJob: () => { polled++; },
      renderBrowserState: () => { browserRendered++; }});
    vm.runInContext(`let INFO = {browser: {automation: true}, settings: {}}, jobRunning = false;
      const batchStartErrors = new Map();
      const BATCHES = [{batch: "test", counts: {pending: 15, done: 0, error: 0}, rows: []}];
      ${renderSource}
      ${bindSource}
      bindBatch({}, BATCHES[0]);`, context);
    $('.act-upload').onclick();
    assert.equal(await confirm(), false, 'A rejected start keeps its confirmation available');
    assert.equal(calls.length, 1, 'An uncertain start must never replay the POST');
    assert.equal(calls[0].url, 'api/upload/start');
    assert.equal($('#upload-start-error').textContent, response.error,
      'Show the error inside the still-open confirmation as plain text');
    assert.equal($('#upload-start-error').hidden, false);
    assert.ok($('#batches').innerHTML.includes(response.error), 'Keep the start error in the batch panel');
    vm.runInContext('renderBatches();', context);
    assert.ok($('#batches').innerHTML.includes(response.error), 'Polling/rerendering must retain the error');
    if (response.request_uncertain) {
      assert.equal(opened, 1);
      assert.equal(polled, 1, 'Reconcile an uncertain start with a safe job-status read');
    } else {
      assert.equal(vm.runInContext('INFO.browser.automation', context), false);
      assert.equal(browserRendered, 1);
      assert.ok($('#batches').innerHTML.includes('Kết nối Chrome → Bật tự động upload.'));
    }
  }

  console.log('Job polling regression checks passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
