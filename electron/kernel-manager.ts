/**
 * 打包件内核生命周期管理（electron main 侧）。
 *
 * 语义（本刀边界，ADR 2026-09-16-electron-kernel-spawn-wiring）：
 *  - 生产模式（app.isPackaged）启动时：9100 已有健康内核 → 复用模式（不 spawn，
 *    该内核属外部实例，不受退出管理）；无监听且 resources/kernel/agentos-kernel.exe
 *    存在 → 直接 spawn（cwd=resources/kernel，env 指向包内插件/配置）；
 *  - 退出清理：对自己 spawn 的内核 taskkill /F /T 树杀（连带 python sidecar）；
 *    electron 崩溃路径的孤儿内核兜底（Job Object）属遗留项，本刀不做；
 *  - 内核 exe 缺失 → KernelMissingError（fail-closed，兜 electron-builder 对
 *    缺失 extraResources 源仅 warn 的静默缺口）；
 *  - dev 模式不进入本模块（main.ts 按 isDevelopment 分流，外部内核不属于应用管理）。
 *
 * 本文件不 import electron：纯函数层（env 构造/健康探测）可在 node 环境直接
 * 单测；副作用层只有 spawn/fs。
 */

import { spawn, type ChildProcess } from "child_process";
import * as fs from "fs";
import * as path from "path";

/** 内核默认端口（与 app-protocol.ts KERNEL_ORIGIN、内核 AGENTOS_KERNEL_PORT 默认值一致） */
export const KERNEL_PORT = 9100;
export const KERNEL_HEALTH_URL = `http://127.0.0.1:${KERNEL_PORT}/health`;

/** 内核健康就绪等待上限（就绪后 health 轮询立即返回，不拖慢热启动） */
export const KERNEL_HEALTH_TIMEOUT_MS = 60_000;
/** 健康轮询间隔 */
export const KERNEL_HEALTH_INTERVAL_MS = 500;

/** 打包件默认空闲回收阈值（对齐 start_web_02.bat：300s 回收真空闲 sidecar） */
const PLUGIN_IDLE_TIMEOUT_SECS = "300";

export interface KernelResourcePaths {
  /** 内核可执行文件（resources/kernel/ 下，win 带 .exe，unix 占位名） */
  kernelExe: string;
  /** 内核工作目录（resources/kernel，内核日志 logs/kernel.log.* 相对此落盘） */
  kernelDir: string;
  /** 内置插件根（resources/plugins/shared） */
  pluginsDir: string;
  /** 出厂配置种子根（resources/config，config ownership ADR 语义） */
  configRoot: string;
}

/**
 * 解析打包件内核资源布局（extraResources 供应面，ADR 2026-09-16 §2.1）。
 *
 * @param resourcesPath - process.resourcesPath（打包件）或任选根（测试）
 * @param platform - 目标平台（默认当前进程；unix 产物名无扩展名）
 */
export function kernelResourcePaths(
  resourcesPath: string,
  platform: NodeJS.Platform = process.platform,
): KernelResourcePaths {
  const exeName = platform === "win32" ? "agentos-kernel.exe" : "agentos-kernel";
  const kernelDir = path.join(resourcesPath, "kernel");
  return {
    kernelExe: path.join(kernelDir, exeName),
    kernelDir,
    pluginsDir: path.join(resourcesPath, "plugins", "shared"),
    configRoot: path.join(resourcesPath, "config"),
  };
}

/**
 * 构造内核子进程环境。
 *
 * - AGENTOS_BIND=127.0.0.1：显式钉回环（KERNEL_ORIGIN 硬编码 127.0.0.1:9100，
 *   回环足够且不暴露外网；内核以 AGENTOS_BIND 为主键，AGENTOS_KERNEL_HOST 为
 *   弃用别名，ambient 残留值因 AGENTOS_BIND 恒被设置而必然失效）；
 * - AGENTOS_PLUGINS_DIR / AGENTOS_CONFIG_ROOT：指向包内资源；
 * - AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS=300：硬设（打包件行为固定）。
 *
 * 其余变量原样继承（Windows 子进程需要 SystemRoot 等系统环境）。
 */
export function buildKernelEnv(
  base: NodeJS.ProcessEnv,
  paths: KernelResourcePaths,
): NodeJS.ProcessEnv {
  return {
    ...base,
    AGENTOS_BIND: "127.0.0.1",
    AGENTOS_PLUGINS_DIR: paths.pluginsDir,
    AGENTOS_CONFIG_ROOT: paths.configRoot,
    AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS: PLUGIN_IDLE_TIMEOUT_SECS,
  };
}

/**
 * 探测内核 /health（一次）。
 * fetch 仅注入便于测试；任何异常（连接拒绝/超时）一律 false。
 */
