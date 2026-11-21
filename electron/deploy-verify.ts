/**
 * 部署回读校验（安装/升级后首启）：包内 deploy-manifest.json vs 安装位实际字节。
 *
 * ADR 2026-11-10-deploy-freshness-verification：
 *  - 清单由 electron-builder afterPack 钩子（scripts/electron-after-pack.cjs）
 *    从最终产物树生成，随 NSIS 安装包落 `<resources>/deploy-manifest.json`；
 *  - **关键项同步校验**（清单 critical[]：内核 exe、app.asar、各 plugin.json、
 *    frontend index、config 内核配置、_host 宿主）：缺失 / 哈希不符 / 清单版本与
 *    `app.getVersion()` 不一致 → 抛 DeployVerifyError，调用方走「启动失败」错误框
 *    + 退出（关键项拦启动，用户裁定 2026-11-10）；
 *  - **全量校验异步**（venv 以外全部文件）：只报告，结果落
 *    `<user_root>/deploy-verify-state.json`，日志给「本次更新变更 N 项 / 未落地
 *    M 项」；与上一版清单一并做差（上一版存在即代表这是一次升级）；
 *  - 幂等：状态文件 manifest_sha256 命中且 critical_ok → 不再跑全量（关键项
 *    每次启动照跑，代价约 50MB 哈希）；`AGENTOS_DEPLOY_VERIFY=always` 强制重跑；
 *  - 旧包无清单（升级前的历史安装）→ 降级 skip，不拦启动；逃生口
 *    `AGENTOS_DEPLOY_VERIFY=off`（完全跳过）/ `warn`（关键项也只告警）。
 *
 * 与 plugin-tier2-provision 同纪律：纯读取层（清单解析/哈希/比对）与状态落盘
 * 分离，副作用只在 ensureDeployVerified / runFullVerification 内发生。
 */

import { createHash } from "crypto";
import * as fs from "fs";
import * as path from "path";

import { resolveTier2UserRoot } from "./plugin-tier2-provision";

/** 包内清单文件名（afterPack 钩子写入 resources 根） */
export const DEPLOY_MANIFEST_FILENAME = "deploy-manifest.json";
/** 回读状态文件名（<user_root> 下，跨启动幂等） */
export const VERIFY_STATE_FILENAME = "deploy-verify-state.json";
/** 上一版清单副本（<user_root> 下，用于「本次更新 diff」报告） */
export const PREV_MANIFEST_FILENAME = "deploy-manifest.prev.json";
/** 逃生口环境变量 */
export const VERIFY_MODE_ENV = "AGENTOS_DEPLOY_VERIFY";

const STATE_SCHEMA = 1;

export type DeployVerifyMode = "normal" | "warn" | "off" | "always";

/** 清单 files 条目 */
export interface DeployManifestEntry {
  sha256: string;
  bytes: number;
}

/** deploy-manifest.json（schema 1，deploy/verify_deploy_manifest.py 同源） */
export interface DeployManifest {
  schema: 1;
  profile: string;
  version: string;
  build_head: string;
  managed_roots: string[];
  critical: string[];
  files: Record<string, DeployManifestEntry>;
}

export type DeployFindingKind = "missing" | "mismatch" | "extra" | "version";

/** 单条回读 finding（critical 决定是否拦启动） */
export interface DeployFinding {
  kind: DeployFindingKind;
  path: string;
  critical: boolean;
  detail: string;
}

/** 关键项失败：调用方（main.ts）按启动失败处置（错误框 + exit 1） */
export class DeployVerifyError extends Error {
  readonly findings: DeployFinding[];

  constructor(message: string, findings: DeployFinding[]) {
    super(message);
    this.name = "DeployVerifyError";
    this.findings = findings;
  }
}

export interface LoadedManifest {
  manifest: DeployManifest;
  /** manifest 文件字节的 sha256（状态文件幂等键） */
  digest: string;
}

export type DeployVerifyStatus = "ok" | "skipped-current" | "no-manifest" | "disabled" | "failed";

export interface DeployVerifyResult {
  status: DeployVerifyStatus;
  /** 清单内容（status=no-manifest/disabled 时缺省） */
  manifest?: DeployManifest;
  manifestDigest?: string;
  /** 用户根（全量校验状态落盘位置） */
  userRoot?: string;
  /** 关键项 finding（status=failed 时有值；normal 模式下已抛错） */
  failures: DeployFinding[];
}

