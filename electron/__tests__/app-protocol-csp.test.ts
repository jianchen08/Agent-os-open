// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * app:// 静态伺服 CSP 回归（审查 F5，2026-09-16）。
 *
 * 行为契约：protocol.handle(APP_SCHEME) 伺服的静态响应（文件命中与 SPA
 * index.html 回落两径）必须携带 Content-Security-Policy（与 web 部署链
 * nginx/vite 同口径，connect 面额外放行内核 WS 直连源）与 nosniff；
 * 内核代理面（/api 等）为 API 消费，不加文档头。
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

describe("app:// 静态响应 CSP（审查 F5）", () => {
  let fixtureRoot: string;
  let originalResourcesPath: string | undefined;

  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "app-protocol-csp-"));
    const dist = path.join(fixtureRoot, "frontend", "dist");
    fs.mkdirSync(dist, { recursive: true });
    fs.writeFileSync(path.join(dist, "index.html"), "<html></html>");
    fs.writeFileSync(path.join(dist, "app.js"), "console.log(1)");
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

  it("SPA 回落（/）响应携带 CSP 与 nosniff", async () => {
    const handler = await install();
    const resp = await handler(new Request("app://bundle/"));
    const csp = resp.headers.get("Content-Security-Policy");
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("object-src 'none'");
    // 内核 WS 直连源（打包件 WebSocket 绕过协议层代理）
    expect(csp).toContain("connect-src 'self' http://127.0.0.1:9100 ws://127.0.0.1:9100");
    // ajv8 (RJSF) new Function 所需（web 链同款）
    expect(csp).toContain("'unsafe-eval'");
    expect(resp.headers.get("X-Content-Type-Options")).toBe("nosniff");
  });

  it("静态文件命中（/app.js）同样携带 CSP", async () => {
    const handler = await install();
    const resp = await handler(new Request("app://bundle/app.js"));
    expect(resp.headers.get("Content-Security-Policy")).toContain("default-src 'self'");
  });

  it("内核代理面（/api）不加文档安全头", async () => {
    const handler = await install();
    const resp = await handler(new Request("app://bundle/api/v1/sessions"));
    expect(resp.headers.get("Content-Security-Policy")).toBeNull();
    const calls = electronMock.net.fetch.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    expect(calls[calls.length - 1]?.[0]).toBe("http://127.0.0.1:9100/api/v1/sessions");
  });
});
