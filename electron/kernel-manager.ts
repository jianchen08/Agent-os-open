/**
 * 打包件内核生命周期管理（electron main 侧）。
 *
 * 语义（本刀边界，ADR 2026-09-16-electron-kernel-spawn-wiring；
 * 端口默认分离 ADR 2026-09-20-packaged-kernel-port-default-9101）：
 *  - 生产模式（app.isPackaged）启动时：**只 spawn 包内内核**（cwd=resources/kernel，
 *    env 指向包内插件/配置）。默认端口 9101 与开发栈默认 9100 天然错峰，两套
 *    环境可并存；解析端口仍被占用 → KernelPortBusyError 显式报错——绝不
 *    复用/连接外部内核（连接外部内核 = 两套环境混用，开发版损坏会拖死装机版，
 *    用户裁定禁止）；
 *  - 运行中死亡自动重启：内核意外退出按退避表自动重启（连续 5 次封顶，稳定
 *    运行满 5 分钟计数清零）；主动退出（shutdownManagedKernel）不触发重启；
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
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";

/**
 * 打包件内核默认端口。
 *
 * 刻意与开发栈默认端口 9100 错峰（start_web_02.bat 起的 dev 内核占 9100）：
 * 装机版与开发版可同时运行，互不抢占（用户裁决 2026-09-20：同端口 = 无隔离）。
 * 前端打包件 WS 直连源（frontend/src/constants/websocket.ts
 * PACKAGED_KERNEL_WS_ORIGIN）与本值保持一致，改动须同刀同步。
 */
export const KERNEL_DEFAULT_PORT = 9101;

/**
 * 内核端口解析：AGENTOS_KERNEL_PORT 可整体错峰（端口冲突规避/隔离端口验证），
 * 非法或缺省回落默认端口。解析结果是全链路唯一事实源：内核 spawn 环境
 * （buildKernelEnv 钉值）、健康探测（KERNEL_HEALTH_URL）、app:// 代理回源与
 * CSP（app-protocol）——四处必须同端口，否则探测/代理/内核各指一处。
 */
export function resolveKernelPort(env: NodeJS.ProcessEnv = process.env): number {
  const parsed = Number.parseInt(env.AGENTOS_KERNEL_PORT ?? "", 10);
  return Number.isInteger(parsed) && parsed > 0 && parsed <= 65535
    ? parsed
    : KERNEL_DEFAULT_PORT;
}

/** 当前进程生效的内核端口（模块加载期解析一次，进程生命周期内不变） */
export const KERNEL_PORT = resolveKernelPort();
export const KERNEL_HEALTH_URL = `http://127.0.0.1:${KERNEL_PORT}/health`;

/**
 * 内核健康就绪等待上限。
 *
 * 首次启动含 venv 自愈（单插件 uv sync 上限即 300s，冷启动整体可达分钟级），
 * 上限须盖过它——绝不能在内核正常引导期间杀进程；进程意外退出会经 exit 事件
 * 提前失败，不靠本上限兜底真实故障。
 */
export const KERNEL_HEALTH_TIMEOUT_MS = 300_000;
/** 健康轮询间隔 */
export const KERNEL_HEALTH_INTERVAL_MS = 500;

/** 内核意外退出后的自动重启退避表（ms）：2s/5s/15s/30s/60s，用尽放弃 */
export const KERNEL_RESTART_DELAYS_MS = [2_000, 5_000, 15_000, 30_000, 60_000];
/** 内核稳定运行满该时长后重启计数清零（偶发一次崩溃不积累退避档位） */
export const KERNEL_RESTART_STABLE_MS = 5 * 60_000;

/** 重启决策（纯函数输出；watcher 副作用层据此执行） */
export type KernelRestartDecision =
  | { action: "none" }
  | { action: "restart"; delayMs: number; attempt: number }
  | { action: "give-up"; attempt: number };

/**
 * 内核退出后的重启决策（纯函数）。
 *
 * - 主动关闭 → 不重启；
 * - 距本次启动不足稳定窗口 → 连续失败计数 +1，按退避表取下一档；
 * - 稳定运行满窗口 → 视为新的一生，计数回到第 1 档；
 * - 退避表用尽 → 放弃（反复崩溃不是重启能治的，需看日志人工介入）。
 */
