// @ci: frontend-test
/**
 * 更新检查机制测试（P-24 更新提示面，不做自动更新）。
 *
 * 行为契约：
 * - compareVersions：三段数字版本比较（段数不齐补零、容忍 v 前缀、数字比较
 *   非字典序——0.2.10 > 0.2.9）；
 * - checkForNewVersion：读版本清单 JSON（{version, download_url}），远端版本
 *   落后于本地才返回 UpdateInfo；同版本/本地更新/网络失败/响应非 JSON 一律
 *   null（不向上抛——检查失败静默，不打扰用户）；
 * - startUpdateCheck：未配置版本清单位（opts 与 AGENTOS_UPDATE_MANIFEST_URL
 *   均缺省）→ 零 fetch、零推送、零状态（诚实降级：仓内打包链无更新服务器，
 *   版本源由部署侧环境变量注入，缺省禁用——注释假设见 update-check.ts）；
 *   配置且落后 → 检查一次并 webContents.send("update:available") + 状态可拉取；
 *   检查失败 → 不推送、状态保持 null。
 *
 * Electron 运行时不进本模块（win 以结构类型注入，fetch 由参数/globalThis
 * 注入）——本测试零 mock 模块，全部真函数调用（与 kernel-liveness.test.ts
 * 的纯逻辑车道同口径）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const {
  compareVersions,
  checkForNewVersion,
  startUpdateCheck,
  currentUpdateInfo,
  resetUpdateInfoForTest,
} = await import("../update-check");

import type { UpdateInfo } from "../update-check";

/** 构造 fetch 桩：返回 JSON body / 抛错 */
function fetchStub(
  impl: (url: string) => { json?: unknown; status?: number; throw?: unknown },
): typeof fetch {
  return vi.fn((input: Parameters<typeof fetch>[0]) => {
    const spec = impl(String(input));
    if (spec.throw !== undefined) return Promise.reject(spec.throw);
    return Promise.resolve({
      ok: (spec.status ?? 200) < 400,
      status: spec.status ?? 200,
      json: async () => spec.json,
    } as Response);
  }) as unknown as typeof fetch;
}

/** 结构化假窗口（update-check 只用 webContents.send） */
function fakeWindow() {
  const sent: Array<{ channel: string; payload: unknown }> = [];
  return {
    sent,
    win: {
      webContents: {
        send: vi.fn((channel: string, payload: unknown) => {
          sent.push({ channel, payload });
        }),
      },
    },
  };
}

beforeEach(() => {
  resetUpdateInfoForTest();
  delete process.env.AGENTOS_UPDATE_MANIFEST_URL;
});

afterEach(() => {
  resetUpdateInfoForTest();
  delete process.env.AGENTOS_UPDATE_MANIFEST_URL;
  vi.restoreAllMocks();
});

describe("compareVersions 三段数字比较", () => {
  it("远端新版本 → 正数（0.2.3 > 0.2.2）", () => {
    expect(compareVersions("0.2.3", "0.2.2")).toBeGreaterThan(0);
  });

  it("数字比较非字典序：0.2.10 > 0.2.9", () => {
    expect(compareVersions("0.2.10", "0.2.9")).toBeGreaterThan(0);
  });

  it("等版本 → 0", () => {
    expect(compareVersions("0.2.2", "0.2.2")).toBe(0);
  });

  it("容忍 v 前缀：v0.2.3 与 0.2.2 可比", () => {
    expect(compareVersions("v0.2.3", "0.2.2")).toBeGreaterThan(0);
  });

  it("段数不齐补零：0.3 > 0.2.9", () => {
    expect(compareVersions("0.3", "0.2.9")).toBeGreaterThan(0);
  });

  it("反对称性：compare(a,b) = -compare(b,a)（性质断言，三组样本）", () => {
    const samples: Array<[string, string]> = [
      ["0.2.2", "0.3.0"],
      ["1.0.0", "0.9.9"],
      ["0.2.10", "0.2.2"],
    ];
    for (const [a, b] of samples) {
      expect(compareVersions(a, b)).toBe(-compareVersions(b, a));
    }
  });
});

