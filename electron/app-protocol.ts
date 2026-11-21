/**
 * app:// 自定义协议：打包件前端加载链（BUG-9 修复）。
 *
 * 打包件不再用 file:// 加载 frontend/dist——file:// 下两类硬性阻断：
 *  1. vite 产物 index.html 的绝对路径引用（/assets/*）解析到盘根 → 资源 404；
 *  2. ES module script 在 file:// 源受 CORS 限制，即使路径正确也无法加载
 *     （Chromium 对 file: 源不签发 CORS 许可，module graph 整体不可用）。
 *
 * 方案：注册 standard + secure 自定义协议 app://bundle，由本模块伺服：
 *  - 静态资源：frontend/dist 按 URL path 映射（存在才按文件伺服，防路径穿越）；
 *  - SPA 深链（/p/<pageId>、/login 等无文件对应路径）：回落 index.html，
 *    createBrowserRouter 按 pathname 匹配（与 nginx try_files 同语义）；
 *  - 内核 API（/api、/ext、/media、/uploads）：主进程回源
 *    http://127.0.0.1:<内核端口>（缺省=打包件默认端口，与 dev 栈 9100 错峰）——同源化后
 *    前端 axios 沿用相对路径，无 CORS、无 CSP connect-src 问题（与 dev 侧
 *    vite server.proxy 拓扑同构）。回源客户端 = Node 侧 fetch（proxyToKernel
 *    注释载取舍），不经 Chromium 网络栈。
 *    WebSocket（/ws）无法经协议层代理，由前端按打包件形态直连内核
 *    （frontend/src/constants/websocket.ts deriveWsUrl 的 app: 分支）。
 *
 * 契约对齐：
 *  - 代理路径集合 = vite.config.ts server.proxy（去 /ws）；
 *  - 内核回源地址 = constants/websocket.ts PACKAGED_KERNEL_WS_ORIGIN 的 http 形态。
 */

import { app, net, protocol } from "electron";
import * as fs from "fs";
import * as path from "path";
import { pathToFileURL } from "url";

import { KERNEL_PORT } from "./kernel-manager";
import type { DiagLineSink } from "./renderer-health";

/** 自定义协议名与宿主（app://bundle 为打包件前端唯一源） */
export const APP_SCHEME = "app";
export const APP_HOST = "bundle";
/** 打包件主窗口/子窗口的加载基址 */
export const APP_BASE_URL = `${APP_SCHEME}://${APP_HOST}`;

/**
 * 应用自身源判定（安全审查 2026-08-19 B-5；政策单一来源，main.ts 消费）：
 *  - dev：Vite dev server（localhost:5188 / 127.0.0.1:5188，兼容 5173 默认口）；
 *  - prod：app://（打包件自定义协议源）。
 */
const APP_DEV_ORIGINS = new Set([
  "localhost:5188",
  "127.0.0.1:5188",
  "localhost:5173",
  "127.0.0.1:5173",
]);

export function isAppSource(rawUrl: string): boolean {
  try {
    const u = new URL(rawUrl);
    if (u.protocol === "app:") {
      return true;
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      return false;
    }
    return APP_DEV_ORIGINS.has(u.host);
  } catch {
    return false;
  }
}

/**
 * 顶层导航政策（死页类级清理，docs/working/文件加载器路由设计_20260928.md §10）：
 *  - 非 app/dev 源 → 阻止（审查 B-5 既有政策，防导航劫持，不回退）；
 *  - app/dev 源命中内核代理面（/api /ext /media /uploads）→ 阻止——裸内核响应
 *    （如 404 "not found" 纯文本、原始 PDF）永远不得整窗替换应用 UI；
 *  - 其余 app/dev 源（SPA 深链 /p/<id>、/__loading.html 等）→ 放行。
 */
export function shouldBlockTopFrameNavigation(rawUrl: string): boolean {
  if (!isAppSource(rawUrl)) {
    return true;
  }
  try {
    return isKernelProxyPath(new URL(rawUrl).pathname);
  } catch {
    return true;
  }
}