export function planKernelRestart(input: {
  intentionalShutdown: boolean;
  runDurationMs: number;
  consecutiveRestarts: number;
}): KernelRestartDecision {
  if (input.intentionalShutdown) {
    return { action: "none" };
  }
  const attempt =
    input.runDurationMs >= KERNEL_RESTART_STABLE_MS ? 1 : input.consecutiveRestarts + 1;
  const delayMs = KERNEL_RESTART_DELAYS_MS[attempt - 1];
  if (delayMs === undefined) {
    return { action: "give-up", attempt };
  }
  return { action: "restart", delayMs, attempt };
}

/** 打包件默认空闲回收阈值（对齐 start_web_02.bat：300s 回收真空闲 sidecar） */
const PLUGIN_IDLE_TIMEOUT_SECS = "300";

/** 内核 token 签名密钥落盘文件名（userData 下，每安装身份一份） */
const TOKEN_SECRET_FILE = "kernel-token-secret";

/** 密钥最小长度（与内核侧 AGENTOS_TOKEN_SECRET 的强度告警阈值一致） */
const TOKEN_SECRET_MIN_LEN = 32;

/**
 * 解析内核 token 签名密钥：userData 下持久化文件优先；缺失或过弱则生成
 * ≥32 字符随机密钥并落盘。
 *
 * 不注入该密钥时内核每进程随机签名（agentos_http::auth）——重启后全部
 * token 失效，用户每次打开应用都要重新登录。持久化一份=刷新令牌跨重启
 * 有效（自动登录）；删除该文件=全端登出（口令改密吊销面不受影响）。
 *
 * @returns 非空密钥；生成/读取失败返回 null（内核回落进程随机密钥，
 *          行为退化为现状的每次重登，不阻断启动）
 */
export function resolveTokenSecret(userDataDir: string): string | null {
  try {
    fs.mkdirSync(userDataDir, { recursive: true });
    const file = path.join(userDataDir, TOKEN_SECRET_FILE);
    try {
      const existing = fs.readFileSync(file, "utf-8").trim();
      if (existing.length >= TOKEN_SECRET_MIN_LEN) {
        return existing;
      }
      // 过弱/空文件：轮换为新密钥（旧 token 本就全部失效，无额外损失）
    } catch {
      // 文件不存在 = 首次生成
    }
    const secret = crypto.randomBytes(48).toString("base64");
    fs.writeFileSync(file, secret, { encoding: "utf-8", mode: 0o600 });
    return secret;
  } catch (err) {
    console.warn("[Kernel] token 签名密钥解析失败，回落进程随机密钥:", err);
    return null;
  }
}

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

/** 主账本库文件名（与内核 storage_factory::DB_FILENAME 同名，改动须同刀同步） */
const DB_FILENAME = "agentos_kernel.db";

/** 读环境变量，空白串视为未设（与内核 env_path 同语义） */
function envValue(env: NodeJS.ProcessEnv, key: string): string | undefined {
  const v = env[key]?.trim();
  return v ? v : undefined;
}

/**
 * 装机形态默认库位置（BUG-85）：`AGENTOS_USER_ROOT` 显式生效，否则回落
 * `<appData>/agentos`——与内核 user_root() 的 dirs 回落（%APPDATA%\agentos）
 * 同源同值；库文件名与内核 DB_FILENAME 同名。默认落在用户根而非安装目录
 * （resources\config 的 storage.yaml 相对路径锚定会把库拖进卸载抹除面）。
 */
export function resolvePackagedDbPath(
  env: NodeJS.ProcessEnv,
  appDataDir: string,
): string {
  const userRoot = envValue(env, "AGENTOS_USER_ROOT") ?? path.join(appDataDir, "agentos");
  return path.join(userRoot, DB_FILENAME);
}

/**
 * 装机形态授权名单真值目录（ADR 2026-09-24-read-deny-write-zones 决策5）：
 * `<userRoot>/config/users`——与插件侧 user_space.user_config_dir() 同源同值。
 * 真值必须在用户空间（可写、应用升级不丢），包内 `resources\config\users`
 * 降级为首次播种源。
 */
