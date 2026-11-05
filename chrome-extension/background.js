importScripts('operations.js', 'server.js');

let activeConnection = null;
let loopRunning = false;
let pollAbort = null;

function isMayzingUrl(value) {
  try { return new URL(value).origin === 'https://dashboard.mayzing.com'; }
  catch { return false; }
}

async function callServer(connection, path, body, signal) {
  let response;
  try {
    response = await fetch(BridgeServer.normalize(connection.server) + '/api/bridge/' + path, {
      method: 'POST', credentials: 'omit', redirect: 'error', signal,
      headers: {'Content-Type': 'application/json', ...(connection.token ?
        {'Authorization': 'Bearer ' + connection.token} : {})},
      body: JSON.stringify(body || {})
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('Không kết nối được ' + connection.server + '. Kiểm tra mạng, quyền truy cập trang của tiện ích và trạng thái MaZing.');
  }
  return BridgeServer.readResponse(response);
}

// These functions are serialized by chrome.scripting and run only in the selected tab.
function probePage() {
  if (location.origin !== 'https://dashboard.mayzing.com')
    return {ready: false, message: 'Mở tab dashboard Mayzing đã đăng nhập.'};
  const title = document.title.trim().toLowerCase().replace(/[.!…]+$/g, '');
  if (['just a moment', 'chờ một chút'].includes(title) ||
      document.querySelector('#challenge-running, #challenge-stage, #challenge-form, iframe[src*="challenges.cloudflare.com"]'))
    return {ready: false, message: 'Hoàn tất xác minh trực tiếp trong Chrome rồi kết nối lại.'};
  const ready = !!document.querySelector('script[src*="/_next/"]');
  return {ready, message: ready ? 'Tab Mayzing sẵn sàng.' : 'Đăng nhập Mayzing và chờ dashboard tải xong.'};
}

async function executeMayzing(data) {
  if (location.origin !== 'https://dashboard.mayzing.com')
    return {kind: 'not_ready', message: 'Tab không còn ở Mayzing.'};
  const title = document.title.trim().toLowerCase().replace(/[.!…]+$/g, '');
  if (['just a moment', 'chờ một chút'].includes(title) ||
      document.querySelector('#challenge-running, #challenge-stage, #challenge-form, iframe[src*="challenges.cloudflare.com"]'))
    return {kind: 'verification', message: 'Mayzing yêu cầu xác minh. Tool đã dừng; bạn hoàn tất trực tiếp trên trang.'};
  if (!document.querySelector('script[src*="/_next/"]'))
    return {kind: 'not_ready', message: 'Dashboard chưa sẵn sàng. Đăng nhập thủ công trước.'};
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), data.timeoutMs);
  try {
    let body;
    const headers = {'Apollo-Require-Preflight': 'true'};
    if (data.operation === 'ImageUpload') {
      const binary = atob(data.upload.b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      body = new FormData();
      body.append('operations', JSON.stringify({operationName: data.operation, query: data.query,
        variables: {file: null, imageUploadInput: {}}}));
      body.append('map', JSON.stringify({'1': ['variables.file']}));
      body.append('1', new File([bytes], data.upload.name, {type: data.upload.mime}));
    } else {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify({operationName: data.operation, query: data.query, variables: data.variables});
    }
    const response = await fetch('/api/graphql', {method: 'POST', credentials: 'include',
      headers, body, signal: controller.signal});
    if (response.headers.get('cf-mitigated') === 'challenge')
      return {kind: 'verification', message: 'Mayzing yêu cầu xác minh. Kiểm tra trang trước khi tiếp tục.'};
    return {kind: 'response', status: response.status, text: await response.text()};
  } catch (error) {
    return {kind: 'uncertain', message: 'Chưa nhận được kết quả: ' + String(error)};
  } finally { clearTimeout(timer); }
}

async function pageState(connection) {
  try {
    const tab = await chrome.tabs.get(connection.tabId);
    if (!isMayzingUrl(tab.url)) return {ready: false, message: 'Tab đã chuyển trang. Mở Mayzing rồi kết nối lại.'};
    const results = await chrome.scripting.executeScript({target: {tabId: connection.tabId},
      world: 'MAIN', func: probePage});
    return {...(results[0]?.result || {ready: false, message: 'Chưa đọc được trạng thái tab.'}),
      extensionVersion: chrome.runtime.getManifest().version};
  } catch {
    return {ready: false, message: 'Tab đã đóng hoặc quyền truy cập đã hết. Kết nối lại từ tab Mayzing.'};
  }
}

async function executeCommand(connection, command) {
  if (!Object.hasOwn(OPERATIONS, command.operation))
    return {kind: 'not_ready', message: 'Lệnh Mayzing không được hỗ trợ.'};
  const payload = command.payload || {};
  const data = {operation: command.operation, query: OPERATIONS[command.operation],
    variables: payload.variables || {}, upload: payload.upload,
    timeoutMs: Math.min(Number(payload.timeoutMs) || 120000, 300000)};
  if (command.operation === 'ImageUpload' && (!data.upload || typeof data.upload.b64 !== 'string'))
    return {kind: 'not_ready', message: 'Thiếu file artwork.'};
  try {
    const results = await chrome.scripting.executeScript({target: {tabId: connection.tabId},
      world: 'MAIN', func: executeMayzing, args: [data]});
    return results[0]?.result || {kind: 'uncertain', message: 'Chrome chưa trả kết quả thao tác.'};
  } catch (error) {
    return {kind: 'uncertain', message: String(error)};
  }
}

async function saveMessage(message) {
  await chrome.storage.session.set({bridgeMessage: message});
}

async function runLoop() {
  if (loopRunning || !activeConnection) return;
  loopRunning = true;
  const connection = activeConnection;
  let heartbeatBusy = false;
  const heartbeat = setInterval(async () => {
    if (heartbeatBusy || activeConnection !== connection) return;
    heartbeatBusy = true;
    try {
      await chrome.storage.session.get('connection');
      await callServer(connection, 'heartbeat', {page: await pageState(connection)});
    } catch { /* The main loop reports errors and stops. */ }
    finally { heartbeatBusy = false; }
  }, 8000);
  try {
    while (activeConnection === connection) {
      await chrome.storage.session.get('connection');
      pollAbort = new AbortController();
      const result = await callServer(connection, 'poll', {page: await pageState(connection)}, pollAbort.signal);
      if (activeConnection !== connection) break;
      if (result.command) {
        const reply = await executeCommand(connection, result.command);
        // A failed acknowledgement never causes a command to execute a second time.
        await callServer(connection, 'result', {id: result.command.id, result: reply});
      }
    }
  } catch (error) {
    if (activeConnection === connection) {
      activeConnection = null;
      await chrome.storage.session.remove('connection');
      await saveMessage('Đã dừng kết nối: ' + String(error));
      await chrome.action.setBadgeText({text: ''});
    }
  } finally {
    clearInterval(heartbeat);
    loopRunning = false;
    pollAbort = null;
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  (async () => {
    if (message.type === 'connect') {
      if (activeConnection || loopRunning) throw new Error('Ngắt kết nối cũ trước khi ghép lại.');
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      if (!tab?.id || !isMayzingUrl(tab.url)) throw new Error('Mở tab dashboard.mayzing.com đã đăng nhập rồi bấm tiện ích.');
      const candidate = {server: BridgeServer.normalize(message.server), tabId: tab.id};
      await chrome.storage.local.set({bridgeServer: candidate.server});
      const page = await pageState(candidate);
      if (!page.ready) throw new Error(page.message);
      const paired = await callServer(candidate, 'pair', {code: String(message.code || '').trim()});
      if (typeof paired.token !== 'string' || !paired.token)
        throw new Error('Máy chủ chưa cấp phiên kết nối. Tạo mã mới trên đúng dashboard MaZing rồi thử lại.');
      activeConnection = {...candidate, token: paired.token};
      await chrome.storage.session.set({connection: activeConnection, bridgeMessage: 'Đã nối tab Mayzing. Quay lại tool và bấm Bật tự động upload.'});
      await chrome.action.setBadgeText({text: 'LINK'});
      runLoop();
      return {ok: true};
    }
    if (message.type === 'disconnect') {
      const previous = activeConnection;
      activeConnection = null;
      if (pollAbort) pollAbort.abort();
      await chrome.storage.session.remove('connection');
      if (previous) {
        try { await callServer(previous, 'disconnect', {}); } catch { /* Already disconnected. */ }
      }
      await chrome.action.setBadgeText({text: ''});
      await saveMessage('Đã ngắt kết nối. Chrome vẫn mở bình thường.');
      return {ok: true};
    }
    return {ok: false, error: 'Lệnh không được hỗ trợ.'};
  })().then(sendResponse, error => sendResponse({ok: false, error: String(error.message || error)}));
  return true;
});

chrome.storage.session.get('connection').then(async state => {
  if (!state.connection) return;
  try {
    state.connection.server = BridgeServer.normalize(state.connection.server);
    activeConnection = state.connection;
    runLoop();
  } catch {
    await chrome.storage.session.remove('connection');
    await saveMessage('Địa chỉ kết nối cũ không hợp lệ. Mở tiện ích để ghép lại.');
  }
});