/**
 * 内核启动加载态页（协议内路由，不走 dist 文件）。
 *
 * 首屏在内核就绪前经本路由加载（main.ts loadInitialContent）——必须与应用
 * 同源（app://）：data: URL 的顶层导航在渲染进程网络栈退化时（如多实例
 * 共享 userData 的磁盘缓存竞争）会被 Chromium 拒绝（ERR_FAILED），而 app://
 * 由主进程 protocol.handle 伺服，与应用其余页面同一条加载链。
 */
export const APP_LOADING_PATHNAME = "/__loading.html";
/** 加载态页完整 URL（main.ts 生产首屏加载用） */
export const APP_LOADING_URL = `${APP_BASE_URL}${APP_LOADING_PATHNAME}`;

/** 加载态页内容（纯静态 HTML+CSS，无脚本；CSP 由静态安全头统一携带） */
const LOADING_PAGE_HTML = `<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>灵汐助手</title>
<style>
  html,body{height:100%;margin:0;background:#f5f6fa;font-family:"Microsoft YaHei",system-ui,sans-serif}
  .box{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;color:#4b5563}
  .spin{width:36px;height:36px;border:4px solid #d1d5db;border-top-color:#4f6ef7;border-radius:50%;animation:r 1s linear infinite}
  p{margin:0;font-size:14px}
  @keyframes r{to{transform:rotate(360deg)}}
</style></head>
<body><div class="box"><div class="spin"></div><p>正在启动灵汐助手内核，首次启动可能需要数十秒…</p></div></body>
</html>`;

/**
 * 内核回源地址（端口与内核 spawn/健康探测同源解析，resolveKernelPort 见
 * kernel-manager；AGENTOS_KERNEL_PORT 可整体错峰，缺省=打包件默认端口）。
 */
const KERNEL_ORIGIN = `http://127.0.0.1:${KERNEL_PORT}`;
/** 打包件渲染进程直连内核 WS 源（WebSocket 无法经协议层代理） */
const KERNEL_WS_ORIGIN = `ws://127.0.0.1:${KERNEL_PORT}`;

/**
 * app:// 源的内容安全策略（与 web 部署链 nginx.conf / vite dev 同口径）。
 *
 * connect-src 放行任意 http/https/ws 源：产品允许用户把应用指向**任意**远端
 * 内核（设备卡/手输地址），connect-src 只列本机回源=远端内核的 API/WS 全被
 * CSP 挡在渲染层（2026-10-22 用户实测「连远端内核直接跳登录」终因：云端
 * nginx 访问日志 0 条 /api/v1 实证——请求根本没出渲染进程）。真实访问控制仍
 * 由目标内核的 CORS 白名单 + 认证门承担；明文远端的用户提示见连接卡 P6 提示。
 *
 * 'unsafe-eval' 为 ajv8 (RJSF v6) new Function 编译校验器所需，web 链同款。
 */
const APP_CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  `connect-src 'self' http: https: ws: wss: ${KERNEL_ORIGIN} ${KERNEL_WS_ORIGIN}`,
  "font-src 'self' data:",
  // frame-src 放行项 = DSH 官方插件页（插件 `dsh_adapter` 的 `dsh_plugin_manager`
  // 页以跨源 iframe 内嵌该 SPA）。端口由 `plugins/shared/dsh_adapter/web_app.py`
  // 的 `WEB_PORT_DEFAULT` 固定为 9177——随机端口无法被静态 CSP 精确放行，唯一能
  // 放行它的写法是 `http://127.0.0.1:*` 通配（把整个回环纳入可嵌面，放宽过大）。
  // 与 Python 常量的一致性由 `plugins/shared/dsh_adapter/tests/
  // test_official_ui_csp_origin.py` 对账；dev 侧同款声明见 `frontend/vite.config.ts`。
  "frame-src 'self' blob: data: http://127.0.0.1:9177 http://localhost:9177",
  "object-src 'none'",
  "base-uri 'self'",
  "frame-ancestors 'self'",
].join("; ");

/**
 * 静态响应补安全头：protocol.handle 的 net.fetch(file:) 响应不携带任何
 * CSP，渲染进程一旦出现 XSS 注入点即无策略层约束（审查 F5）。仅作用于
 * 静态伺服面（HTML 文档所在）；内核代理面为 API/JSON 消费，不加文档头。
 */