export function resolvePackagedConfigUsersDir(
  env: NodeJS.ProcessEnv,
  appDataDir: string,
): string {
  const userRoot = envValue(env, "AGENTOS_USER_ROOT") ?? path.join(appDataDir, "agentos");
  return path.join(userRoot, "config", "users");
}

/**
 * 播种授权名单（幂等，非破坏性）：把包内 `configRoot/users` 子树复制到用户
 * 空间真值目录——仅补缺失文件，绝不覆盖/删除既有内容（用户空间是活跃真值，
 * 用户批准的永久授权都在这里）。包内无 users 子树（异常包）时不动作。
 * fsMod 注入便于测试（与 probeKernelHealth 的 fetch 注入同款）。
 */
export function seedZonePolicyFiles(
  fsMod: Pick<
    typeof fs,
    "existsSync" | "mkdirSync" | "readdirSync" | "statSync" | "copyFileSync"
  >,
  configRoot: string,
  targetUsersDir: string,
): void {
  const sourceUsersDir = path.join(configRoot, "users");
  if (!fsMod.existsSync(sourceUsersDir)) {
    return;
  }
  const walk = (src: string, dst: string): void => {
    fsMod.mkdirSync(dst, { recursive: true });
    for (const entry of fsMod.readdirSync(src, { withFileTypes: true })) {
      const s = path.join(src, entry.name);
      const d = path.join(dst, entry.name);
      if (entry.isDirectory()) {
        walk(s, d);
      } else if (!fsMod.existsSync(d)) {
        fsMod.copyFileSync(s, d);
      }
    }
  };
  walk(sourceUsersDir, targetUsersDir);
}

/**
 * 构造内核子进程环境。
 *
 * - AGENTOS_BIND=127.0.0.1：显式钉回环（KERNEL_ORIGIN 硬编码 127.0.0.1 回环，
 *   回环足够且不暴露外网；内核以 AGENTOS_BIND 为主键，AGENTOS_KERNEL_HOST 为
 *   弃用别名，ambient 残留值因 AGENTOS_BIND 恒被设置而必然失效）；
 * - AGENTOS_KERNEL_PORT：钉为 resolveKernelPort 解析值——内核监听端口与健康
 *   探测（KERNEL_HEALTH_URL）恒一致，ambient 值不致造成两者分叉；
 * - AGENTOS_PLUGINS_DIR / AGENTOS_CONFIG_ROOT：指向包内资源；
 * - AGENTOS_DB_PATH：BUG-85 库位置改用户根——仅当 ambient 未设时注入
 *   resolvePackagedDbPath 解析值（显式覆盖能力归用户，空白串视为未设）；
 *   不注入时包内 storage.yaml 的相对路径会把库锚进安装目录，静默卸载即删库；
 * - AGENTOS_CONFIG_USERS_DIR：授权名单真值钉用户空间（ADR 2026-09-24 决策5，
 *   恒为 `<userRoot>/config/users`）——包内 resources 名单经 seedZonePolicyFiles
 *   首次播种，授权卡永久项写用户空间，升级不丢；ambient 显式设置优先；
 * - AGENTOS_PLUGIN_SOURCE_PRIORITY=builtin：双源同 id 裁决置内置优先——装机版
 *   用户空间的共享插件副本是旧安装/旧迁移遗留（陈旧/扁平布局/.venv 缺失），
 *   不得压过包内版本匹配的新版（BUG-55，ADR
 *   2026-09-20-packaged-dual-source-adjudication）；用户根仍承载内置没有的
 *   额外插件；
 * - AGENTOS_PLUGIN_VENV_AUTOPROVISION=1：boot 后台对缺 .venv 的 Python sidecar
 *   跑 `uv sync` 自愈（打包资源排除 .venv 且装机链没有 dev launcher；同 ADR
 *   决策②；uv 缺席/失败仅 warn 降级）；
 * - AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS=300：硬设（打包件行为固定）。
 *
 * 其余变量原样继承（Windows 子进程需要 SystemRoot 等系统环境）。
 */
