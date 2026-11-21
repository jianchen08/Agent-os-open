// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * app:// 内核回源代理（proxyToKernel）+ 处理失败观测面测试。
 *
 * 背景（2026-11-20 装机事故）：用户系统代理（ProxyEnable=1 → Clash 回环
 * 127.0.0.1:7993）开启时，net.fetch（Chromium 网络栈，自动管理系统代理）
 * 对 127.0.0.1 内核的回源请求在拨号前被代理路径吞掉，app:// 全页面
 * "internal error"；且 catch 只 console.error，打包件无 stdout 消费者，
 * 真实错误零落盘。
 *
 * 行为契约：
 *  - 内核回源走 Node 侧 fetch（undici，node:http 直连，不读系统代理——
 *    环境变量代理指到死端口仍直连成功）：真本地 http 服务器收到请求，
 *    electron net.fetch（Chromium 栈）零调用；
 *  - 方法/头（Origin/Referer 剥离、其余透传）/体/查询串透传，响应状态/
 *    头/体透传；
 *  - 回源失败（连接拒绝）→ 500 "app:// internal error" + logDiag 行含
 *    请求 URL 与底层错误 message（cause 链——undici 把真实原因包在
 *    cause 里，只取顶层 message 会落成无信息量的 "fetch failed"）；
 *  - describeDiagError：cause 链拼接、超长截断（上限常量）、非 Error 值
 *    String 化。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（app/net/protocol）；
 * 内核端口经 kernel-manager mock 钉到测试服务器；回源客户端用真实
 * undici fetch 打真回环端口（关键路径真实依赖，防拟合）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { startKernelStub } from "./kernel-stub";

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

/** 内核端口钉住面：app-protocol 模块求值时读 KERNEL_PORT 拼回源 origin */
const kernelManagerMock = vi.hoisted(() => ({
  state: { port: 0 },
}));

vi.mock("electron", () => electronMock);
// getter 形态：mock 实例在 vi.resetModules 后仍被复用，固化值会把首个用例的
// 端口泄漏到后续用例；每次属性读取都取当前 state
vi.mock("../kernel-manager", () => ({
  get KERNEL_PORT() {
    return kernelManagerMock.state.port;
  },
}));