export interface DeployVerifyOptions {
  resourcesPath: string;
  appDataDir: string;
  userRootEnv?: string;
  packageVersion: string;
  env?: NodeJS.ProcessEnv;
}

export interface VerifyState {
  schema: 1;
  manifest_sha256: string;
  version: string;
  build_head: string;
  verified_at: string;
  critical_ok: boolean;
  findings: { missing: string[]; mismatch: string[]; extra: string[] };
}

export interface FullVerifyReport {
  checked: number;
  findings: DeployFinding[];
  /** 本次更新变更集（无上一版清单时为 null = 首次安装） */
  changed: string[] | null;
  /** 变更集中未落地的路径（直接回答「diff 更新是否被更新部署」） */
  failedChanged: string[];
}

export function resolveVerifyMode(env: NodeJS.ProcessEnv = process.env): DeployVerifyMode {
  const raw = (env[VERIFY_MODE_ENV] || "").trim().toLowerCase();
  if (raw === "off" || raw === "warn" || raw === "always") return raw;
  return "normal";
}

/** 归档本体路径判定（`*.asar`）。 */
export function isAsarArchivePath(file: string): boolean {
  return path.basename(file).toLowerCase().endsWith(".asar");
}

/**
 * 读文件字节：`*.asar` 归档本体必须用 noAsar 逃生口读真实字节。
 *
 * Electron 主进程的 asar fs 拦截把归档自身也当虚拟目录——对
 * `<resources>/app.asar` 直接 `readFileSync` 报
 * `ENOENT,  not found in <resources>/app.asar`（2026-11-10 装机首启实测，
 * 关键项校验因此拦启动）；归档内的普通路径不受影响（不需要也不该绕过）。
 * `process.noAsar` 是 Electron 官方逃生口，非 Electron 环境（单测）无副作用；
 * 只在同步读期间置位并复位，不跨异步边界改变全局行为。
 */
export function readFileBytes(file: string): Buffer {
  if (!isAsarArchivePath(file)) return fs.readFileSync(file);
  const proc = process as NodeJS.Process & { noAsar?: boolean };
  const prev = proc.noAsar;
  proc.noAsar = true;
  try {
    return fs.readFileSync(file);
  } finally {
    proc.noAsar = prev;
  }
}

export function sha256FileSync(file: string): string {
  return createHash("sha256").update(readFileBytes(file)).digest("hex");
}

export function sha256FileStream(file: string): Promise<string> {
  // 归档本体走同步整读（23MB 量级）：noAsar 是进程全局开关，跨 await 置位
  // 会影响并发读面，异步流路径不适合承接归档读取。
  if (isAsarArchivePath(file)) return Promise.resolve(sha256FileSync(file));
  return new Promise((resolve, reject) => {
    const digest = createHash("sha256");
    const stream = fs.createReadStream(file);
    stream.on("data", (chunk) => digest.update(chunk));
    stream.on("error", reject);
    stream.on("end", () => resolve(digest.digest("hex")));
  });
}

function isManifestShape(raw: unknown): raw is DeployManifest {
  if (typeof raw !== "object" || raw === null) return false;
  const value = raw as Record<string, unknown>;
  const files = value.files;
  const critical = value.critical;
  return (
    value.schema === 1 &&
    typeof value.version === "string" &&
    typeof value.build_head === "string" &&
    typeof value.profile === "string" &&
    Array.isArray(value.managed_roots) &&
    Array.isArray(critical) &&
    critical.every((item) => typeof item === "string") &&
    typeof files === "object" &&
    files !== null &&
    Object.values(files as Record<string, unknown>).every(
      (entry) =>
        typeof entry === "object" &&
        entry !== null &&
        typeof (entry as DeployManifestEntry).sha256 === "string" &&
        (entry as DeployManifestEntry).sha256.length === 64 &&
        typeof (entry as DeployManifestEntry).bytes === "number",
    )
  );
}

