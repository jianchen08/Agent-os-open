/**
 * 打包件内核生命周期管理（electron main 侧）。
 *
 * 语义（本刀边界，ADR 2026-09-16-electron-kernel-spawn-wiring；
 * 端口默认分离 ADR 2026-09-20-packaged-kernel-port-default-9101）：
 *  - 生产模式（app.isPackaged）启动时：**只 spawn 包内内核**（cwd=resources/kernel，
 *    env 指向包内插件/配置）。默认端口（KERNEL_DEFAULT_PORT，生成物单源）与
 *    开发栈默认 9100 天然错峰，两套
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

import {
  diagLogPaths,
  logDiag,
  pruneExpiredKernelLogs,
  shouldRotateDiagLog,
} from "./diag-log";
import { PACKAGED_KERNEL_PORT } from "./packaged-kernel-port.generated";

/**
 * 打包件内核默认端口（生成物单源 re-export）。
 *
 * 真值单源在 scripts/generate_port_constants.py 真值区 → 本文件旁的
 * packaged-kernel-port.generated.ts（DO-NOT-EDIT）；与开发栈默认端口 9100
 * 错峰，装机版与开发版可并存（用户裁决 2026-09-20，ADR 同名）。改端口 =
 * 改生成器真值 → 跑生成脚本两侧随动；一致性由
 * scripts/check_kernel_port_sync.py 执法（生成幂等 = 绿）。
 */
export const KERNEL_DEFAULT_PORT = PACKAGED_KERNEL_PORT;

/**
 * 内核端口解析：AGENTOS_KERNEL_PORT 可整体错峰（端口冲突规避/隔离端口验证），
 * 非法或缺省回落默认端口。解析结果是全链路唯一事实源：内核 spawn 环境
 * （buildKernelEnv 钉值）、健康探测（KERNEL_HEALTH_URL）、app:// 代理回源与
 * CSP（app-protocol）——四处必须同端口，否则探测/代理/内核各指一处。
 */
export function resolveKernelPort(
  env: NodeJS.ProcessEnv = process.env,
): number {
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
    input.runDurationMs >= KERNEL_RESTART_STABLE_MS
      ? 1
      : input.consecutiveRestarts + 1;
  const delayMs = KERNEL_RESTART_DELAYS_MS[attempt - 1];
  if (delayMs === undefined) {
    return { action: "give-up", attempt };
  }
  return { action: "restart", delayMs, attempt };
}

/** 打包件默认空闲回收阈值（对齐 start_web_02.bat：300s 回收真空闲 sidecar） */
const PLUGIN_IDLE_TIMEOUT_SECS = "300";

/** 内核 token 签名密钥落盘文件名（认证身份目录下，机器级一份跨安装稳定） */
export const TOKEN_SECRET_FILE = "kernel-token-secret";

/** 密钥最小长度（与内核侧 AGENTOS_TOKEN_SECRET 的强度告警阈值一致） */
const TOKEN_SECRET_MIN_LEN = 32;

/**
 * 解析内核 token 签名密钥：认证身份目录下持久化文件优先；缺失或过弱则
 * 生成 ≥32 字符随机密钥并落盘。
 *
 * 不注入该密钥时内核每进程随机签名（agentos_http::auth）——重启后全部
 * token 失效，用户每次打开应用都要重新登录。持久化一份=刷新令牌跨重启
 * 有效（自动登录）；删除该文件=全端登出（口令改密吊销面不受影响）。
 * 目录取认证身份目录（main.resolveAuthIdentityDir，机器级跨安装稳定），
 * 不随安装目录哈希走——内核用户库机器级共享（BUG-85），同一账号体系
 * 每安装一份密钥=换目录重装即全端令牌作废（2026-10-22 用户实测）。
 *
 * @returns 非空密钥；生成/读取失败返回 null（内核回落进程随机密钥，
 *          行为退化为现状的每次重登，不阻断启动）
 */
