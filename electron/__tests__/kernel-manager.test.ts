// @ci: frontend-test
/**
 * kernel-manager 纯函数层单测（node 环境，不触碰 Electron 运行时）。
 *
 * 覆盖口径：断行为（输入→输出），fetch 仅 mock 外部边界（内核 HTTP 端点）；
 * 时序用真实定时器 + 注入的可调小延迟，不用零延迟 mock。
 * spawn/taskkill 副作用层由打包件手工验收清单覆盖（见 ADR 验收节）。
 */

import * as path from "path";

import { describe, expect, it, vi } from "vitest";

import * as fs from "fs";
import * as os from "os";

import {
  KERNEL_DEFAULT_PORT,
  KERNEL_HEALTH_URL,
  KERNEL_PORT,
  KERNEL_RESTART_DELAYS_MS,
  KERNEL_RESTART_STABLE_MS,
  KernelMissingError,
  KernelPortBusyError,
  buildKernelEnv,
  ensurePackagedKernelRunning,
  kernelResourcePaths,
  planKernelRestart,
  probeKernelHealth,
  resolveKernelPort,
  resolveTokenSecret,
  shutdownManagedKernel,
  waitForKernelHealth,
} from "../kernel-manager";

describe("kernelResourcePaths", () => {
  // 布局契约以 path.relative 断言（不硬编码分隔符，测试主机分隔符不影响）
  it("win32：内核 exe 带 .exe 后缀，工作目录/插件/配置指向 resources 布局", () => {
    const p = kernelResourcePaths("C:\\app\\resources", "win32");
    expect(path.basename(p.kernelExe)).toBe("agentos-kernel.exe");
    expect(path.relative("C:\\app\\resources", p.kernelExe)).toBe(path.join("kernel", "agentos-kernel.exe"));
    expect(path.relative("C:\\app\\resources", p.kernelDir)).toBe("kernel");
    expect(path.relative("C:\\app\\resources", p.pluginsDir)).toBe(path.join("plugins", "shared"));
    expect(path.relative("C:\\app\\resources", p.configRoot)).toBe("config");
  });

  it("linux/darwin：内核 exe 为无扩展名占位名（与 extraResources unix 产物名同口径）", () => {
    for (const platform of ["linux", "darwin"] as const) {
      const p = kernelResourcePaths("/opt/app/resources", platform);
      expect(path.basename(p.kernelExe)).toBe("agentos-kernel");
      expect(path.relative("/opt/app/resources", p.kernelExe)).toBe(path.join("kernel", "agentos-kernel"));
    }
  });

  it("性质：所有路径都落在 resourcesPath 之内（两平台）", () => {
    for (const [root, platform] of [
      ["C:\\app\\resources", "win32"],
      ["/opt/app/resources", "linux"],
    ] as const) {
      const p = kernelResourcePaths(root, platform);
      for (const v of [p.kernelExe, p.kernelDir, p.pluginsDir, p.configRoot]) {
        const rel = path.relative(root, v);
        expect(rel).not.toBe("");
        expect(rel.startsWith("..")).toBe(false);
      }
    }
  });
});

describe("buildKernelEnv", () => {
  const paths = kernelResourcePaths("C:\\app\\resources", "win32");

  it("空白基座：写入绑定面/插件根/配置根/双源裁决/venv 自愈/空闲回收六个变量", () => {
    const env = buildKernelEnv({}, paths);
    expect(env.AGENTOS_BIND).toBe("127.0.0.1");
    expect(env.AGENTOS_PLUGINS_DIR).toBe(paths.pluginsDir);
    expect(env.AGENTOS_CONFIG_ROOT).toBe(paths.configRoot);
    expect(env.AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS).toBe("300");
  });

  it("双源裁决置内置优先（BUG-55：用户空间陈旧副本不得压过打包新版）", () => {
    const env = buildKernelEnv({}, paths);
    expect(env.AGENTOS_PLUGIN_SOURCE_PRIORITY).toBe("builtin");
  });

  it("venv 自愈门开（装机链无 dev launcher，boot 后台 uv sync 重建缺 .venv 的 sidecar）", () => {
    const env = buildKernelEnv({}, paths);
    expect(env.AGENTOS_PLUGIN_VENV_AUTOPROVISION).toBe("1");
  });

  it("AGENTOS_KERNEL_PORT 钉为解析端口：ambient 值不致内核监听与探测分叉", () => {
    const env = buildKernelEnv({ AGENTOS_KERNEL_PORT: "1" }, paths);
    expect(env.AGENTOS_KERNEL_PORT).toBe(String(KERNEL_PORT));
  });

  it("保留基座既有变量（Windows 子进程需要 SystemRoot 等）", () => {
    const env = buildKernelEnv({ SystemRoot: "C:\\Windows", PATH: "X" }, paths);
    expect(env.SystemRoot).toBe("C:\\Windows");
    expect(env.PATH).toBe("X");
  });

  it("打包确定性：ambient 的 AGENTOS_BIND 被覆盖为 127.0.0.1（不继承外部值）", () => {
    for (const ambient of ["0.0.0.0", "192.168.1.9"]) {
      const env = buildKernelEnv({ AGENTOS_BIND: ambient }, paths);
      expect(env.AGENTOS_BIND).toBe("127.0.0.1");
    }
  });

  it("性质：不修改传入的 base 对象", () => {
    const base: Record<string, string> = { A: "1" };
    buildKernelEnv(base, paths);
    expect(base).toEqual({ A: "1" });
  });
});