describe("checkForNewVersion 清单读取与版本比对", () => {
  const manifest = {
    version: "0.3.0",
    download_url: "https://example.invalid/download",
  };

  it("远端落后于本地 → 返回 UpdateInfo（版本+下载地址）", async () => {
    const fetchFn = fetchStub(() => ({ json: manifest }));
    const info = await checkForNewVersion({
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    });
    expect(info).toEqual({
      latestVersion: "0.3.0",
      downloadUrl: "https://example.invalid/download",
    });
  });

  it("同版本 → null（不提示）", async () => {
    const fetchFn = fetchStub(() => ({ json: { ...manifest, version: "0.2.2" } }));
    const info = await checkForNewVersion({
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    });
    expect(info).toBeNull();
  });

  it("本地比远端新（dev 高于发布位）→ null", async () => {
    const fetchFn = fetchStub(() => ({ json: { ...manifest, version: "0.2.1" } }));
    const info = await checkForNewVersion({
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    });
    expect(info).toBeNull();
  });

  it("网络失败 → null（不向上抛，检查静默降级）", async () => {
    const fetchFn = fetchStub(() => ({ throw: new Error("network down") }));
    const info = await checkForNewVersion({
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    });
    expect(info).toBeNull();
  });

  it("响应非 JSON（json() 抛错）→ null（不向上抛）", async () => {
    const fetchFn = (async () => {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => {
          throw new Error("invalid json");
        },
      } as Response);
    }) as unknown as typeof fetch;
    const info = await checkForNewVersion({
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    });
    expect(info).toBeNull();
  });

  it("清单 JSON 缺 version 字段 → null（格式不合法按无新版本处理）", async () => {
    const fetchFn = fetchStub(() => ({ json: { download_url: "u" } }));
    const info = await checkForNewVersion({
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    });
    expect(info).toBeNull();
  });

  it("清单缺 download_url → downloadUrl 空串（前端不渲染地址段）", async () => {
    const fetchFn = fetchStub(() => ({ json: { version: "9.9.9" } }));
    const info = (await checkForNewVersion({
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    })) as UpdateInfo;
    expect(info.latestVersion).toBe("9.9.9");
    expect(info.downloadUrl).toBe("");
  });
});

describe("startUpdateCheck 触发与推送", () => {
  it("未配置版本清单位（opts 与 env 均缺省）→ 零 fetch、零推送、零状态", async () => {
    const fetchFn = fetchStub(() => ({ json: { version: "9.9.9" } }));
    const { win } = fakeWindow();
    startUpdateCheck(win, { fetchFn, localVersion: "0.2.2" });
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    expect(fetchFn).not.toHaveBeenCalled();
    expect(win.webContents.send).not.toHaveBeenCalled();
    expect(currentUpdateInfo()).toBeNull();
  });

  it("配置且远端落后 → 检查一次 + send update:available + 状态可拉取", async () => {
    const fetchFn = fetchStub(() => ({ json: { version: "0.3.0", download_url: "u" } }));
    const { win, sent } = fakeWindow();
    startUpdateCheck(win, {
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([
      {
        channel: "update:available",
        payload: { latestVersion: "0.3.0", downloadUrl: "u" },
      },
    ]);
    expect(currentUpdateInfo()).toEqual({ latestVersion: "0.3.0", downloadUrl: "u" });
  });

  it("env 注入版本清单位同样生效", async () => {
    process.env.AGENTOS_UPDATE_MANIFEST_URL = "https://env.example.invalid/latest.json";
    let requested = "";
    const fetchFn = fetchStub((url) => {
      requested = url;
      return { json: { version: "0.3.0", download_url: "u" } };
    });
    const { win } = fakeWindow();
    startUpdateCheck(win, { localVersion: "0.2.2", fetchFn });
    await new Promise((r) => setTimeout(r, 0));
    expect(requested).toBe("https://env.example.invalid/latest.json");
    expect(currentUpdateInfo()).not.toBeNull();
  });

  it("检查失败 → 不推送、状态保持 null", async () => {
    const fetchFn = fetchStub(() => ({ throw: new Error("down") }));
    const { win } = fakeWindow();
    startUpdateCheck(win, {
      manifestUrl: "https://example.invalid/latest.json",
      localVersion: "0.2.2",
      fetchFn,
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(win.webContents.send).not.toHaveBeenCalled();
    expect(currentUpdateInfo()).toBeNull();
  });
});