/** 读包内清单；文件不存在返回 null，内容不可判抛 DeployVerifyError（fail-closed）。 */
export function loadDeployManifest(resourcesPath: string): LoadedManifest | null {
  // resourcesPath 缺席（dev/测试环境未走打包入口）→ 无清单语义，降级不拦
  if (!resourcesPath) return null;
  const manifestPath = path.join(resourcesPath, DEPLOY_MANIFEST_FILENAME);
  if (!fs.existsSync(manifestPath)) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
  } catch (err) {
    throw new DeployVerifyError(
      `部署清单不可解析（${manifestPath}）：${String(err)}\n请用最新安装包覆盖安装。`,
      [{ kind: "version", path: DEPLOY_MANIFEST_FILENAME, critical: true, detail: "清单不可解析" }],
    );
  }
  if (!isManifestShape(raw)) {
    throw new DeployVerifyError(
      `部署清单 schema 违约（${manifestPath}）：请用最新安装包覆盖安装。`,
      [{ kind: "version", path: DEPLOY_MANIFEST_FILENAME, critical: true, detail: "清单 schema 违约" }],
    );
  }
  const digest = createHash("sha256").update(fs.readFileSync(manifestPath)).digest("hex");
  return { manifest: raw, digest };
}

function versionFinding(manifestVersion: string, packageVersion: string): DeployFinding {
  return {
    kind: "version",
    path: DEPLOY_MANIFEST_FILENAME,
    critical: true,
    detail: `清单版本 ${manifestVersion} ≠ 应用版本 ${packageVersion}（asar 与 resources 混版）`,
  };
}

/**
 * 关键项校验（同步、启动前跑）：清单版本一致性 + critical[] 逐文件哈希。
 * hashFile 注入供单测替换；缺省真实 sha256。
 */
export function verifyCritical(
  resourcesPath: string,
  manifest: DeployManifest,
  packageVersion: string,
  hashFile: (file: string) => string = sha256FileSync,
): DeployFinding[] {
  const findings: DeployFinding[] = [];
  if (manifest.version !== packageVersion) {
    findings.push(versionFinding(manifest.version, packageVersion));
  }
  for (const rel of manifest.critical) {
    const file = path.join(resourcesPath, ...rel.split("/"));
    const expected = manifest.files[rel]?.sha256;
    if (!expected) {
      findings.push({ kind: "missing", path: rel, critical: true, detail: "清单 critical 条目缺 files 哈希" });
      continue;
    }
    if (!fs.existsSync(file)) {
      findings.push({ kind: "missing", path: rel, critical: true, detail: "清单有条目但安装位不存在" });
      continue;
    }
    const actual = hashFile(file);
    if (actual !== expected) {
      findings.push({
        kind: "mismatch",
        path: rel,
        critical: true,
        detail: `期望 ${expected.slice(0, 16)}… 实际 ${actual.slice(0, 16)}…`,
      });
    }
  }
  return findings;
}

/** 全量校验（异步，只报告）：清单 files 逐项缺失/哈希不符。 */
export async function verifyAll(
  resourcesPath: string,
  manifest: DeployManifest,
  hashFile: (file: string) => Promise<string> = sha256FileStream,
): Promise<DeployFinding[]> {
  const critical = new Set(manifest.critical);
  const findings: DeployFinding[] = [];
  for (const rel of Object.keys(manifest.files).sort()) {
    const file = path.join(resourcesPath, ...rel.split("/"));
    const expected = manifest.files[rel].sha256;
    if (!fs.existsSync(file)) {
      findings.push({ kind: "missing", path: rel, critical: critical.has(rel), detail: "清单有条目但安装位不存在" });
      continue;
    }
    const actual = await hashFile(file);
    if (actual !== expected) {
      findings.push({
        kind: "mismatch",
        path: rel,
        critical: critical.has(rel),
        detail: `期望 ${expected.slice(0, 16)}… 实际 ${actual.slice(0, 16)}…`,
      });
    }
  }
  return findings;
}

/** 两版清单变更集（新增/删除/哈希变化），排序稳定。 */
export function changedPaths(prev: DeployManifest, cur: DeployManifest): string[] {
  const changed: string[] = [];
  const keys = new Set([...Object.keys(prev.files), ...Object.keys(cur.files)]);
  for (const rel of [...keys].sort()) {
    if (prev.files[rel]?.sha256 !== cur.files[rel]?.sha256) changed.push(rel);
  }
  return changed;
}

function statePath(userRoot: string): string {
  return path.join(userRoot, VERIFY_STATE_FILENAME);
}

