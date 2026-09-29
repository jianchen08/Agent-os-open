// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
/**
 * 顶层导航政策回归（文件加载器路由设计 §10 死页类级清理）。
 *
 * 行为契约 shouldBlockTopFrameNavigation：
 *  - 非 app 源（外部 http(s)/畸形 URL）→ 阻止（既有 B-5 政策，不回退）；
 *  - app 源 SPA 深链（/p/<id>、/__loading.html 等）→ 放行；
 *  - app 源命中内核代理面（/api /ext /media /uploads）→ 阻止——裸内核响应
 *    （如 404 "not found" 纯文本）永远不能整窗替换应用 UI；
 *  - dev 源（vite 5173/5188）同政策：内核代理路径同样阻止。
 *
 * Electron 运行时是本测试唯一 mock 的外部边界（与 app-protocol-csp.test 同款）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => ({
  app: { getAppPath: vi.fn(() => "."), isReady: vi.fn(() => true), on: vi.fn() },
  net: { fetch: vi.fn() },
  protocol: {
    registerSchemesAsPrivileged: vi.fn(),
    handle: vi.fn(),
  },
}));

vi.mock("electron", () => electronMock);

import { shouldBlockTopFrameNavigation } from "../app-protocol";

describe("顶层导航政策（死页类级清理）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    ["app://bundle/p/page-1", false, "app 源 SPA 深链放行"],
    ["app://bundle/", false, "app 源根路径放行"],
    ["app://bundle/__loading.html", false, "加载态页放行"],
    ["http://localhost:5188/p/x", false, "dev 源 SPA 深链放行"],
    ["http://127.0.0.1:5173/settings", false, "dev 5173 源放行"],
  ])("%s → 放行（%s）", (url, expected) => {
    expect(shouldBlockTopFrameNavigation(url)).toBe(expected);
  });

  it.each([
    ["app://bundle/uploads/abc.pdf", "app 源 /uploads 阻止（本 bug 入口）"],
    ["app://bundle/api/v1/sessions", "app 源 /api 阻止"],
    ["app://bundle/ext/artifacts", "app 源 /ext 阻止"],
    ["app://bundle/media/bg.png", "app 源 /media 阻止"],
    ["http://localhost:5188/uploads/x.txt", "dev 源 /uploads 同样阻止"],
    ["https://evil.example.com/anything", "非 app 源阻止（既有政策不回退）"],
    ["file:///C:/Windows/System32/calc.exe", "file: 协议阻止"],
    ["javascript:void(0)", "javascript: 协议阻止"],
    ["not a url at all", "畸形 URL 阻止（防御）"],
  ])("%s → 阻止（%s）", (url, expected) => {
    expect(shouldBlockTopFrameNavigation(url)).toBe(true);
    void expected;
  });
});