describe("app:// 内核回源代理（系统代理隔离）", () => {
  let fixtureRoot: string;
  let originalResourcesPath: string | undefined;
  const diag: string[] = [];
  const diagSink = (line: string): void => {
    diag.push(line);
  };
  const prevProxyEnv: Record<string, string | undefined> = {};

  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    diag.length = 0;
    fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "app-kernel-proxy-"));
    const dist = path.join(fixtureRoot, "frontend", "dist");
    fs.mkdirSync(dist, { recursive: true });
    fs.writeFileSync(path.join(dist, "index.html"), "<html></html>");
    // resolveDistRoot 候选一读 process.resourcesPath（普通 node 下 undefined）
    originalResourcesPath = process.resourcesPath;
    process.resourcesPath = fixtureRoot;
    for (const key of ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy"]) {
      prevProxyEnv[key] = process.env[key];
    }
  });

  afterEach(async () => {
    for (const key of Object.keys(prevProxyEnv)) {
      if (prevProxyEnv[key] === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = prevProxyEnv[key];
      }
    }
    if (originalResourcesPath === undefined) {
      delete (process as { resourcesPath?: string }).resourcesPath;
    } else {
      process.resourcesPath = originalResourcesPath;
    }
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  });

  /** 设端口后动态 import（KERNEL_ORIGIN 在模块求值时拼入端口） */
  async function importAppProtocol(port: number) {
    kernelManagerMock.state.port = port;
    return await import("../app-protocol");
  }

  async function install(port: number): Promise<(req: Request) => Promise<Response>> {
    const mod = await importAppProtocol(port);
    mod.installAppProtocolHandler(diagSink);
    const handler = electronMock.state.handler;
    expect(handler).toBeTruthy();
    return handler as (req: Request) => Promise<Response>;
  }

  it("内核回源走 Node 侧直连：环境代理指死端口仍直达真服务器，net.fetch（Chromium 栈）零调用", async () => {
    const kernel = await startKernelStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true }),
    }));
    // 代理环境变量指死端口：任何走代理路径的回源实现都到不了真服务器
    process.env.HTTP_PROXY = "http://127.0.0.1:1";
    process.env.HTTPS_PROXY = "http://127.0.0.1:1";
    process.env.NO_PROXY = "";
    try {
      const handler = await install(kernel.port);
      const resp = await handler(
        new Request("app://bundle/api/v1/schema?x=1", {
          headers: {
            Authorization: "Bearer tok",
            Origin: "app://bundle",
            Referer: "app://bundle/p/1",
          },
        }),
      );
      expect(resp.status).toBe(200);
      expect(await resp.json()).toEqual({ ok: true });
      // Chromium 网络栈零参与（内核回源不经代理栈，静态伺服面本测试也未触达）
      expect(electronMock.net.fetch).not.toHaveBeenCalled();
      expect(kernel.requests).toHaveLength(1);
      const seen = kernel.requests[0]!;
      expect(seen.method).toBe("GET");
      expect(seen.url).toBe("/api/v1/schema?x=1");
      expect(seen.headers["authorization"]).toBe("Bearer tok");
      // 源相关头剥离（既有契约：Origin/Referer 属页面源语义，不透传内核）
      expect(seen.headers["origin"]).toBeUndefined();
      expect(seen.headers["referer"]).toBeUndefined();
    } finally {
      await kernel.close();
    }
  });

  it("POST 体透传、响应状态与自定义头透传", async () => {
    const kernel = await startKernelStub((_req, body) => ({
      status: 201,
      body: JSON.stringify({ echo: body }),
      headers: {
        "Content-Type": "application/json",
        "X-Kernel-Trace": "t-9",
      },
    }));
    try {
      const handler = await install(kernel.port);
      const resp = await handler(
        new Request("app://bundle/api/v1/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: "demo" }),
        }),
      );
      expect(resp.status).toBe(201);
      expect(resp.headers.get("X-Kernel-Trace")).toBe("t-9");
      expect(await resp.json()).toEqual({ echo: JSON.stringify({ title: "demo" }) });
      expect(electronMock.net.fetch).not.toHaveBeenCalled();
      const seen = kernel.requests[0]!;
      expect(seen.method).toBe("POST");
      expect(seen.url).toBe("/api/v1/tasks");
      expect(JSON.parse(seen.body)).toEqual({ title: "demo" });
      expect(seen.headers["content-type"]).toContain("application/json");
    } finally {
      await kernel.close();
    }
  });

  it("回源连接拒绝 → 500 app:// internal error，logDiag 行含 URL 与底层错误串（cause 链）", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    // 起服再关：拿到一个确定无监听的回环端口（连接拒绝是确定的失败源）
    const kernel = await startKernelStub(() => ({ status: 200, body: "" }));
    const deadPort = kernel.port;
    await kernel.close();
    const handler = await install(deadPort);
    const resp = await handler(new Request("app://bundle/api/v1/tasks"));
    expect(resp.status).toBe(500);
    expect(await resp.text()).toBe("app:// internal error");
    expect(diag).toHaveLength(1);
    const line = diag[0]!;
    expect(line).toContain("url=app://bundle/api/v1/tasks");
    // undici 把真实失败原因包在 cause 里：诊断行必须穿透到 ECONNREFUSED
    expect(line).toContain("ECONNREFUSED");
  });
});

describe("describeDiagError（诊断错误摘要纯函数）", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  async function fn(): Promise<(err: unknown, maxChars?: number) => string> {
    const mod = await import("../app-protocol");
    return mod.describeDiagError;
  }

  it("Error 且带 cause 链 → 逐层 message 以 ' <- ' 拼接（穿透 undici cause 包装）", async () => {
    const describeDiagError = await fn();
    const root = new TypeError("fetch failed", {
      cause: new Error("connect ECONNREFUSED 127.0.0.1:9101"),
    });
    expect(describeDiagError(root)).toBe(
      "fetch failed <- connect ECONNREFUSED 127.0.0.1:9101",
    );
  });

  it("三层 cause 链全量拼接；普通 Error 只取 message；非 Error 值 String 化", async () => {
    const describeDiagError = await fn();
    const deep = new Error("l1", {
      cause: new Error("l2", { cause: new Error("l3") }),
    });
    expect(describeDiagError(deep)).toBe("l1 <- l2 <- l3");
    expect(describeDiagError(new Error("boom"))).toBe("boom");
    expect(describeDiagError("plain string failure")).toBe("plain string failure");
    expect(describeDiagError(undefined)).toBe("undefined");
  });

  it("超长截断到上限且以省略号收尾（防超长刷盘）；不同输入产出不同摘要（防拟合）", async () => {
    const mod = await import("../app-protocol");
    const describeDiagError = mod.describeDiagError;
    const longMessage = "e".repeat(500);
    const out = describeDiagError(new Error(longMessage));
    expect(out.length).toBe(mod.DIAG_ERROR_MAX_CHARS);
    expect(out.endsWith("…")).toBe(true);
    expect(out.startsWith("eeee")).toBe(true);
    // 有区分度的第二组：短消息原样通过，不触发截断
    const short = describeDiagError(new Error("short"));
    expect(short).toBe("short");
    // 自定义上限生效
    expect(describeDiagError(new Error("abcdef"), 4)).toBe("abc…");
  });
});