describe("probeKernelHealth", () => {
  it("200 → true，且探测的是内核 /health 端点", async () => {
    const calls: string[] = [];
    const fetchImpl = (async (url: string) => {
      calls.push(String(url));
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    await expect(probeKernelHealth(fetchImpl)).resolves.toBe(true);
    expect(calls).toEqual([KERNEL_HEALTH_URL]);
  });

  it("非 200 → false（404/500 两档区分输入）", async () => {
    for (const status of [404, 500]) {
      const fetchImpl = (async () => new Response("err", { status })) as typeof fetch;
      await expect(probeKernelHealth(fetchImpl)).resolves.toBe(false);
    }
  });

  it("网络异常（连接拒绝）→ false 而非抛出", async () => {
    const fetchImpl = (async () => {
      throw new Error("ECONNREFUSED");
    }) as typeof fetch;
    await expect(probeKernelHealth(fetchImpl)).resolves.toBe(false);
  });

  it("默认探测 URL 钉在 127.0.0.1:<解析端口>/health（与内核 spawn/app:// 回源同源）", () => {
    expect(KERNEL_HEALTH_URL).toBe(`http://127.0.0.1:${KERNEL_PORT}/health`);
  });
});

describe("waitForKernelHealth", () => {
  it("探测翻转后返回 true（真实小间隔轮询）", async () => {
    let calls = 0;
    const probe = async (): Promise<boolean> => {
      calls += 1;
      return calls >= 3;
    };
    await expect(
      waitForKernelHealth(probe, { intervalMs: 5, timeoutMs: 2000 }),
    ).resolves.toBe(true);
    expect(calls).toBeGreaterThanOrEqual(3);
  });

  it("超时未就绪返回 false（不抛出、不无限等待，耗时不少于 timeoutMs）", async () => {
    const probe = async (): Promise<boolean> => false;
    const start = Date.now();
    await expect(
      waitForKernelHealth(probe, { intervalMs: 10, timeoutMs: 80 }),
    ).resolves.toBe(false);
    expect(Date.now() - start).toBeGreaterThanOrEqual(80);
  });
});

describe("KernelMissingError", () => {
  it("是 Error，消息带缺失路径与重装指引（fail-closed 语义可读）", () => {
    const err = new KernelMissingError("C:\\r\\kernel\\agentos-kernel.exe");
    expect(err).toBeInstanceOf(Error);
    expect(err.message).toContain("agentos-kernel.exe");
    expect(err.message).toContain("重新安装");
    expect(err.name).toBe("KernelMissingError");
  });
});

describe("ensurePackagedKernelRunning", () => {
  /** 造一个「内核 exe 存在」的临时 resources 布局 */
  const makeFakeResources = (): string => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "km-test-"));
    const exeName = process.platform === "win32" ? "agentos-kernel.exe" : "agentos-kernel";
    const exe = path.join(dir, "kernel", exeName);
    fs.mkdirSync(path.dirname(exe), { recursive: true });
    fs.writeFileSync(exe, "");
    return dir;
  };

  it("解析端口被外部内核占用 → KernelPortBusyError（fail-closed，绝不连接外部内核）", async () => {
    const dir = makeFakeResources();
    try {
      await expect(
        ensurePackagedKernelRunning({ resourcesPath: dir, probe: async () => true }),
      ).rejects.toMatchObject({ name: "KernelPortBusyError" });
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("占用报错消息含端口号与「先关闭占用者」指引（可读 fail-closed）", async () => {
    const dir = makeFakeResources();
    try {
      await expect(
        ensurePackagedKernelRunning({ resourcesPath: dir, probe: async () => true }),
      ).rejects.toThrow(new RegExp(`${KERNEL_PORT}.*关闭.*占用`, "s"));
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("KernelPortBusyError 是独立错误类型（main 侧可与其他失败区分呈现）", () => {
    const err = new KernelPortBusyError(9101);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe("KernelPortBusyError");
    expect(err.message).toContain("9101");
  });
});

describe("shutdownManagedKernel", () => {
  it("无受管内核时幂等空操作（reuse/dev 态重复调用不外抛）", () => {
    expect(() => shutdownManagedKernel()).not.toThrow();
    expect(() => shutdownManagedKernel()).not.toThrow();
  });
});

describe("planKernelRestart（内核意外退出重启决策，纯函数）", () => {
  const crash = {
    intentionalShutdown: false,
    runDurationMs: 10_000,
    consecutiveRestarts: 0,
  };

  it("主动关闭 → 不重启（退出清理不触发守护）", () => {
    expect(planKernelRestart({ ...crash, intentionalShutdown: true })).toEqual({
      action: "none",
    });
  });

  it("首次意外退出 → 第 1 档退避", () => {
    expect(planKernelRestart(crash)).toEqual({
      action: "restart",
      delayMs: KERNEL_RESTART_DELAYS_MS[0],
      attempt: 1,
    });
  });

  it("连续退出逐档退避（前 3 档 2s/5s/15s 且严格递增）", () => {
    const delays = [0, 1, 2].map((n) => {
      const d = planKernelRestart({ ...crash, consecutiveRestarts: n });
      if (d.action !== "restart") {
        throw new Error(`连续第 ${n + 1} 次应给 restart，得到 ${d.action}`);
      }
      return d.delayMs;
    });
    expect(delays).toEqual(KERNEL_RESTART_DELAYS_MS.slice(0, 3));
    for (let i = 1; i < delays.length; i++) {
      expect(delays[i]).toBeGreaterThan(delays[i - 1]);
    }
  });

  it("稳定运行满窗口 → 计数清零回第 1 档（偶发崩溃不积累退避档位）", () => {
    const d = planKernelRestart({
      intentionalShutdown: false,
      runDurationMs: KERNEL_RESTART_STABLE_MS + 1,
      consecutiveRestarts: 4,
    });
    expect(d).toEqual({ action: "restart", delayMs: KERNEL_RESTART_DELAYS_MS[0], attempt: 1 });
  });

  it("边界：恰好稳定窗口时长即视为稳定（>= 语义）", () => {
    const d = planKernelRestart({
      intentionalShutdown: false,
      runDurationMs: KERNEL_RESTART_STABLE_MS,
      consecutiveRestarts: 3,
    });
    expect(d.action === "restart" && d.attempt).toBe(1);
  });

  it("退避表用尽 → 放弃（连续次数超过表长，attempt = 表长 + 1）", () => {
    expect(planKernelRestart({ ...crash, consecutiveRestarts: KERNEL_RESTART_DELAYS_MS.length })).toEqual({
      action: "give-up",
      attempt: KERNEL_RESTART_DELAYS_MS.length + 1,
    });
  });

  it("边界：最后一次退避（第 5 次连续）仍给最后一档 60s", () => {
    const d = planKernelRestart({
      ...crash,
      consecutiveRestarts: KERNEL_RESTART_DELAYS_MS.length - 1,
    });
    expect(d.action === "restart" && d.delayMs).toBe(KERNEL_RESTART_DELAYS_MS[KERNEL_RESTART_DELAYS_MS.length - 1]);
  });

  it("性质：退避表严格递增且非空（重启语义的前提）", () => {
    expect(KERNEL_RESTART_DELAYS_MS.length).toBeGreaterThan(0);
    for (let i = 1; i < KERNEL_RESTART_DELAYS_MS.length; i++) {
      expect(KERNEL_RESTART_DELAYS_MS[i]).toBeGreaterThan(KERNEL_RESTART_DELAYS_MS[i - 1]);
    }
  });
});

describe("resolveTokenSecret（token 签名密钥持久化，自动登录跨重启）", () => {
  it("首次调用生成 ≥32 字符密钥并落盘；再次调用读回同一份（跨重启稳定）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "km-secret-"));
    try {
      const s1 = resolveTokenSecret(dir);
      expect(s1).not.toBeNull();
      expect(s1!.length).toBeGreaterThanOrEqual(32);
      expect(resolveTokenSecret(dir)).toBe(s1);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("过弱/空存量文件轮换为新强密钥（旧 token 本已全失效，无额外损失）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "km-secret-"));
    try {
      const file = path.join(dir, "kernel-token-secret");
      fs.writeFileSync(file, "short");
      const s = resolveTokenSecret(dir);
      expect(s!.length).toBeGreaterThanOrEqual(32);
      expect(s).not.toBe("short");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("性质：不同安装身份生成不同密钥（互不伪造对方 token）", () => {
    const d1 = fs.mkdtempSync(path.join(os.tmpdir(), "km-s1-"));
    const d2 = fs.mkdtempSync(path.join(os.tmpdir(), "km-s2-"));
    try {
      expect(resolveTokenSecret(d1)).not.toBe(resolveTokenSecret(d2));
    } finally {
      fs.rmSync(d1, { recursive: true, force: true });
      fs.rmSync(d2, { recursive: true, force: true });
    }
  });
});

describe("buildKernelEnv token 密钥注入", () => {
  const paths = kernelResourcePaths("C:\\app\\resources", "win32");
  it("提供密钥时注入 AGENTOS_TOKEN_SECRET（跨重启自动登录的前提）", () => {
    const env = buildKernelEnv({}, paths, "x".repeat(48));
    expect(env.AGENTOS_TOKEN_SECRET).toBe("x".repeat(48));
  });
  it("不提供时不注入该键（dev 形态零变化）", () => {
    const env = buildKernelEnv({}, paths);
    expect(env.AGENTOS_TOKEN_SECRET).toBeUndefined();
  });
});

describe("resolveKernelPort（AGENTOS_KERNEL_PORT 错峰解析）", () => {
  it("缺省/非法值回落默认端口", () => {
    expect(resolveKernelPort({})).toBe(KERNEL_DEFAULT_PORT);
    for (const raw of ["", "abc", "0", "-1", "65536"]) {
      expect(resolveKernelPort({ AGENTOS_KERNEL_PORT: raw })).toBe(KERNEL_DEFAULT_PORT);
    }
  });

  it("默认端口 9101：与 dev 栈默认 9100 错峰，两套环境可并存（ADR 2026-09-20 用户裁决）", () => {
    expect(KERNEL_DEFAULT_PORT).toBe(9101);
    expect(KERNEL_DEFAULT_PORT).not.toBe(9100);
  });

  it("合法端口透传（边界 1/65535 与常规错峰值，两档以上区分输入）", () => {
    expect(resolveKernelPort({ AGENTOS_KERNEL_PORT: "1" })).toBe(1);
    expect(resolveKernelPort({ AGENTOS_KERNEL_PORT: "65535" })).toBe(65535);
    expect(resolveKernelPort({ AGENTOS_KERNEL_PORT: "19200" })).toBe(19200);
  });

  it("模块加载期解析一次：常量 = 同环境解析值；stub 错峰后重载模块整体迁移", async () => {
    expect(KERNEL_PORT).toBe(resolveKernelPort());
    expect(KERNEL_HEALTH_URL).toBe(`http://127.0.0.1:${KERNEL_PORT}/health`);
    vi.stubEnv("AGENTOS_KERNEL_PORT", "19200");
    try {
      vi.resetModules();
      const shifted = await import("../kernel-manager");
      expect(shifted.KERNEL_PORT).toBe(19200);
      expect(shifted.KERNEL_HEALTH_URL).toBe("http://127.0.0.1:19200/health");
      const paths = shifted.kernelResourcePaths("C:\\app\\resources", "win32");
      expect(shifted.buildKernelEnv({}, paths).AGENTOS_KERNEL_PORT).toBe("19200");
    } finally {
      vi.unstubAllEnvs();
      vi.resetModules();
    }
  });
});