function withStaticSecurityHeaders(resp: Response): Response {
  const headers = new Headers(resp.headers);
  headers.set("Content-Security-Policy", APP_CSP);
  headers.set("X-Content-Type-Options", "nosniff");
  return new Response(resp.body, { status: resp.status, headers });
}

/** 经主进程代理到内核的路径前缀（= vite.config.ts server.proxy 去掉 /ws） */
const KERNEL_PROXY_PREFIXES = ["/api", "/ext", "/media", "/uploads"];

/** URL path 是否命中内核代理面 */
export function isKernelProxyPath(pathname: string): boolean {
  return KERNEL_PROXY_PREFIXES.some(
    (p) => pathname === p || pathname.startsWith(p + "/"),
  );
}

/**
 * 注册 app 协议特权。必须在 app ready 之前调用（Electron 硬性时序），
 * main.ts 在模块顶层执行。
 *
 *  - standard：URL 按 host/path 规范解析（绝对路径引用 / 相对解析的前提）；
 *  - secure：视为安全上下文（localStorage/clipboard 等 Web 平台能力可用）；
 *  - supportFetchAPI + stream：渲染进程 fetch 与流式响应可用；
 *  - codeCache：V8 代码缓存，二次启动加速。
 */
export function registerAppSchemePrivileges(): void {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: APP_SCHEME,
      privileges: {
        standard: true,
        secure: true,
        supportFetchAPI: true,
        stream: true,
        codeCache: true,
      },
    },
  ]);
}

/**
 * 解析前端 dist 根目录。
 *
 * 打包件取 extraResources 解包目录（resources/frontend/dist，真实文件系统）；
 * 缺失时回落 app 路径下的 frontend/dist（app.asar 内打包副本，Electron fs 补丁
 * 对 net.fetch 的 file: URL 同样透明）。dev 直跑（electron .）取仓库 frontend/dist。
 */
function resolveDistRoot(): string {
  const candidates = [
    path.join(process.resourcesPath, "frontend", "dist"),
    path.join(app.getAppPath(), "frontend", "dist"),
  ];
  for (const dir of candidates) {
    if (fs.existsSync(path.join(dir, "index.html"))) {
      return dir;
    }
  }
  throw new Error(
    `[Electron] 前端 dist 目录不存在（含 index.html）: ${candidates.join(" | ")}`,
  );
}

/**
 * 把 URL path 安全映射到 dist 内文件，防路径穿越（.. / 盘根绝对路径）。
 * 非法或越界返回 null。
 */
function safeJoinDist(distRoot: string, urlPathname: string): string | null {
  let decoded: string;
  try {
    decoded = decodeURIComponent(urlPathname);
  } catch {
    return null; // 形如 %ZZ 的畸形编码
  }
  const root = path.resolve(distRoot);
  const target = path.resolve(root, decoded.replace(/^([/\\])+/, ""));
  if (target !== root && !target.startsWith(root + path.sep)) {
    return null;
  }
  return target;
}

/** 静态伺服 + SPA 回落：文件存在按文件（net.fetch file: 语义自动带 MIME），否则 index.html */
async function serveStatic(distRoot: string, url: URL): Promise<Response> {
  if (url.pathname !== "/") {
    const file = safeJoinDist(distRoot, url.pathname);
    if (file && fs.existsSync(file) && fs.statSync(file).isFile()) {
      return withStaticSecurityHeaders(
        await net.fetch(pathToFileURL(file).toString()),
      );
    }
  }
  // SPA 深链（createBrowserRouter pathname 匹配）：/p/<pageId>、/login 等回落入口
  return withStaticSecurityHeaders(
    await net.fetch(pathToFileURL(path.join(distRoot, "index.html")).toString()),
  );
}

