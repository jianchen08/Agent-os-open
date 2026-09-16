/**
 * kernel-manager 纯函数层单测（node 环境，不触碰 Electron 运行时）。
 *
 * 覆盖口径：断行为（输入→输出），fetch 仅 mock 外部边界（内核 HTTP 端点）；
 * 时序用真实定时器 + 注入的可调小延迟，不用零延迟 mock。
 * spawn/taskkill 副作用层由打包件手工验收清单覆盖（见 ADR 验收节）。
 */

import * as path from "path";

import { describe, expect, it } from "vitest";

import {
  KERNEL_HEALTH_URL,
  KernelMissingError,
  buildKernelEnv,
  kernelResourcePaths,
  probeKernelHealth,
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

  it("空白基座：写入绑定面/插件根/配置根/空闲回收四个变量", () => {
    const env = buildKernelEnv({}, paths);
    expect(env.AGENTOS_BIND).toBe("127.0.0.1");
    expect(env.AGENTOS_PLUGINS_DIR).toBe(paths.pluginsDir);
    expect(env.AGENTOS_CONFIG_ROOT).toBe(paths.configRoot);
    expect(env.AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS).toBe("300");
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

  it("默认探测 URL 钉在 127.0.0.1:9100/health（与 KERNEL_ORIGIN 同源）", () => {
    expect(KERNEL_HEALTH_URL).toBe("http://127.0.0.1:9100/health");
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

describe("shutdownManagedKernel", () => {
  it("无受管内核时幂等空操作（reuse/dev 态重复调用不外抛）", () => {
    expect(() => shutdownManagedKernel()).not.toThrow();
    expect(() => shutdownManagedKernel()).not.toThrow();
  });
});