/** 读状态文件；缺失/损坏/字段不符一律 null（幂等失效 = 重跑校验，不拦启动）。 */
export function readVerifyState(userRoot: string): VerifyState | null {
  const file = statePath(userRoot);
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf-8")) as VerifyState;
    if (
      raw?.schema !== STATE_SCHEMA ||
      typeof raw.manifest_sha256 !== "string" ||
      typeof raw.critical_ok !== "boolean"
    ) {
      return null;
    }
    return raw;
  } catch {
    return null;
  }
}

function writeJson(file: string, value: unknown): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + "\n", "utf-8");
}

function composeFailureMessage(findings: DeployFinding[]): string {
  const lines = findings.map((f) => `- [${f.kind}] ${f.path}：${f.detail}`);
  return [
    `安装位与发版清单不符（${findings.length} 项关键文件）：`,
    ...lines,
    "",
    "应用可能没有升级成功。请用最新安装包覆盖安装；",
    `若为手工维护后的预期状态，可设置 ${VERIFY_MODE_ENV}=warn 临时放行。`,
  ].join("\n");
}

/**
 * 首启校验入口：关键项同步判定（normal 模式不符即抛 DeployVerifyError），
 * 返回是否需要后续跑全量校验（status=ok → 调用方应调度 runFullVerification）。
 */
export function ensureDeployVerified(opts: DeployVerifyOptions): DeployVerifyResult {
  const mode = resolveVerifyMode(opts.env ?? process.env);
  if (mode === "off") return { status: "disabled", failures: [] };

  const loaded = loadDeployManifest(opts.resourcesPath);
  if (!loaded) return { status: "no-manifest", failures: [] };

  const failures = verifyCritical(opts.resourcesPath, loaded.manifest, opts.packageVersion);
  if (failures.length > 0 && mode !== "warn") {
    throw new DeployVerifyError(composeFailureMessage(failures), failures);
  }

  const userRoot = resolveTier2UserRoot(opts.userRootEnv, opts.appDataDir);
  const state = readVerifyState(userRoot);
  const current = state?.manifest_sha256 === loaded.digest && state?.critical_ok === true;
  const status: DeployVerifyStatus =
    failures.length > 0 ? "failed" : current && mode !== "always" ? "skipped-current" : "ok";
  return {
    status,
    manifest: loaded.manifest,
    manifestDigest: loaded.digest,
    userRoot,
    failures,
  };
}

/**
 * 全量回读（内核就绪后异步跑，只报告）：逐文件哈希 + 与上一版清单做差，
 * 结果写状态文件并把当前清单留作下一版的 baseline。
 */
export async function runFullVerification(opts: {
  resourcesPath: string;
  userRoot: string;
  manifest: DeployManifest;
  manifestDigest: string;
  hashFile?: (file: string) => Promise<string>;
}): Promise<FullVerifyReport> {
  let prev: DeployManifest | null = null;
  try {
    const raw = JSON.parse(
      fs.readFileSync(path.join(opts.userRoot, PREV_MANIFEST_FILENAME), "utf-8"),
    ) as unknown;
    if (isManifestShape(raw)) prev = raw;
  } catch {
    prev = null;
  }
  const changed = prev ? changedPaths(prev, opts.manifest) : null;
  const findings = await verifyAll(opts.resourcesPath, opts.manifest, opts.hashFile);
  const failedChanged = changed ? findings.map((f) => f.path).filter((rel) => changed.includes(rel)) : [];

  const state: VerifyState = {
    schema: STATE_SCHEMA,
    manifest_sha256: opts.manifestDigest,
    version: opts.manifest.version,
    build_head: opts.manifest.build_head,
    verified_at: new Date().toISOString(),
    critical_ok: findings.every((f) => !f.critical),
    findings: {
      missing: findings.filter((f) => f.kind === "missing").map((f) => f.path),
      mismatch: findings.filter((f) => f.kind === "mismatch").map((f) => f.path),
      extra: [],
    },
  };
  writeJson(statePath(opts.userRoot), state);
  writeJson(path.join(opts.userRoot, PREV_MANIFEST_FILENAME), opts.manifest);
  return { checked: Object.keys(opts.manifest.files).length, findings, changed, failedChanged };
}

/** 全量报告的一行摘要（diag 日志用）。 */
export function summarizeFullReport(report: FullVerifyReport): string {
  const changedText =
    report.changed === null ? "首次安装" : `本次更新变更 ${report.changed.length} 项，未落地 ${report.failedChanged.length} 项`;
  return `checked=${report.checked} findings=${report.findings.length} ${changedText}`;
}
