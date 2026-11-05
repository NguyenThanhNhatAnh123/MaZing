const BridgeServer = Object.freeze({
  defaultOrigin: 'https://mz.eagleglobal2023.com',
  normalize(value) {
    let url;
    try { url = new URL(String(value || '').trim()); }
    catch { throw new Error('Nhập địa chỉ MaZing HTTPS hoặc http://127.0.0.1:8780.'); }
    const production = url.origin === this.defaultOrigin;
    const loopback = url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname);
    if ((!production && !loopback) || url.username || url.password ||
        url.pathname !== '/' || url.search || url.hash) {
      throw new Error('Chỉ cho phép https://mz.eagleglobal2023.com hoặc tool HTTP trên localhost / 127.0.0.1, không kèm đường dẫn.');
    }
    return url.origin;
  },
  async readResponse(response) {
    let result;
    try { result = await response.json(); }
    catch {
      throw new Error(`MaZing trả HTTP ${response.status}, không phải JSON. Kiểm tra địa chỉ máy chủ và kết nối mạng rồi tạo mã mới nếu cần.`);
    }
    if (!result || typeof result !== 'object' || Array.isArray(result))
      throw new Error(`Phản hồi MaZing không hợp lệ (HTTP ${response.status}).`);
    if (!response.ok) {
      const detail = typeof result.error === 'string' ? result.error : 'Máy chủ chưa chấp nhận yêu cầu.';
      throw new Error(`HTTP ${response.status}: ${detail}`);
    }
    return result;
  }
});