/**
 * 内核回源客户端 = Node 侧 fetch（Electron main 的 global fetch 是内置 Node
 * 的 undici 实现，走 node:http 直连），不用 electron net.fetch（Chromium
 * 网络栈）。
 *
 * 取舍（2026-11-20 装机事故）：用户开启系统代理（ProxyEnable=1 → Clash 系
 * 127.0.0.1:7993）时，net.fetch 的回环回源请求被 Chromium 代理路径在拨号前
 * 吞掉（应用全进程零 TCP 拨号，Clash 对回环目标 502），且代理关闭后运行中
 * 的 Chromium 网络服务不自愈、只能重启应用——app:// 全页面「internal error」
 * 且操作全灭。undici 不读系统代理（注册表/WPAD/PAC 环境代理均不涉），回环
 * 回源恒直达内核；流式请求体直传无碍（protocol.handle 传入的 Request 即
 * main 进程 undici Request，body 为 Node stream/web 流，同 realm）。
 * 被否方案：session.setProxy({ mode: 'direct' })——作用面是该 session 全部
 * 流量，DSH 官方插件页（127.0.0.1:9177 跨源 iframe）等外部内容会被误伤；
 * 本方案只换内核回源一处客户端，零外溢。
 */

/** 主进程侧回源内核：同源化代理（渲染进程侧无 CORS/预检问题） */
async function proxyToKernel(request: Request, url: URL): Promise<Response> {
  const target = KERNEL_ORIGIN + url.pathname + url.search;
  const headers = new Headers(request.headers);
  // 源相关/逐跳头不透传：Host 由 fetch 客户端按目标重写，Origin/Referer 属页面源语义
  headers.delete("Origin");
  headers.delete("Referer");
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  return fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? request.body : undefined,
    ...(hasBody ? { duplex: "half" as const } : {}),
    redirect: "follow",
  });
}

/** 诊断错误串长度上限（防超长刷盘/防堆栈类内容整段落盘，message 链足够定位） */
export const DIAG_ERROR_MAX_CHARS = 300;

/**
 * 底层错误摘要（单行，观测面用）：沿 Error cause 链逐层取 message——undici
 * fetch 把真实失败原因包在 cause 里（顶层 message 只有无信息量的
 * "fetch failed"），不穿透链等于没记；非 Error 值 String 化；超长截断
 * （尾部省略号收尾）。
 */
export function describeDiagError(
  err: unknown,
  maxChars: number = DIAG_ERROR_MAX_CHARS,
): string {
  const parts: string[] = [];
  let cur: unknown = err;
  for (let depth = 0; depth < 5 && cur instanceof Error; depth++) {
    parts.push(cur.message || cur.name);
    cur = (cur as { cause?: unknown }).cause;
  }
  if (parts.length === 0) {
    parts.push(String(cur));
  }
  const joined = parts.join(" <- ");
  return joined.length > maxChars ? joined.slice(0, maxChars - 1) + "…" : joined;
}

/**
 * 安装 app:// 协议处理器。在 app ready 之后、创建任何窗口之前调用一次。
 * 处理失败一律 500（含内核不可达时的回源 fetch 异常），不向渲染进程泄漏
 * 堆栈；失败详情经 logDiag 落 diag-log（userData/logs/electron-main.log）——
 * 打包件 console.* 无 stdout 消费者，2026-11-20 装机事故的真实错误（URL/
 * 错误码）因此前零落盘而多耗数小时排障（logDiag 注入式接线同 renderer-health）。
 */
export function installAppProtocolHandler(logDiag: DiagLineSink): void {
  const distRoot = resolveDistRoot();
  protocol.handle(APP_SCHEME, async (request) => {
    try {
      const url = new URL(request.url);
      if (isKernelProxyPath(url.pathname)) {
        return await proxyToKernel(request, url);
      }
      if (url.pathname === APP_LOADING_PATHNAME) {
        return withStaticSecurityHeaders(
          new Response(LOADING_PAGE_HTML, {
            status: 200,
            headers: { "Content-Type": "text/html; charset=utf-8" },
          }),
        );
      }
      return await serveStatic(distRoot, url);
    } catch (err) {
      console.error(`[Electron] app:// 请求处理失败: ${request.url}`, err);
      logDiag(
        `[app-protocol] 请求处理失败 url=${request.url} err=${describeDiagError(err)}`,
      );
      return new Response("app:// internal error", { status: 500 });
    }
  });
  console.info(`[Electron] app:// 协议已就绪，静态根: ${distRoot}，内核回源: ${KERNEL_ORIGIN}`);
}
