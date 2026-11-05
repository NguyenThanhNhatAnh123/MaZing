const status = document.getElementById('status');
const connect = document.getElementById('connect');
const disconnect = document.getElementById('disconnect');
async function refresh() {
  const state = await chrome.storage.session.get(['connection', 'bridgeMessage']);
  disconnect.hidden = !state.connection;
  connect.disabled = !!state.connection;
  status.textContent = state.bridgeMessage || 'Chưa kết nối.';
}
connect.onclick = async () => {
  connect.disabled = true;
  try {
    const result = await chrome.runtime.sendMessage({type: 'connect',
      server: document.getElementById('server').value.trim(),
      code: document.getElementById('code').value.trim()});
    if (!result.ok) throw new Error(result.error);
    document.getElementById('code').value = '';
    await refresh();
  } catch (error) { status.textContent = error.message; connect.disabled = false; }
};
disconnect.onclick = async () => {
  const result = await chrome.runtime.sendMessage({type: 'disconnect'});
  if (!result.ok) status.textContent = result.error;
  else await refresh();
};
refresh();
