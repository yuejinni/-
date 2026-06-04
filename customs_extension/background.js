/**
 * Background Service Worker
 * 代理所有对 localhost 的请求，绕过 Chrome Private Network Access 限制
 * （content.js 在公网页面上下文，直接 fetch localhost 会被 Chrome 拦截）
 */
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type !== 'SERVER_FETCH') return;

  const { url, method = 'GET', headers = {}, body = null, timeout = 30000 } = msg;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  fetch(url, {
    method,
    headers,
    body,
    signal: controller.signal,
  })
    .then(r => r.json())
    .then(data => { clearTimeout(timer); sendResponse({ ok: true, data }); })
    .catch(err => { clearTimeout(timer); sendResponse({ ok: false, error: err.name === 'AbortError' ? '请求超时' : err.message }); });

  return true; // 保持消息通道开放（异步响应必须）
});