export function buildKernelEnv(
  base: NodeJS.ProcessEnv,
  paths: KernelResourcePaths,
  tokenSecret?: string,
  dbPath?: string,
  configUsersDir?: string,
): NodeJS.ProcessEnv {
  return {
    ...base,
    AGENTOS_BIND: "127.0.0.1",
    AGENTOS_KERNEL_PORT: String(KERNEL_PORT),
    AGENTOS_PLUGINS_DIR: paths.pluginsDir,
    AGENTOS_CONFIG_ROOT: paths.configRoot,
    AGENTOS_PLUGIN_SOURCE_PRIORITY: "builtin",
    AGENTOS_PLUGIN_VENV_AUTOPROVISION: "1",
    AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS: PLUGIN_IDLE_TIMEOUT_SECS,
    // token 签名密钥持久化（每安装身份一份）：不注入时内核每进程随机签名，
    // 重启后全部 token 失效=每次打开应用都要重新登录
    ...(tokenSecret ? { AGENTOS_TOKEN_SECRET: tokenSecret } : {}),
    // 库位置钉用户根（BUG-85）：ambient 显式设置优先（覆盖能力保留）
    ...(dbPath && !envValue(base, "AGENTOS_DB_PATH") ? { AGENTOS_DB_PATH: dbPath } : {}),
    // 授权名单真值钉用户空间（ADR 2026-09-24 决策5）：ambient 显式设置优先
    ...(configUsersDir && !envValue(base, "AGENTOS_CONFIG_USERS_DIR")
      ? { AGENTOS_CONFIG_USERS_DIR: configUsersDir }
      : {}),
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

/** 解析端口被外部进程占用（fail-closed：绝不连接外部内核，两套环境禁止混用） */
export class KernelPortBusyError extends Error {
  constructor(port: number) {
    super(
      `端口 ${port} 已被其他内核占用（可能是开发环境的内核）。` +
      `为避免两套环境混用，本应用只运行自带的内核；` +
      `请先关闭占用该端口的内核后重试。`,
    );
    this.name = "KernelPortBusyError";
  }
}

/** 当前受管内核进程（spawn 模式才有；dev 恒为 null） */
let managed: ChildProcess | null = null;
/** 主动关闭标记：shutdownManagedKernel 置位，exit 监视据此不重启 */
let intentionalShutdown = false;
/** 连续重启计数（稳定运行满 KERNEL_RESTART_STABLE_MS 清零） */
let consecutiveRestarts = 0;
/** 当前受管内核的启动时刻（稳定窗口判定用） */
let currentRunStartedAt = 0;
let restartTimer: NodeJS.Timeout | null = null;
let restarting = false;
/** 当前安装身份的 token 签名密钥（ensure 时解析一次，重启复用同一份） */
let tokenSecret: string | null = null;
/** 当前安装身份的库位置（ensure 时解析一次，重启复用同一份；BUG-85） */
let packagedDbPath: string | null = null;
/** 当前安装身份的授权名单真值目录（ensure 时解析并播种一次；ADR 2026-09-24 决策5） */
let packagedConfigUsersDir: string | null = null;

/** spawn 包内内核（ensure 与自动重启共用的副作用层） */
function spawnKernelProcess(paths: KernelResourcePaths): ChildProcess {
  return spawn(paths.kernelExe, [], {
    cwd: paths.kernelDir,
    env: buildKernelEnv(
      process.env,
      paths,
      tokenSecret ?? undefined,
      packagedDbPath ?? undefined,
      packagedConfigUsersDir ?? undefined,
    ),
    stdio: "ignore",
    windowsHide: true,
  });
}

/** 等待内核健康就绪；进程退出或 spawn 错误提前失败（绝不在引导期错杀内核） */
async function awaitKernelReady(
  child: ChildProcess,
  probe: () => Promise<boolean>,
): Promise<{ ready: boolean; spawnError: Error | null; exited: boolean; exitCode: number | null }> {
  let spawnError: Error | null = null;
  const spawnFailed = new Promise<null>((resolve) => {
    child.once("error", (err) => {
      spawnError = new Error(`内核进程启动失败: ${err.message}`);
      resolve(null);
    });
  });
  let exited = false;
  let exitCode: number | null = null;
  const processExited = new Promise<null>((resolve) => {
    child.once("exit", (code) => {
      exited = true;
      exitCode = code;
      resolve(null);
    });
  });
  const ready = await Promise.race([
    waitForKernelHealth(probe, {
      intervalMs: KERNEL_HEALTH_INTERVAL_MS,
      timeoutMs: KERNEL_HEALTH_TIMEOUT_MS,
    }),
    spawnFailed,
    processExited,
  ]);
  return { ready: ready === true, spawnError, exited, exitCode };
}

/**
 * 挂内核退出监视：意外退出按 planKernelRestart 决策退避重启。
 * 新进程接管（managed 换人）后旧监视自然失效（managed !== child 守卫）。
 */
function armKernelExitWatcher(paths: KernelResourcePaths): void {
  const child = managed;
  if (!child) {
    return;
  }
  child.once("exit", (code, signal) => {
    if (managed !== child) {
      return;
    }
    managed = null;
    const decision = planKernelRestart({
      intentionalShutdown,
      runDurationMs: Date.now() - currentRunStartedAt,
      consecutiveRestarts,
    });
    if (decision.action === "none") {
      return;
    }
    if (decision.action === "give-up") {
      console.error(
        `[Kernel] 内核连续退出 ${decision.attempt} 次，放弃自动重启。` +
          `请重启应用；日志见安装目录 resources\\kernel\\logs\\`,
      );
      return;
    }
    consecutiveRestarts = decision.attempt;
    console.warn(
      `[Kernel] 内核意外退出（code=${code} signal=${signal}），` +
        `${decision.delayMs / 1000}s 后自动重启（第 ${decision.attempt}/${KERNEL_RESTART_DELAYS_MS.length} 次）`,
    );
    restartTimer = setTimeout(() => {
      restartTimer = null;
      void restartManagedKernel(paths);
    }, decision.delayMs);
    restartTimer.unref();
  });
}

/** 自动重启一轮：端口已有内核应答则不叠加拉起（禁止混用），失败续退避 */
async function restartManagedKernel(paths: KernelResourcePaths): Promise<void> {
  if (intentionalShutdown || restarting || managed) {
    return;
  }
  if (!fs.existsSync(paths.kernelExe)) {
    console.error(`[Kernel] 自动重启中止：内核组件缺失（${paths.kernelExe}），请重新安装。`);
    return;
  }
  // 端口已有内核应答（本应用残留孤儿，或同机第二份实例）→ 不再 spawn：
  // 服务已在答，叠加拉起只会 KernelPortBusy；绝不复用/连接外部内核。
  if (await probeKernelHealth(fetch)) {
    console.warn(
      `[Kernel] 端口 ${KERNEL_PORT} 已有内核在应答（疑似残留进程），本轮不再拉起。`,
    );
    return;
  }
  restarting = true;
  try {
    const child = spawnKernelProcess(paths);
    managed = child;
    const { ready, spawnError } = await awaitKernelReady(child, () => probeKernelHealth(fetch));
    if (!ready) {
      killKernelTree(child.pid);
      managed = null;
      const decision = planKernelRestart({
        intentionalShutdown: false,
        runDurationMs: 0,
        consecutiveRestarts,
      });
      if (decision.action === "restart") {
        consecutiveRestarts = decision.attempt;
        console.error(
          `[Kernel] 重启后未就绪${spawnError ? `：${spawnError.message}` : ""}，` +
            `${decision.delayMs / 1000}s 后再试（第 ${decision.attempt}/${KERNEL_RESTART_DELAYS_MS.length} 次）`,
        );
        restartTimer = setTimeout(() => {
          restartTimer = null;
          void restartManagedKernel(paths);
        }, decision.delayMs);
        restartTimer.unref();
      } else if (decision.action === "give-up") {
        console.error(
          `[Kernel] 内核反复无法就绪（连续 ${decision.attempt} 次），放弃自动重启。` +
            `请重启应用；日志见安装目录 resources\\kernel\\logs\\`,
        );
      }
      return;
    }
    consecutiveRestarts = 0;
    currentRunStartedAt = Date.now();
    console.info(`[Kernel] 内核已自动重启 pid=${child.pid}`);
    armKernelExitWatcher(paths);
  } finally {
    restarting = false;
  }
}

/**
 * 确保打包件内核运行并等待健康就绪（只 spawn 包内内核，不复用外部内核）。
 *
 * 仅生产模式调用（dev 由 main.ts 分流，不进入本函数）。
 *
 * @throws KernelMissingError 内核 exe 缺失（安装损坏）
 * @throws KernelPortBusyError 解析端口已被外部内核占用（禁止混用）
 * @throws Error 内核进程启动失败/健康就绪超时（失败路径会先收掉已拉起进程再抛）
 */
export async function ensurePackagedKernelRunning(opts: {
  resourcesPath: string;
  /** 健康探测注入（测试用）；缺省探测解析端口 /health */
  probe?: () => Promise<boolean>;
  /** userData 目录：解析持久化 token 签名密钥（自动登录跨重启） */
  userDataDir?: string;
  /** OS 应用数据目录（app.getPath("appData")）：解析装机默认库位置（BUG-85 用户根） */
  appDataDir?: string;
}): Promise<{ mode: "spawned"; pid?: number }> {
  const paths = kernelResourcePaths(opts.resourcesPath);
  if (!fs.existsSync(paths.kernelExe)) {
    throw new KernelMissingError(paths.kernelExe);
  }
  tokenSecret = opts.userDataDir ? resolveTokenSecret(opts.userDataDir) : null;
  packagedDbPath = opts.appDataDir
    ? resolvePackagedDbPath(process.env, opts.appDataDir)
    : null;
  packagedConfigUsersDir = opts.appDataDir
    ? resolvePackagedConfigUsersDir(process.env, opts.appDataDir)
    : null;
  if (packagedConfigUsersDir) {
    try {
      seedZonePolicyFiles(fs, paths.configRoot, packagedConfigUsersDir);
    } catch (err) {
      // 播种失败不阻断启动：名单读取侧对缺失文件按空名单处理（安全侧）
      console.warn("[kernel-manager] 授权名单播种失败（按空白名单降级）:", err);
    }
  }

  const probe = opts.probe ?? (() => probeKernelHealth(fetch));
  if (await probe()) {
    throw new KernelPortBusyError(KERNEL_PORT);
  }

  const child = spawnKernelProcess(paths);
  managed = child;

  const { ready, spawnError, exited, exitCode } = await awaitKernelReady(child, probe);
  if (!ready) {
    shutdownManagedKernel();
    throw spawnError ?? (exited
      ? new Error(
          `内核进程在启动期间退出（code=${exitCode}）。` +
          `请重试；若反复出现，请反馈内核日志（安装目录 resources\\kernel\\logs\\）。`,
        )
      : new Error(
          `内核在 ${KERNEL_HEALTH_TIMEOUT_MS / 1000} 秒内未就绪，应用将退出。` +
          `请重试；若反复出现，请反馈内核日志（安装目录 resources\\kernel\\logs\\）。`,
        ));
  }
  intentionalShutdown = false;
  consecutiveRestarts = 0;
  currentRunStartedAt = Date.now();
  armKernelExitWatcher(paths);
  console.info(`[Kernel] 打包内核已就绪 pid=${child.pid}`);
  return { mode: "spawned", pid: child.pid };
}

/** 结束内核进程树（win：taskkill /F /T 连 sidecar；其余平台 SIGTERM） */
function killKernelTree(pid: number | undefined): void {
  if (pid === undefined) {
    return;
  }
  if (process.platform === "win32") {
    try {
      spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      }).unref();
      console.info(`[Kernel] 已请求结束内核进程树 pid=${pid}`);
    } catch (err) {
      console.warn(`[Kernel] taskkill 请求失败 pid=${pid}:`, err);
    }
  } else {
    try {
      process.kill(pid, "SIGTERM");
      console.info(`[Kernel] 已发送 SIGTERM pid=${pid}`);
    } catch (err) {
      console.warn(`[Kernel] 结束内核失败 pid=${pid}:`, err);
    }
  }
}

/**
 * 结束受管内核进程树并置主动关闭标记（挂起的自动重启一并取消；幂等）。
 *
 * electron 崩溃路径的孤儿兜底属遗留项 Job Object，本刀不做。
 */
export function shutdownManagedKernel(): void {
  if (!managed) {
    return;
  }
  const pid = managed.pid;
  managed = null;
  intentionalShutdown = true;
  if (restartTimer) {
    clearTimeout(restartTimer);
    restartTimer = null;
  }
  killKernelTree(pid);
}
