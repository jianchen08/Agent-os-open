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
 *  - 内核 API（/api、/ext、/media、/uploads）：主进程 net.fetch 回源
 *    http://127.0.0.1:9100——同源化后前端 axios 沿用相对路径，无 CORS、
 *    无 CSP connect-src 问题（与 dev 侧 vite server.proxy 拓扑同构）。
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

/** 自定义协议名与宿主（app://bundle 为打包件前端唯一源） */
export const APP_SCHEME = "app";
export const APP_HOST = "bundle";
/** 打包件主窗口/子窗口的加载基址 */
export const APP_BASE_URL = `${APP_SCHEME}://${APP_HOST}`;

/** 内核回源地址（内核默认端口 9100，与 dev 侧 vite proxy 默认目标一致） */
const KERNEL_ORIGIN = "http://127.0.0.1:9100";

/**
 * app:// 源的内容安全策略（与 web 部署链 nginx.conf / vite dev 同口径）。
 *
 * 差异点仅 connect-src：内核 API 走 app://bundle 同源代理，但 WebSocket
 * 无法经协议层代理，打包件由渲染进程直连内核（websocket.ts app: 分支）
 * ——须显式放行内核源 http/ws://127.0.0.1:9100。'unsafe-eval' 为 ajv8
 * (RJSF v6) new Function 编译校验器所需，web 链同款。
 */
const APP_CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "connect-src 'self' http://127.0.0.1:9100 ws://127.0.0.1:9100",
  "font-src 'self' data:",
  "frame-src 'self' blob: data:",
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

/** 主进程侧回源内核：同源化代理（渲染进程侧无 CORS/预检问题） */
async function proxyToKernel(request: Request, url: URL): Promise<Response> {
  const target = KERNEL_ORIGIN + url.pathname + url.search;
  const headers = new Headers(request.headers);
  // 源相关/逐跳头不透传：Host 由 net.fetch 按目标重写，Origin/Referer 属页面源语义
  headers.delete("Origin");
  headers.delete("Referer");
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  return net.fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? request.body : undefined,
    ...(hasBody ? { duplex: "half" as const } : {}),
    redirect: "follow",
  });
}

/**
 * 安装 app:// 协议处理器。在 app ready 之后、创建任何窗口之前调用一次。
 * 处理失败一律 500（含内核不可达时的 net.fetch 异常），不向渲染进程泄漏堆栈。
 */
export function installAppProtocolHandler(): void {
  const distRoot = resolveDistRoot();
  protocol.handle(APP_SCHEME, async (request) => {
    try {
      const url = new URL(request.url);
      if (isKernelProxyPath(url.pathname)) {
        return await proxyToKernel(request, url);
      }
      return await serveStatic(distRoot, url);
    } catch (err) {
      console.error(`[Electron] app:// 请求处理失败: ${request.url}`, err);
      return new Response("app:// internal error", { status: 500 });
    }
  });
  console.info(`[Electron] app:// 协议已就绪，静态根: ${distRoot}，内核回源: ${KERNEL_ORIGIN}`);
}
