const status = document.getElementById('status');
const connect = document.getElementById('connect');
const disconnect = document.getElementById('disconnect');
const server = document.getElementById('server');
async function refresh() {
  const [state, saved] = await Promise.all([
    chrome.storage.session.get(['connection', 'bridgeMessage']),
    chrome.storage.local.get('bridgeServer')
  ]);
  server.value = state.connection?.server || saved.bridgeServer || BridgeServer.defaultOrigin;
  server.disabled = !!state.connection;
  disconnect.hidden = !state.connection;
  connect.disabled = !!state.connection;
  status.textContent = state.bridgeMessage || 'Chưa kết nối.';
}
connect.onclick = async () => {
  connect.disabled = true;
  try {
    const selectedServer = BridgeServer.normalize(server.value);
    await chrome.storage.local.set({bridgeServer: selectedServer});
    const result = await chrome.runtime.sendMessage({type: 'connect',
      server: selectedServer,
      code: document.getElementById('code').value.trim()});
    if (!result.ok) throw new Error(result.error);
    document.getElementById('code').value = '';
    await refresh();
  } catch (error) { status.textContent = error.message; connect.disabled = false; }
};
disconnect.onclick = async () => {
  try {
    const result = await chrome.runtime.sendMessage({type: 'disconnect'});
    if (!result.ok) status.textContent = result.error;
    else await refresh();
  } catch (error) { status.textContent = error.message; }
};
server.onchange = async () => {
  try {
    const selectedServer = BridgeServer.normalize(server.value);
    await chrome.storage.local.set({bridgeServer: selectedServer});
    server.value = selectedServer;
  } catch (error) { status.textContent = error.message; }
};
refresh().catch(error => { status.textContent = error.message; });
