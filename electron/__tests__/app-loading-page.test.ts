// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
// @ci: frontend-test
/**
 * app:// 内核启动加载态页（协议内路由 /__loading.html）。
 *
 * 行为契约：加载态页由主进程 protocol.handle 伺服（与应用同源），携带与其他
 * app:// 文档相同的静态安全头（CSP + nosniff）；内容为无脚本纯静态页；仅精确
 * path 命中，不遮蔽静态文件伺服 / SPA 回落 / 内核代理面。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（app/net/protocol）；
 * dist 根用临时目录夹具 + 真实文件系统（safeJoinDist 走真分支）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

const electronMock = vi.hoisted(() => {
  const state = {
    handler: null as null | ((req: Request) => Promise<Response>),
  };
  return {
    state,
    app: { getAppPath: vi.fn(() => ".") },
    net: { fetch: vi.fn(async () => new Response("<html></html>")) },
    protocol: {
      registerSchemesAsPrivileged: vi.fn(),
      handle: vi.fn((_scheme: string, h: (req: Request) => Promise<Response>) => {
        state.handler = h;
      }),
    },
  };
});

vi.mock("electron", () => electronMock);

import { APP_LOADING_PATHNAME, APP_LOADING_URL } from "../app-protocol";

describe("app:// 内核启动加载态页", () => {
  let fixtureRoot: string;
  let originalResourcesPath: string | undefined;

  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "app-loading-page-"));
    const dist = path.join(fixtureRoot, "frontend", "dist");
    fs.mkdirSync(dist, { recursive: true });
    fs.writeFileSync(path.join(dist, "index.html"), "<html></html>");
    // resolveDistRoot 候选一读 process.resourcesPath（普通 node 下 undefined）
    originalResourcesPath = process.resourcesPath;
    process.resourcesPath = fixtureRoot;
  });

  afterEach(() => {
    if (originalResourcesPath === undefined) {
      delete (process as { resourcesPath?: string }).resourcesPath;
    } else {
      process.resourcesPath = originalResourcesPath;
    }
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  });

  async function install(): Promise<(req: Request) => Promise<Response>> {
    const mod = await import("../app-protocol");
    mod.installAppProtocolHandler();
    const handler = electronMock.state.handler;
    expect(handler).toBeTruthy();
    return handler as (req: Request) => Promise<Response>;
  }

  it("加载态 URL 指向 app:// 源的 /__loading.html（与前端同源，不用 data:）", () => {
    expect(APP_LOADING_PATHNAME).toBe("/__loading.html");
    expect(APP_LOADING_URL).toBe("app://bundle/__loading.html");
  });

  it("精确 path 返回 200 text/html，携带 CSP + nosniff，内容无脚本", async () => {
    const handler = await install();
    const resp = await handler(new Request(APP_LOADING_URL));
    expect(resp.status).toBe(200);
    expect(resp.headers.get("Content-Type")).toContain("text/html");
    expect(resp.headers.get("Content-Security-Policy")).toContain("default-src 'self'");
    expect(resp.headers.get("X-Content-Type-Options")).toBe("nosniff");
    const body = await resp.text();
    // 加载态文案（内核拉起期间的 UI 反馈）
    expect(body).toContain("正在启动");
    // 无脚本承诺：任何 CSP 口径下都可用
    expect(body).not.toContain("<script");
  });

  it("非精确 path 不走加载态路由：深层路径回落 SPA，文件命中按文件", async () => {
    const handler = await install();
    const deep = await handler(new Request("app://bundle/__loading.html/extra"));
    expect(await deep.text()).toBe("<html></html>"); // SPA 回落 index.html
    const root = await handler(new Request("app://bundle/"));
    expect(await root.text()).toBe("<html></html>");
  });
});
