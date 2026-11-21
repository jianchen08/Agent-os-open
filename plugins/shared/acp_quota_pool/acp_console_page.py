"""acp_quota_pool 网关控制台 webview 页（独名模块，server 薄引用）。

形态（2026-10-16 用户裁定：不自渲染任何额度数据；2026-11-12 修订：不自导航嵌页；
2026-11-21 修订：拉起按钮语义移交——拉起职责在 llm_provider_gateway 插件，
本页只探活与展示移交提示）。
页面只做三件事：
① 探网关状态（经宿主桥 fetch 本插件端点），在运行 → 显式「网关运行中」卡片 +
  「新窗口打开管理页」按钮（window.open 走沙箱 allow-popups，新窗口是真实
  origin，网关管理页 SPA 可正常运行）。**不自导航嵌页**：网关管理页是 React
  SPA（强依赖 localStorage/sessionStorage，实证 management.html 含 localStorage
  引用 34 处），iframe 自导航后沙箱旗标随浏览上下文继承、SPA 仍在 opaque
  origin 内运行，访问 storage 抛 SecurityError 初始化崩溃 → 整页空白
  （2026-11-12 用户实测装机白屏根因）；
② 未运行 → 显式「网关未运行」态 + 移交提示（拉起由 llm_provider_gateway 插件
  承接——捆绑网关自持拉起）+「重新检查」按钮（重探本插件 status 端点）；
③ 零自定义数据渲染、不碰管理密钥（登录与「记住密码」由用户在网关页内完成）。

样式走宿主主题令牌：WebviewWidget 经 theme.sync 下行 ``--ag-*`` token map
（webviewThemeTokens 固定 12 键），本页监听后写回 :root；var() 缺省值兜底首绘。
"""

from __future__ import annotations

CONSOLE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>ACP 网关控制台</title>
<style>
  :root { --ag-bg:#f8fafc; --ag-fg:#1a1a1a; --ag-muted:#64748b; --ag-border:#e2e8f0;
          --ag-card:#ffffff; --ag-accent:#2563eb; --ag-accent-soft:rgba(37,99,235,.14);
          --ag-chip:#f1f5f9; --ag-ok:#16a34a; --ag-warn:#d97706; --ag-err:#dc2626;
          --ag-radius:8px; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         font-family: -apple-system, 'Segoe UI', sans-serif; font-size: 13px;
         color: var(--ag-fg); background: var(--ag-bg); }
  .card { background: var(--ag-card); border: 1px solid var(--ag-border);
          border-radius: var(--ag-radius); padding: 28px 32px; min-width: 320px;
          max-width: 460px; text-align: center; }
  h2 { margin: 0 0 10px; font-size: 15px; }
  .detail { color: var(--ag-muted); font-size: 12px; word-break: break-all; margin: 6px 0 0; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         margin-right: 6px; vertical-align: middle; }
  .dot.ok { background: var(--ag-ok); }
  .dot.err { background: var(--ag-err); }
  button { margin-top: 16px; padding: 6px 18px; background: var(--ag-accent); color: #fff;
           border: none; border-radius: var(--ag-radius); cursor: pointer; font-size: 13px; }
  button:hover { filter: brightness(1.08); }
  button:disabled { opacity: .55; cursor: default; }
  a { color: var(--ag-accent); font-size: 12px; word-break: break-all; }
  .row { margin-top: 12px; }
</style></head><body>
<div class="card">
  <h2>ACP 额度网关控制台</h2>
  <div id="state"><span class="dot"></span>正在检查网关状态...</div>
  <div class="detail" id="detail"></div>
  <div class="row" id="actions"></div>
</div>
<script>
function agentosFetch(path, params) {
  return new Promise(function(resolve, reject) {
    if (!window.agentos) { reject(new Error('window.agentos 不可用（须在 AgentOS 面板内打开）')); return; }
    var id = window.agentos.postMessage(path, params);
    function handler(e) {
      var d = e.data;
      if (!d || !d.__agentos_webview || d.id !== id) return;
      window.removeEventListener('message', handler);
      if (d.method && d.method.indexOf('.error') > -1) reject(new Error(JSON.stringify(d.params || d.error)));
      else resolve(d.params);
    }
    window.addEventListener('message', handler);
  });
}
function unwrap(res) {
  var data = (res && res.data) ? res.data : res;
  if (data && data.body) {
    try { return JSON.parse(atob(data.body)); } catch(e) { return data; }
  }
  return data;
}
// 主题令牌桥：宿主 theme.sync 下行 --ag-* token map 写回 :root（首绘靠 CSS 缺省值）
window.addEventListener('message', function(e) {
  var d = e.data;
  if (!d || !d.__agentos_webview) return;
  if (d.method === 'theme.sync' && d.params && d.params.tokens) {
    var tokens = d.params.tokens;
    for (var key in tokens) {
      if (Object.prototype.hasOwnProperty.call(tokens, key)) {
        document.documentElement.style.setProperty(key, tokens[key]);
      }
    }
  }
});
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function setHtml(id, html) { document.getElementById(id).innerHTML = html; }
function renderRunning(url) {
  setHtml('state', '<span class="dot ok"></span>网关运行中');
  setHtml('detail', '');
  var actions = document.getElementById('actions');
  actions.innerHTML = '';
  var btn = document.createElement('button');
  btn.id = 'btn_open';
  btn.textContent = '新窗口打开管理页';
  btn.addEventListener('click', function() { window.open(url); });
  actions.appendChild(btn);
}
function renderStopped(detail) {
  setHtml('state', '<span class="dot err"></span>网关未运行');
  setHtml('detail', escapeHtml(detail || ''));
  setHtml('actions', '<div class="detail">拉起由 llm_provider_gateway 插件托管（捆绑网关自持拉起）</div>'
    + '<button id="btn_recheck">重新检查</button>');
  document.getElementById('btn_recheck').addEventListener('click', boot);
}
async function boot() {
  try {
    var data = unwrap(await agentosFetch('/ext/acp_quota_pool/gateway/status'));
    if (data.management_url && data.running) {
      renderRunning(data.management_url);
    } else {
      renderStopped(data.detail);
    }
  } catch(e) {
    renderStopped('状态检查失败: ' + String(e));
  }
}
boot();
</script></body></html>
"""

__all__ = ["CONSOLE_HTML"]