export async function probeKernelHealth(
  fetchImpl: typeof fetch,
  url: string = KERNEL_HEALTH_URL,
  timeoutMs = 2000,
): Promise<boolean> {
  try {
    const res = await fetchImpl(url, { signal: AbortSignal.timeout(timeoutMs) });
    return res.status === 200;
  } catch {
    return false;
  }
}

/**
 * 轮询健康直至就绪或超时（真实定时器，延迟由 opts 注入）。
 * @returns 是否就绪
 */
export async function waitForKernelHealth(
  probe: () => Promise<boolean>,
  opts: { intervalMs: number; timeoutMs: number },
): Promise<boolean> {
  const deadline = Date.now() + opts.timeoutMs;
  for (;;) {
    if (await probe()) {
      return true;
    }
    if (Date.now() >= deadline) {
      return false;
    }
    await new Promise((r) => setTimeout(r, opts.intervalMs));
  }
}

/** 打包件内核组件缺失（fail-closed） */
export class KernelMissingError extends Error {
  constructor(kernelExe: string) {
    super(`应用安装损坏：缺少内核组件（${kernelExe}）。请重新安装本应用。`);
    this.name = "KernelMissingError";
  }
}

/** 当前受管内核进程（spawn 模式才有；reuse/dev 恒为 null） */
let managed: ChildProcess | null = null;

/**
 * 确保打包件内核运行（spawn 或复用），并等待健康就绪。
 *
 * 仅生产模式调用（dev 由 main.ts 分流，不进入本函数）。
 *
 * @throws KernelMissingError 内核 exe 缺失（安装损坏）
 * @throws Error 内核进程启动失败/健康就绪超时（失败路径会先收掉已拉起进程再抛）
 */
export async function ensurePackagedKernelRunning(opts: {
  resourcesPath: string;
}): Promise<{ mode: "reuse" | "spawned"; pid?: number }> {
  const paths = kernelResourcePaths(opts.resourcesPath);
  if (!fs.existsSync(paths.kernelExe)) {
    throw new KernelMissingError(paths.kernelExe);
  }

  if (await probeKernelHealth(fetch)) {
    console.info("[Kernel] 检测到 9100 已有健康内核，复用模式（不 spawn，不受退出管理）");
    return { mode: "reuse" };
  }

  const child = spawn(paths.kernelExe, [], {
    cwd: paths.kernelDir,
    env: buildKernelEnv(process.env, paths),
    stdio: "ignore",
    windowsHide: true,
  });
  managed = child;

  // spawn 层错误（exe 损坏/被杀软拦截等）异步落在 error 事件，转成可诊断失败
  let spawnError: Error | null = null;
  const spawnFailed = new Promise<null>((resolve) => {
    child.once("error", (err) => {
      spawnError = new Error(`内核进程启动失败: ${err.message}`);
      resolve(null);
    });
  });

  const ready = await Promise.race([
    waitForKernelHealth(() => probeKernelHealth(fetch), {
      intervalMs: KERNEL_HEALTH_INTERVAL_MS,
      timeoutMs: KERNEL_HEALTH_TIMEOUT_MS,
    }),
    spawnFailed,
  ]);

  if (!ready) {
    shutdownManagedKernel();
    throw spawnError ?? new Error(
      `内核在 ${KERNEL_HEALTH_TIMEOUT_MS / 1000} 秒内未就绪，应用将退出。` +
      `请重试；若反复出现，请反馈内核日志（安装目录 resources\\kernel\\logs\\）。`,
    );
  }
  console.info(`[Kernel] 打包内核已就绪 pid=${child.pid}`);
  return { mode: "spawned", pid: child.pid };
}

/**
 * 结束受管内核进程树（正常退出路径的清理；electron 崩溃路径的孤儿兜底属
 * 遗留项 Job Object，本刀不做）。
 *
 * win：taskkill /F /T 连 sidecar 树一并强收；其余平台：SIGTERM（未验证，
 * mac/linux 打包侧登记不做）。幂等，可重复调用。
 */
export function shutdownManagedKernel(): void {
  if (!managed) {
    return;
  }
  const pid = managed.pid;
  managed = null;
  if (process.platform === "win32" && pid !== undefined) {
    try {
      spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      }).unref();
      console.info(`[Kernel] 已请求结束内核进程树 pid=${pid}`);
    } catch (err) {
      console.warn(`[Kernel] taskkill 请求失败 pid=${pid}:`, err);
    }
  } else if (pid !== undefined) {
    try {
      process.kill(pid, "SIGTERM");
      console.info(`[Kernel] 已发送 SIGTERM pid=${pid}`);
    } catch (err) {
      console.warn(`[Kernel] 结束内核失败 pid=${pid}:`, err);
    }
  }
}