export function resolveTokenSecret(identityDir: string): string | null {
  try {
    fs.mkdirSync(identityDir, { recursive: true });
    const file = path.join(identityDir, TOKEN_SECRET_FILE);
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
  const exeName =
    platform === "win32" ? "agentos-kernel.exe" : "agentos-kernel";
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
  const userRoot =
    envValue(env, "AGENTOS_USER_ROOT") ?? path.join(appDataDir, "agentos");
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
  const userRoot =
    envValue(env, "AGENTOS_USER_ROOT") ?? path.join(appDataDir, "agentos");
  return path.join(userRoot, "config", "users");
}

/**
 * 装机形态配置根真值（ADR 2026-10-17-installed-config-root-user-space）：
 * `<userRoot>/config`——与内核 `user_space::user_config_dir()`、
 * `scripts/sync_installed_user_space.py --config` 的落点同源同值。
 *
 * 为什么必须是用户空间而非包内 `resources/config`：
 * - **安装目录是升级/卸载抹除面**：写进去的配置每次升级回退、卸载即删；
 * - **读侧按路径直读的插件看不见用户空间**：插件（如 isolation 的
 *   `_isolation_config_path()`）先查 `AGENTOS_CONFIG_ROOT`、未命中才从插件
 *   文件向上找祖先目录那份——该变量指包内时，用户空间配置永不被读，
 *   用户写的 `workspace.root` 静默失效（2026-10-17 事故根因）。
 *
 * 优先级：ambient `AGENTOS_CONFIG_ROOT`（显式钉配置根的能力归用户）→
 * `AGENTOS_USER_CONFIG_DIR`（用户层根覆盖）→ `<userRoot>/config`。前两者
 * 与内核 `user_space::user_config_dir()` 的解析链逐项对齐——**注入值恒等于
 * 内核自己解析出的用户配置层**，否则会出现"内核写 A 层、插件读 B 层"的
 * 分叉（单真值模型的实证根因）。**显式设置时解析值即该值**，播种与注入
 * 共用同一解析结果，二者不得分叉。
 */
export function resolvePackagedConfigRootDir(
  env: NodeJS.ProcessEnv,
  appDataDir: string,
): string {
  const pinned = envValue(env, "AGENTOS_CONFIG_ROOT");
  if (pinned) {
    return pinned;
  }
  const pinnedUserConfig = envValue(env, "AGENTOS_USER_CONFIG_DIR");
  if (pinnedUserConfig) {
    return pinnedUserConfig;
  }
  const userRoot =
    envValue(env, "AGENTOS_USER_ROOT") ?? path.join(appDataDir, "agentos");
  return path.join(userRoot, "config");
}

/**
 * 出厂配置树播种（幂等）：把包内 `resources/config` 整树播种到用户空间配置根。
 *
 * 两段裁决（ADR 2026-10-24-config-seed-version-arbitration）：
 * - **非受管文件**（无有效顶层 `version:` 声明）：copy-if-missing——仅补缺失，
 *   绝不覆盖既有内容（用户空间是活跃真值，用户在 storage/授权名单/插件配置
 *   上的改动都在这里，出厂树只补洞）；
 * - **受管出厂默认**（yaml 顶层 `version:` 为点分整数）：版本裁决——用户副本
 *   版本**严格大于**包内版本才保留，否则（持平/更低/无版本/不可解析）包内
 *   覆盖。受管是包侧逐文件自声明的：包内文件不带有效 version 即不入受管面，
 *   语义与旧版逐字节一致。
 *
 * 为什么覆盖裁决放播种步而非内核读侧：读侧只认一个根（ADR
 * 2026-10-17-installed-config-root-user-space，半配置不失效）保持不变；
 * 「用户空间陈旧副本遮蔽包内修复」（2026-10-24 评估通过空转事故根因，
 * 10-21 旧 autonomous.yaml 遮蔽 10-23 包内路由修复）在播种步一次治好，
 * 内核零改动。
 *
 * 版本格式（点分整数，如 1 / 1.2）由保存面（PUT 校验）与 CI 门禁
 * `scripts/check_config_seed_versions.py`（内容变更必须抬版本）把守；
 * 播种侧对不可解析版本 fail 向包内（用户副本视为 0，被覆盖自愈）。
 *
 * 返回计数（copied=补缺，overwritten=版本覆盖）——overwritten>0 由调用方
 * 留痕（覆盖不可见=本次事故复盘盲区）。
 *
 * fsMod 注入便于测试（与 seedZonePolicyFiles 同款）。
 */
export interface SeedConfigTreeResult {
  copied: number;
  overwritten: number;
}

export function seedConfigTree(
  fsMod: Pick<
    typeof fs,
    | "existsSync"
    | "mkdirSync"
    | "readdirSync"
    | "readFileSync"
    | "copyFileSync"
  >,
  packageConfigRoot: string,
  userConfigDir: string,
): SeedConfigTreeResult {
  const result: SeedConfigTreeResult = { copied: 0, overwritten: 0 };
  if (!fsMod.existsSync(packageConfigRoot)) {
    return result;
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
        result.copied += 1;
      } else if (shouldOverwriteManaged(fsMod, s, d)) {
        fsMod.copyFileSync(s, d);
        result.overwritten += 1;
      }
    }
  };
  walk(packageConfigRoot, userConfigDir);
  return result;
}

/** 受管出厂默认判定后是否覆盖用户副本（包内未声明有效 version = 永不覆盖）。 */
function shouldOverwriteManaged(
  fsMod: Pick<typeof fs, "existsSync" | "readFileSync">,
  packageFile: string,
  userFile: string,
): boolean {
  if (!/\.(yaml|yml)$/i.test(packageFile)) {
    return false;
  }
  let pkgVersion: number[] | null;
  let userVersion: number[] | null;
  try {
    pkgVersion = topLevelVersion(fsMod.readFileSync(packageFile, "utf-8"));
    userVersion = fsMod.existsSync(userFile)
      ? topLevelVersion(fsMod.readFileSync(userFile, "utf-8"))
      : null;
  } catch {
    // 读失败（编码/占用等）不覆盖——播种失败面留给既有 try/catch 语义
    return false;
  }
  if (!pkgVersion) {
    return false;
  }
  return !versionStrictlyGreater(userVersion ?? [], pkgVersion);
}

/**
 * yaml 顶层 `version:` 提取（列首无缩进行，值必须点分整数；其余形态含缺席
 * 一律 null）。列首限定排除嵌套键与列表项；只扫 yaml 文本，无需完整解析器
 * （格式由保存面与 CI 把守，此处只做保守识别）。
 */
function topLevelVersion(text: string): number[] | null {
  for (const line of text.split(/\r?\n/)) {
    const m = /^version:\s*(\d+(?:\.\d+)*)\s*(?:#.*)?$/.exec(line);
    if (m) {
      return m[1].split(".").map(Number);
    }
  }
  return null;
}

/** 逐段数值比较（短侧补零）：user 严格大于 pkg 才真。 */
function versionStrictlyGreater(user: number[], pkg: number[]): boolean {
  const len = Math.max(user.length, pkg.length);
  for (let i = 0; i < len; i += 1) {
    const u = user[i] ?? 0;
    const p = pkg[i] ?? 0;
    if (u !== p) {
      return u > p;
    }
  }
  return false;
}

/**
 * 构造内核子进程环境。
 *
 * - AGENTOS_BIND=127.0.0.1：显式钉回环（KERNEL_ORIGIN 硬编码 127.0.0.1 回环，
 *   回环足够且不暴露外网；内核以 AGENTOS_BIND 为主键，AGENTOS_KERNEL_HOST 为
 *   弃用别名，ambient 残留值因 AGENTOS_BIND 恒被设置而必然失效）；
 * - AGENTOS_KERNEL_PORT：钉为 resolveKernelPort 解析值——内核监听端口与健康
 *   探测（KERNEL_HEALTH_URL）恒一致，ambient 值不致造成两者分叉；
 * - AGENTOS_PLUGINS_DIR：指向包内内置插件根；
 * - AGENTOS_CONFIG_ROOT：钉用户空间 `<userRoot>/config`
 *   （resolvePackagedConfigRootDir 解析值，ADR 2026-10-17）——配置真值可写
 *   且升级不丢，且**按路径直读配置的插件能真正读到用户改的值**（指包内时
 *   用户空间配置永不被读）；包内 `resources/config` 经 seedConfigTree 首次
 *   播种，用户空间缺项由出厂树补齐。configRoot 未解析到时回落包内资源；
 *   ambient 显式设置优先（显式钉配置根的能力归用户）；
 * - AGENTOS_DB_PATH：BUG-85 库位置改用户根——仅当 ambient 未设时注入
 *   resolvePackagedDbPath 解析值（显式覆盖能力归用户，空白串视为未设）；
 *   不注入时包内 storage.yaml 的相对路径会把库锚进安装目录，静默卸载即删库；
 * - AGENTOS_CONFIG_USERS_DIR：授权名单真值钉用户空间（ADR 2026-09-24 决策5，
 *   恒为 `<userRoot>/config/users`）——经 seedConfigTree 随配置树首次播种，
 *   授权卡永久项写用户空间，升级不丢；ambient 显式设置优先；
 * - AGENTOS_PLUGIN_SOURCE_PRIORITY=builtin：双源同 id 裁决置内置优先——装机版
 *   用户空间的共享插件副本是旧安装/旧迁移遗留（陈旧/扁平布局/.venv 缺失），
 *   不得压过包内版本匹配的新版（BUG-55，ADR
 *   2026-09-20-packaged-dual-source-adjudication）；用户根仍承载内置没有的
 *   额外插件；
 * - AGENTOS_PLUGIN_VENV_AUTOPROVISION=1：boot 后台对缺 .venv 的 Python sidecar
 *   跑 `uv sync` 自愈（打包资源排除 .venv 且装机链没有 dev launcher；同 ADR
 *   决策②；uv 缺席/失败仅 warn 降级）；
 * - AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS=300：硬设（打包件行为固定）；
 * - AGENTOS_ADMIN_PASSWORD：不注入——ambient 值经原样继承直达内核（ADR
 *   2026-10-02-packaged-auto-login-env-only：env 是自动登录口令唯一事实源，
 *   launcher 播种是部署契约，缺失即部署 bug，应用绝不自造口令）。
 *
 * 其余变量原样继承（Windows 子进程需要 SystemRoot 等系统环境）。
 */
export function buildKernelEnv(
  base: NodeJS.ProcessEnv,
  paths: KernelResourcePaths,
  tokenSecret?: string,
  dbPath?: string,
  configUsersDir?: string,
  configRoot?: string,
): NodeJS.ProcessEnv {
  return {
    ...base,
    AGENTOS_BIND: "127.0.0.1",
    AGENTOS_KERNEL_PORT: String(KERNEL_PORT),
    AGENTOS_PLUGINS_DIR: paths.pluginsDir,
    AGENTOS_CONFIG_ROOT: envValue(base, "AGENTOS_CONFIG_ROOT") ?? configRoot ?? paths.configRoot,
    AGENTOS_PLUGIN_SOURCE_PRIORITY: "builtin",
    AGENTOS_PLUGIN_VENV_AUTOPROVISION: "1",
    AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS: PLUGIN_IDLE_TIMEOUT_SECS,
    // token 签名密钥持久化（每安装身份一份）：不注入时内核每进程随机签名，
    // 重启后全部 token 失效=每次打开应用都要重新登录
    ...(tokenSecret ? { AGENTOS_TOKEN_SECRET: tokenSecret } : {}),
    // 库位置钉用户根（BUG-85）：ambient 显式设置优先（覆盖能力保留）
    ...(dbPath && !envValue(base, "AGENTOS_DB_PATH")
      ? { AGENTOS_DB_PATH: dbPath }
      : {}),
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
    const res = await fetchImpl(url, {
      signal: AbortSignal.timeout(timeoutMs),
    });
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
/**
 * 当前安装身份的配置根真值（ensure 时解析 + 播种一次；
 * ADR 2026-10-17-installed-config-root-user-space）。播种失败时置 null——
 * buildKernelEnv 随之回落包内出厂配置根（宁可读旧配置，不可读半份配置）。
 */
let packagedConfigRootDir: string | null = null;
/** 装机版自动登录凭据：username 固定内置 admin，password 即 env 事实源值 */
export interface AdminCredential {
  username: string;
  password: string;
}

/**
 * 解析装机版自动登录凭据（ADR 2026-10-02-packaged-auto-login-env-only）：
 * 只认 ambient `AGENTOS_ADMIN_PASSWORD`——launcher/安装器播种该变量是部署
 * 契约，未播种（含空白串）即部署 bug，返回 null（不自动登录，回落登录框），
 * 绝不自造口令。trim 对齐内核 resolve_admin_password 的「空白即未设」语义。
 */
export function resolveAutoLoginCredential(
  ambient: string | null | undefined,
): AdminCredential | null {
  const password = ambient?.trim();
  if (!password) {
    return null;
  }
  return { username: "admin", password };
}

/** 当前安装身份的自动登录凭据（ensure 时解析一次，重启复用同一份；env 事实源） */
let adminCredential: AdminCredential | null = null;

/** 自动登录用的当前 admin 凭据（未走包内内核 ensure 前为 null；仅装机路径有值） */
export function getPackagedAdminCredential(): AdminCredential | null {
  return adminCredential;
}

/** 内核 stderr 落盘文件名（kernel logs 目录内，与 kernel.log.* 同处） */
const KERNEL_STDERR_FILE = "kernel-stderr.log";

/** 内核日志目录（判活/托盘等观测面复用）：内核按 cwd 相对 `logs/` 写日轮转日志 */
export function kernelLogDir(paths: KernelResourcePaths): string {
  return path.join(paths.kernelDir, "logs");
}

/**
 * 托盘「打开日志目录」落点（P-22 日志可达性）：受管内核日志真身优先（壳拉起
 * 的内核 spawn 时钉住，装机=resources\kernel\logs）；壳未拉起内核（dev 外部
 * 内核）回退同一资源解析（appPath 侧 kernelDir，dev=kernel/logs）——单点
 * 解析，不硬编码双份路径。
 */
export function resolveKernelLogDirForTray(
  managedDir: string | null,
  appPath: string,
): string {
  return managedDir ?? kernelLogDir(kernelResourcePaths(appPath));
}

/**
 * 受管内核的日志目录（spawnKernelProcess 拉起时更新；未由壳拉起过 = null）。
 * 内核日志活性判活（kernel-liveness.ts）据它定位 `kernel.liveness` 标记文件。
 */
let managedLogDir: string | null = null;

/** 受管内核日志目录（未拉起过 = null；dev 外部内核不属于应用管理，恒 null） */
export function managedKernelLogDir(): string | null {
  return managedLogDir;
}

/** 内核 stderr 落盘轮转上限（panic 痕迹低频，8MB 足够追溯） */
export const KERNEL_STDERR_MAX_BYTES = 8 * 1024 * 1024;

/**
 * spawn 前清理过期内核日志（P-29c 保留策略副作用薄层）：名单判过期纯函数
 * 在 diag-log（可单测），这里只执行删除。清理失败仅留痕——保留策略绝不
 * 反噬内核拉起主流程。
 */
function pruneKernelLogsBeforeSpawn(logDir: string): void {
  let expired: string[];
  try {
    expired = pruneExpiredKernelLogs(fs.readdirSync(logDir), new Date());
  } catch {
    return; // 目录不存在（首次启动）= 无可清理
  }
  for (const name of expired) {
    try {
      fs.rmSync(path.join(logDir, name), { force: true });
    } catch (err) {
      console.warn(`[Kernel] 过期日志清理失败（跳过）: ${name}:`, err);
    }
  }
}

/**
 * 追加内核 stderr 到文件（panic/abort 唯一痕迹——装机 2026-10-10 B75 实锤：
 * 内核 0xc0000409 崩溃在 Windows 事件日志外零痕迹，stdio "ignore" 把
 * panic 输出直接丢弃）。轮转单代（.old）；失败静默（观测面不反噬）。
 */
export function appendKernelStderr(logDir: string, chunk: string, maxBytes = KERNEL_STDERR_MAX_BYTES): void {
  try {
    fs.mkdirSync(logDir, { recursive: true });
    const current = path.join(logDir, KERNEL_STDERR_FILE);
    const old = path.join(logDir, `${KERNEL_STDERR_FILE}.old`);
    try {
      if (shouldRotateDiagLog(fs.statSync(current).size, maxBytes)) {
        fs.rmSync(old, { force: true });
        fs.renameSync(current, old);
      }
    } catch {
      // 首次写入前文件不存在 = 正常路径
    }
    fs.appendFileSync(current, chunk, "utf-8");
  } catch {
    // 磁盘故障时观测面退场
  }
}

/** spawn 包内内核（ensure 与自动重启共用的副作用层） */
function spawnKernelProcess(paths: KernelResourcePaths): ChildProcess {
  pruneKernelLogsBeforeSpawn(kernelLogDir(paths));
  const child = spawn(paths.kernelExe, [], {
    cwd: paths.kernelDir,
    env: buildKernelEnv(
      process.env,
      paths,
      tokenSecret ?? undefined,
      packagedDbPath ?? undefined,
      packagedConfigUsersDir ?? undefined,
      packagedConfigRootDir ?? undefined,
    ),
    // stderr 落盘：panic/abort 痕迹必须可见（stdout 无内容消费，维持 ignore）
    stdio: ["ignore", "ignore", "pipe"],
    windowsHide: true,
  });
  const stderrLogDir = kernelLogDir(paths);
  managedLogDir = stderrLogDir;
  child.stderr?.on("data", (chunk: Buffer) => {
    appendKernelStderr(stderrLogDir, chunk.toString("utf-8"));
  });
  child.stderr?.on("error", () => {
    // 流级错误（进程已死等）不处理：观测面不反噬
  });
  return child;
}

/** 等待内核健康就绪；进程退出或 spawn 错误提前失败（绝不在引导期错杀内核） */
async function awaitKernelReady(
  child: ChildProcess,
  probe: () => Promise<boolean>,
): Promise<{
  ready: boolean;
  spawnError: Error | null;
  exited: boolean;
  exitCode: number | null;
}> {
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
    // 退出事实源（观测契约）：code/signal/重启决策必须落盘——外部树杀（无
    // signal、负 code）、G8 exit(75)、主动关闭在此可辨别（装机 2026-10-10
    // 整树退出无任何痕迹的直接缺口）。
    logDiag(
      `[kernel] exit pid=${child.pid} code=${code} signal=${signal} ` +
        `intentionalShutdown=${intentionalShutdown} decision=${JSON.stringify(decision)}`,
    );
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
    console.error(
      `[Kernel] 自动重启中止：内核组件缺失（${paths.kernelExe}），请重新安装。`,
    );
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
    const { ready, spawnError } = await awaitKernelReady(child, () =>
      probeKernelHealth(fetch),
    );
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
  /** 认证身份目录（main.resolveAuthIdentityDir，机器级跨安装稳定）：解析
   *  持久化 token 签名密钥；其存在性兼作「装机形态」指示（admin 凭据仅在
   *  此形态消费 ambient env） */
  authIdentityDir?: string;
  /** OS 应用数据目录（app.getPath("appData")）：解析装机默认库位置（BUG-85 用户根） */
  appDataDir?: string;
}): Promise<{ mode: "spawned"; pid?: number }> {
  const paths = kernelResourcePaths(opts.resourcesPath);
  if (!fs.existsSync(paths.kernelExe)) {
    throw new KernelMissingError(paths.kernelExe);
  }
  tokenSecret = opts.authIdentityDir
    ? resolveTokenSecret(opts.authIdentityDir)
    : null;
  adminCredential = opts.authIdentityDir
    ? resolveAutoLoginCredential(process.env.AGENTOS_ADMIN_PASSWORD)
    : null;
  if (opts.authIdentityDir && !adminCredential) {
    console.warn(
      "[Kernel] 装机版未设置 AGENTOS_ADMIN_PASSWORD，自动登录不可用" +
        "（部署契约：launcher/安装器须播种该变量；现回落手动登录）",
    );
  }
  packagedDbPath = opts.appDataDir
    ? resolvePackagedDbPath(process.env, opts.appDataDir)
    : null;
  packagedConfigUsersDir = opts.appDataDir
    ? resolvePackagedConfigUsersDir(process.env, opts.appDataDir)
    : null;
  // 配置根钉用户空间（ADR 2026-10-17）：包内 resources/config 降级为**首次
  // 播种源**，用户空间是真值。播种失败不阻断启动——回落包内配置根（旧行为）
  // 好过让内核对着半份配置启动。
  packagedConfigRootDir = null;
  if (opts.appDataDir) {
    const userConfigDir = resolvePackagedConfigRootDir(
      process.env,
      opts.appDataDir,
    );
    try {
      const seeded = seedConfigTree(fs, paths.configRoot, userConfigDir);
      if (seeded.overwritten > 0) {
        // 版本覆盖留痕（ADR 2026-10-24）：覆盖不可见 = 2026-10-24 遮蔽事故
        // 复盘盲区——被治好的陈旧副本必须可从日志追认。
        console.log(
          `[kernel-manager] 出厂配置播种：补缺 ${seeded.copied} 件，版本覆盖 ${seeded.overwritten} 件（用户副本版本不高于包内，按 ADR 2026-10-24 裁决覆盖）。`,
        );
      }
      packagedConfigRootDir = userConfigDir;
    } catch (err) {
      console.warn(
        "[kernel-manager] 配置树播种失败（配置根回落包内出厂配置）:",
        err,
      );
    }
  }

  const probe = opts.probe ?? (() => probeKernelHealth(fetch));
  if (await probe()) {
    throw new KernelPortBusyError(KERNEL_PORT);
  }

  const child = spawnKernelProcess(paths);
  managed = child;

  const { ready, spawnError, exited, exitCode } = await awaitKernelReady(
    child,
    probe,
  );
  if (!ready) {
    shutdownManagedKernel();
    throw (
      spawnError ??
      (exited
        ? new Error(
            `内核进程在启动期间退出（code=${exitCode}）。` +
              `请重试；若反复出现，请反馈内核日志（安装目录 resources\\kernel\\logs\\）。`,
          )
        : new Error(
            `内核在 ${KERNEL_HEALTH_TIMEOUT_MS / 1000} 秒内未就绪，应用将退出。` +
              `请重试；若反复出现，请反馈内核日志（安装目录 resources\\kernel\\logs\\）。`,
          ))
    );
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
  logDiag(`[kernel] shutdownManagedKernel pid=${pid}（主动收树 taskkill /F /T）`);
  killKernelTree(pid);
}

// ── 插件 venv 健康巡检（boot 后台；launcher venv 自愈三犯缺陷的装机侧断言面）──
//
// 背景（装机 2026-10-18 三犯实证）：venv「重建成功」不等于「可用」——部分完成
// 的 uv sync 会留下缺 agentos_plugin_sdk editable 的残缺 venv，门控只看目录/
// 解释器存在就跳过，坏态不可见地传播到装机（合宿组全灭 50 分钟零断言）。内核
// autoprovision（boot 后台 uv sync 重建）的选形同样只认解释器存在。本巡检是
// launcher 侧的补充断言面：内核就绪后逐目标断言「解释器在 + SDK 可 import」，
// 不健康即补装并再断言（有界两轮），坏态必然留日志。uv 缺席/失败仅 warn 降级
// （与内核 autoprovision 同语义，不阻断、不重试风暴）。

/** venv 内 SDK 模块名（import 断言对象）。 */
export const VENV_SDK_MODULE = "agentos_plugin_sdk";
/** pyproject 依赖声明名（声明扫描对象）。 */
export const VENV_SDK_DIST = "agentos-plugin-sdk";

/** 单插件巡检目标（`_host` 共享宿主 + 独立 sidecar）。 */
export interface VenvHealthTarget {
  /** 插件 id 或 "_host"（日志/上报键）。 */
  key: string;
  /** `uv sync --project` 的工程目录。 */
  projectDir: string;
  /** pyproject 声明了 SDK → 做 import 断言；未声明 → 仅解释器存在性。 */
  requiresSdk: boolean;
}

/** venv 目录下的解释器路径（Windows `Scripts\python.exe`，其余 `bin/python`）。 */
export function venvInterpreterPath(
  venvDir: string,
  platform: NodeJS.Platform = process.platform,
): string {
  const exe = platform === "win32" ? "python.exe" : "python";
  return path.join(venvDir, platform === "win32" ? "Scripts" : "bin", exe);
}

/**
 * 枚举巡检目标（与 dev 侧 scripts/ensure_plugin_venvs.py 同规则）：
 * `_host` 恒首个（合宿全体 spawn 的前置）；host_group 成员跳过（运行期零消费）；
 * 有 plugin.json + pyproject.toml 的独立 sidecar 入选。定深一级——dsh_adapter
 * 存在自嵌套 node_modules junction，递归会挂死。
 */
export function enumerateVenvHealthTargets(
  pluginsDir: string,
  fsMod: Pick<
    typeof fs,
    "existsSync" | "readdirSync" | "readFileSync" | "statSync"
  > = fs,
): VenvHealthTarget[] {
  let entries: string[];
  try {
    entries = fsMod.readdirSync(pluginsDir);
  } catch {
    return [];
  }
  const isCohostMember = (dir: string): boolean => {
    try {
      const meta = JSON.parse(
        fsMod.readFileSync(path.join(dir, "plugin.json"), "utf-8"),
      ) as { host_group?: string };
      return Boolean(meta.host_group);
    } catch {
      return false;
    }
  };
  const targets: VenvHealthTarget[] = [];
  for (const name of entries) {
    const dir = path.join(pluginsDir, name);
    try {
      if (!fsMod.statSync(dir).isDirectory()) {
        continue;
      }
    } catch {
      continue;
    }
    if (name !== "_host") {
      if (!fsMod.existsSync(path.join(dir, "plugin.json"))) {
        continue;
      }
      if (isCohostMember(dir)) {
        continue;
      }
    }
    let declaresSdk = false;
    try {
      declaresSdk = fsMod
        .readFileSync(path.join(dir, "pyproject.toml"), "utf-8")
        .includes(VENV_SDK_DIST);
    } catch {
      continue; // 无 pyproject：重建无从谈起（与选形三件套同判）
    }
    targets.push({ key: name, projectDir: dir, requiresSdk: declaresSdk });
  }
  targets.sort((a, b) =>
    a.key === "_host" ? -1 : b.key === "_host" ? 1 : a.key < b.key ? -1 : 1,
  );
  return targets;
}

/** 补装执行体（生产=uv sync 子进程；测试注入假 runner）。 */
export type VenvSyncRunner = (
  projectDir: string,
) => Promise<{ ok: boolean; error?: string }>;
/** SDK import 探测体（生产=venv 解释器子进程；测试注入假 runner）。 */
export type VenvImportRunner = (interpreter: string) => Promise<boolean>;

function awaitProcessClose(child: ChildProcess): Promise<{ code: number | null }> {
  return new Promise((resolve) => {
    child.once("close", (code) => resolve({ code }));
    child.once("error", () => resolve({ code: null }));
  });
}

/** 生产补装：`uv sync --project <dir>`（launcher 完整同步语义，不加 frozen/inexact）。 */
export const uvSyncRunner: VenvSyncRunner = async (projectDir) => {
  try {
    const args = ["sync", "--project", projectDir];
    // 离线依赖库（与内核 venv_provision UV_WHEELHOUSE_ENV 同源）：随包 wheels
    // 在场时只吃本地 wheel，绝不触网（2026-10-26 离线发行裁定）。
    const wheelhouse = process.env.AGENTOS_UV_WHEELHOUSE;
    if (wheelhouse !== undefined && wheelhouse.trim() !== "") {
      // 离线模式锁文件即真相（frozen 禁锁重解析/回写，与内核 sync_args 同语义）
      args.push("--frozen", "--no-index", "--find-links", wheelhouse);
    }
    const child = spawn("uv", args, {
      stdio: "ignore",
      windowsHide: true,
    });
    const timer = setTimeout(() => child.kill(), 300_000);
    timer.unref();
    const { code } = await awaitProcessClose(child);
    return code === 0 ? { ok: true } : { ok: false, error: `uv sync 退出码 ${code}` };
  } catch (err) {
    return { ok: false, error: `uv 拉起失败（是否在 PATH）: ${String(err)}` };
  }
};

/** 生产探测：venv 解释器 `-c "import agentos_plugin_sdk"`，exit 0 = 可用。 */
export const venvImportRunner: VenvImportRunner = async (interpreter) => {
  try {
    const child = spawn(interpreter, ["-c", `import ${VENV_SDK_MODULE}`], {
      stdio: "ignore",
      windowsHide: true,
    });
    const timer = setTimeout(() => child.kill(), 60_000);
    timer.unref();
    const { code } = await awaitProcessClose(child);
    return code === 0;
  } catch {
    return false;
  }
};

/**
 * 一轮 venv 健康巡检：逐目标断言（解释器在 + 声明了 SDK 则 import 必须过），
 * 不健康即补装并再断言（有界两轮），坏态必进日志。单目标失败不牵连其余；
 * 全程无异常抛出（观测面不反噬主流程）。
 *
 * `onSynced` 钩子供测试在假 runner 下建模「补装生效」（真实 uv sync 自带该
 * 副作用，无需钩子）。
 */
export async function patrolPluginVenvs(opts: {
  pluginsDir: string;
  syncRunner?: VenvSyncRunner;
  importRunner?: VenvImportRunner;
  log?: (message: string) => void;
  onSynced?: (projectDir: string) => void;
  fsMod?: Pick<
    typeof fs,
    "existsSync" | "readdirSync" | "readFileSync" | "statSync"
  >;
}): Promise<{
  ok: string[];
  repaired: string[];
  broken: Array<{ key: string; error: string }>;
}> {
  const log = opts.log ?? ((message: string) => console.warn(message));
  const sync = opts.syncRunner ?? uvSyncRunner;
  const probe = opts.importRunner ?? venvImportRunner;
  const fsMod = opts.fsMod ?? fs;
  const report = {
    ok: [] as string[],
    repaired: [] as string[],
    broken: [] as Array<{ key: string; error: string }>,
  };

  const healthy = async (target: VenvHealthTarget): Promise<boolean> => {
    const interpreter = venvInterpreterPath(path.join(target.projectDir, ".venv"));
    if (!fsMod.existsSync(interpreter)) {
      return false;
    }
    if (!target.requiresSdk) {
      return true;
    }
    return probe(interpreter);
  };

  for (const target of enumerateVenvHealthTargets(opts.pluginsDir, fsMod)) {
    if (await healthy(target)) {
      report.ok.push(target.key);
      continue;
    }
    let error = "";
    let fixed = false;
    for (let round = 1; round <= 2 && !fixed; round += 1) {
      const result = await sync(target.projectDir);
      opts.onSynced?.(target.projectDir);
      if (await healthy(target)) {
        fixed = true;
        log(
          `[VenvPatrol] ${target.key} venv 不健康，补装后断言通过（第 ${round} 轮）` +
            (result.ok ? "" : `；sync 自身报错: ${result.error ?? "未知"}`),
        );
        report.repaired.push(target.key);
      } else {
        error = result.error ?? "补装后断言仍未通过";
      }
    }
    if (!fixed) {
      log(
        `[VenvPatrol] ${target.key} venv 两轮补装后仍不可用（${error}）——` +
          "对应工具/合宿组将 502，请检查 uv 输出或网络后重启应用重试",
      );
      report.broken.push({ key: target.key, error });
    }
  }
  return report;
}

// ── uv 引导（2026-10-26 用户裁定「安装上就能用」）────────────────────────────
// 打包资源排除插件 venv（布局门禁现役设计），首启 venv 自愈靠内核 autoprovision
// 拉 PATH 上的 uv——外部净装机没有 uv 即全线瘫痪（venv_provision.rs 原话
// 「uv 是否在 PATH？」）。本段裁定+落位随包副本：本机有 uv 用本机的（不另装）；
// 没有就把随包 prereqs/uv.exe 落到用户根 bin/ 并前置进程 PATH（内核与全部子
// spawn 继承）。纯函数裁定 + fs 副作用分层，与本文件既有纪律同形。

export type UvBootstrapDecision =
  | { action: "none" }
  | { action: "materialize"; bundledUvPath: string; binDir: string; targetUvPath: string }
  | { action: "unavailable"; reason: string };

/** uv 引导裁定（纯函数，fs/平台注入）：PATH 命中 → none；缺而随包在 →
 * materialize；双缺 → unavailable。PATH 空条目跳过，空串视为未设。 */
export function resolveUvBootstrapDecision(input: {
  pathEnv: string | undefined;
  sep: string;
  uvExecutableName: string;
  exists: (p: string) => boolean;
  bundledUvPath: string | null;
  bundledUvExists: boolean;
  binDir: string;
}): UvBootstrapDecision {
  const dirs = (input.pathEnv ?? "").split(input.sep).filter((d) => d.trim() !== "");
  const onPath = dirs.some((dir) => input.exists(path.join(dir, input.uvExecutableName)));
  if (onPath) {
    return { action: "none" };
  }
  if (input.bundledUvPath && input.bundledUvExists) {
    return {
      action: "materialize",
      bundledUvPath: input.bundledUvPath,
      binDir: input.binDir,
      targetUvPath: path.join(input.binDir, input.uvExecutableName),
    };
  }
  return {
    action: "unavailable",
    reason: "PATH 无 uv 且包内无随包副本（prereqs/uv.exe 缺失）——插件 venv 自愈将失败",
  };
}

/**
 * uv 引导副作用（打包 boot 接线）：裁定 → materialize 时落副本 + 前置
 * env.PATH（幂等：目标已存在不重拷；PATH 首位已是 binDir 不重复前置）。
 * none/unavailable 不动 env。返回裁定供调用方记日志。
 */
export function ensureUvForPackagedBoot(input: {
  resourcesPath: string;
  appDataDir: string;
  userRootEnv: string | undefined;
  env: NodeJS.ProcessEnv;
  sep?: string;
  platform?: NodeJS.Platform;
  exists?: (p: string) => boolean;
  copyFileSync?: typeof fs.copyFileSync;
  mkdirSync?: typeof fs.mkdirSync;
}): UvBootstrapDecision {
  const sep = input.sep ?? path.delimiter;
  const platform = input.platform ?? process.platform;
  const uvExecutableName = platform === "win32" ? "uv.exe" : "uv";
  const exists = input.exists ?? fs.existsSync;
  const bundledUvPath = path.join(input.resourcesPath, "prereqs", "uv.exe");
  const userRoot =
    input.userRootEnv && input.userRootEnv.trim() !== ""
      ? input.userRootEnv
      : path.join(input.appDataDir, "agentos");
  const binDir = path.join(userRoot, "bin");
  const decision = resolveUvBootstrapDecision({
    pathEnv: input.env.PATH,
    sep,
    uvExecutableName,
    exists,
    bundledUvPath,
    bundledUvExists: exists(bundledUvPath),
    binDir,
  });
  if (decision.action !== "materialize") {
    return decision;
  }
  const copyFileSync = input.copyFileSync ?? fs.copyFileSync;
  const mkdirSync = input.mkdirSync ?? fs.mkdirSync;
  mkdirSync(decision.binDir, { recursive: true });
  if (!exists(decision.targetUvPath)) {
    copyFileSync(decision.bundledUvPath, decision.targetUvPath);
  }
  const currentPath = input.env.PATH ?? "";
  const firstEntry = currentPath.split(sep)[0] ?? "";
  if (firstEntry !== decision.binDir) {
    input.env.PATH = currentPath === "" ? decision.binDir : decision.binDir + sep + currentPath;
  }
  return decision;
}

// ── 离线 venv 供给 env（2026-10-26 用户裁定「都放进去/离线也能用」）──────────
// 随包 prereqs/python（完整解释器）+ prereqs/wheels（依赖 wheel 库）：boot 注入
// UV_PYTHON 与 AGENTOS_UV_WHEELHOUSE，uv 建 venv 不找系统 python、不触网拉包
// （内核 autoprovision 与本文件 uvSyncRunner 同源消费 wheelhouse env）。

export interface OfflineVenvEnvResult {
  pythonShipped: boolean;
  wheelhouseShipped: boolean;
}

/** 离线供给 env 注入（副作用：写 env；两件皆缺=旧包/dev 形态，零改动）。 */
export function applyOfflineVenvEnv(input: {
  resourcesPath: string;
  env: NodeJS.ProcessEnv;
  exists?: (p: string) => boolean;
}): OfflineVenvEnvResult {
  const exists = input.exists ?? fs.existsSync;
  const result: OfflineVenvEnvResult = { pythonShipped: false, wheelhouseShipped: false };
  const pythonExe = path.join(input.resourcesPath, "prereqs", "python", "python.exe");
  if (exists(pythonExe)) {
    input.env.UV_PYTHON = pythonExe;
    result.pythonShipped = true;
  }
  const wheelsDir = path.join(input.resourcesPath, "prereqs", "wheels");
  if (exists(wheelsDir)) {
    input.env.AGENTOS_UV_WHEELHOUSE = wheelsDir;
    result.wheelhouseShipped = true;
  }
  return result;
}
