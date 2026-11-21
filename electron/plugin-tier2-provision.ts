/**
 * Tier2 插件安装期供给：解包 + 基线清单 + 升级覆盖规则（装机安装器侧）。
 *
 * ADR 2026-11-07-plugin-tiered-single-copy-model 刀 3（决策 2.1）：
 *  - Tier1（安全级 + 共享模块，清单 `config/kernel/plugin_tiers.yaml`）留在
 *    应用目录（包内 resources），**绝不解包**到用户目录；
 *  - Tier2（其余功能插件）安装/重装时从包内种子（`resources/plugins/shared`，
 *    纯安装源）解包到 `<user_root>/plugins`，写基线清单
 *    `<user_root>/plugin-baseline.json`；
 *  - 升级覆盖规则（机械可判，逐插件 diff 基线）：
 *      用户目录 == 基线（未改）→ 覆盖 + 基线翻新；
 *      用户目录 != 基线（用户改过）→ 保留用户版 + 写冲突记录
 *      `<user_root>/plugin-upgrade-conflicts.json`（前端告警消费面后续刀接）；
 *  - 基线/冲突 JSON 格式 ADR 钉死（BASELINE_SCHEMA/CONFLICTS_SCHEMA）。
 *
 * 挂点：`main.ts` bootPackagedKernelThenLoad（生产态专用路径）在 spawn 内核
 * 之前调用 ensureTier2Provisioned——「安装完成后首次启动前」语义；失败不反噬
 * 启动（调用方 try/catch + logDiag，与 uv-bootstrap 同语义）。
 *
 * 分层：纯函数层（清单解析/种子清点/哈希/diff/计划）不接触 fs；副作用层
 * （复制/删除/JSON 落盘）只做执行——与本文件同目录 kernel-manager 系列的
 * 注入式单测纪律同形。
 */

import { createHash } from "crypto";
import * as fs from "fs";
import * as path from "path";

import { load as loadYaml } from "js-yaml";

/** 基线清单文件名（ADR 决策 2.1 钉死：<user_root>/plugin-baseline.json） */
export const BASELINE_FILENAME = "plugin-baseline.json";
/** 冲突记录文件名（ADR 决策 2.1 钉死：<user_root>/plugin-upgrade-conflicts.json） */
export const CONFLICTS_FILENAME = "plugin-upgrade-conflicts.json";
/** 包内种子根相对 resourcesPath 的布局（= kernel-manager kernelResourcePaths().pluginsDir 同值，独立推导防跨模块耦合） */
export const SEED_PLUGINS_REL = path.join("plugins", "shared");
/** Tier1 清单相对 resourcesPath 的布局（extraResources config → resources/config） */
export const TIERS_MANIFEST_REL = path.join("config", "kernel", "plugin_tiers.yaml");

/** ADR 钉死的基线 schema 版本 */
const BASELINE_SCHEMA = 1;
/** ADR 钉死的冲突记录 schema 版本 */
const CONFLICTS_SCHEMA = 1;

/**
 * 解包/哈希共用的排除规则（与 package.json extraResources 打包过滤同口径）：
 * 种子随包时已过滤过，此处兜底防 venv/缓存类垃圾进用户目录与基线
 * （dev 直跑/异常包形态下的第二道闸）。
 */
const EXCLUDED_DIR_NAMES = new Set([
  "node_modules",
  "__pycache__",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  "target",
]);
const EXCLUDED_DIR_PREDICATE = (name: string): boolean =>
  EXCLUDED_DIR_NAMES.has(name) || name === ".venv" || name.startsWith(".venv-");
const EXCLUDED_FILE_SUFFIXES = [".pyc"];

function isExcludedEntry(name: string, isDirectory: boolean): boolean {
  return isDirectory ? EXCLUDED_DIR_PREDICATE(name) : EXCLUDED_FILE_SUFFIXES.some((s) => name.endsWith(s));
}

// ── ADR 钉死格式的类型面 ────────────────────────────────────────────────

/** 基线内单插件条目（tier 恒 2：本模块只处理 Tier2） */
export interface BaselinePluginEntry {
  tier: 2;
  /** 种子 plugin.json 的 version（缺省空串） */
  version: string;
  /** 相对插件目录的 posix 路径 → sha256（hex 小写） */
  files: Record<string, string>;
}

/** <user_root>/plugin-baseline.json（ADR 决策 2.1 钉死格式） */
export interface PluginBaseline {
  schema: 1;
  package_version: string;
  written_at: string;
  plugins: Record<string, BaselinePluginEntry>;
}

/** 单条升级冲突（ADR 决策 2.1 钉死格式） */
export interface UpgradeConflict {
  plugin_id: string;
  seed_version: string;
  user_version: string;
  /** 用户相对基线的改动文件（修改/缺失/新增，posix 相对路径，排序稳定） */
  changed_files: string[];
}

/** <user_root>/plugin-upgrade-conflicts.json（ADR 决策 2.1 钉死格式） */
export interface UpgradeConflicts {
  schema: 1;
  generated_at: string;
  conflicts: UpgradeConflict[];
}

/** 包内种子里的一个插件目录 */
export interface SeedPlugin {
  /** 插件身份：plugin.json id（缺省回落目录名，与内核按 manifest id 消费的口径一致） */
  id: string;
  dirName: string;
  version: string;
}

/** 单插件供给动作（计划输出；执行器据此落盘） */
export type ProvisionAction =
  | { kind: "install"; pluginId: string; dirName: string; seedVersion: string }
  | { kind: "overwrite"; pluginId: string; dirName: string; seedVersion: string }
  | {
      kind: "keep-conflict";
      pluginId: string;
      dirName: string;
      seedVersion: string;
      userVersion: string;
      changedFiles: string[];
    };

export interface ProvisionPlan {
  /** provisioned=有动作要执行；skipped-current=同包版本已就位（执行器零写） */
  status: "provisioned" | "skipped-current";
  actions: ProvisionAction[];
  /** Tier1 清单命中而跳过解包的种子目录名 */
  skippedTier1: string[];
  /** 全量新基线（执行器写盘；skipped-current 时无意义） */
  baseline: PluginBaseline;
  /** 本轮冲突集（全量刷新语义：含空集，见台账遗留节） */
  conflicts: UpgradeConflicts;
}

// ── 纯函数层 ────────────────────────────────────────────────────────────

/**
 * 解析 Tier1 清单 yaml 文本 → Tier1 条目名集合（安全插件目录名 + 共享模块
 * 目录/文件名）。清单缺失或结构非法时抛错——Tier1 不可辨别时绝不允许解包
 * （fail-closed：把 approval/_host 落到用户目录 = 模型被破坏）。
 */
export function parseTier1Entries(yamlText: string): Set<string> {
  let doc: unknown;
  try {
    doc = loadYaml(yamlText);
  } catch (err) {
    throw new Error(`plugin_tiers.yaml 解析失败（Tier1 不可辨别，拒绝解包）: ${String(err)}`);
  }
  if (doc === null || typeof doc !== "object" || Array.isArray(doc)) {
    throw new Error("plugin_tiers.yaml 顶层结构非法（需要 tier1 映射），拒绝解包");
  }
  const tier1 = (doc as { tier1?: unknown }).tier1;
  if (tier1 === undefined || tier1 === null) {
    return new Set();
  }
  if (typeof tier1 !== "object" || Array.isArray(tier1)) {
    throw new Error("plugin_tiers.yaml 的 tier1 节非法（需要映射），拒绝解包");
  }
  const entries = new Set<string>();
  for (const key of ["safety_plugins", "shared_modules"] as const) {
    const list = (tier1 as Record<string, unknown>)[key];
    if (list === undefined || list === null) {
      continue;
    }
    if (!Array.isArray(list)) {
      throw new Error(`plugin_tiers.yaml 的 tier1.${key} 非列表，拒绝解包`);
    }
    for (const item of list) {
      if (typeof item === "string" && item.trim() !== "") {
        entries.add(item.trim());
      }
    }
  }
  return entries;
}

/**
 * 清点包内种子根下的插件目录（含 plugin.json 的一级子目录，按目录名排序）。
 * seedRoot 不可读 → 抛错（打包损坏须显式暴露，与 KernelMissingError 同哲学）。
 */
export function listSeedPlugins(
  seedRoot: string,
  fsMod: Pick<typeof fs, "existsSync" | "readdirSync" | "readFileSync" | "statSync"> = fs,
): SeedPlugin[] {
  let entries: fs.Dirent[];
  try {
    entries = fsMod.readdirSync(seedRoot, { withFileTypes: true });
  } catch (err) {
    throw new Error(`包内插件种子根不可读: ${seedRoot}（安装损坏）: ${String(err)}`);
  }
  const seeds: SeedPlugin[] = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) {
      continue;
    }
    const dir = path.join(seedRoot, entry.name);
    if (!fsMod.existsSync(path.join(dir, "plugin.json"))) {
      continue;
    }
    let id = entry.name;
    let version = "";
    try {
      const meta = JSON.parse(fsMod.readFileSync(path.join(dir, "plugin.json"), "utf-8")) as {
        id?: unknown;
        version?: unknown;
      };
      if (typeof meta.id === "string" && meta.id.trim() !== "") {
        id = meta.id.trim();
      }
      if (typeof meta.version === "string") {
        version = meta.version;
      }
    } catch {
      // 清单不可读：身份回落目录名、版本空串，解包照常（基线如实记录）
    }
    seeds.push({ id, dirName: entry.name, version });
  }
  seeds.sort((a, b) => (a.dirName < b.dirName ? -1 : a.dirName > b.dirName ? 1 : 0));
  return seeds;
}

/**
 * 全树哈希：插件目录 → { posix 相对路径: sha256 }（应用排除规则；键排序稳定）。
 * 目录不可读 → 抛错（调用方按各自语义处置：种子侧=安装损坏；用户侧=读不到
 * 视同目录缺失走保守分支）。
 */
export function hashPluginTree(
  pluginDir: string,
  fsMod: Pick<typeof fs, "readdirSync" | "readFileSync" | "statSync"> = fs,
  createHashImpl: typeof createHash = createHash,
): Record<string, string> {
  const files: Record<string, string> = {};
  const walk = (dir: string, rel: string): void => {
    const entries = fsMod.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      if (isExcludedEntry(entry.name, entry.isDirectory())) {
        continue;
      }
      const child = path.join(dir, entry.name);
      const childRel = rel === "" ? entry.name : `${rel}/${entry.name}`;
      if (entry.isDirectory()) {
        walk(child, childRel);
      } else if (entry.isFile()) {
        files[childRel] = createHashImpl("sha256").update(fsMod.readFileSync(child)).digest("hex");
      }
      // 符号链接等特殊条目不入基线（种子是普通文件树；非常规条目跳过）
    }
  };
  walk(pluginDir, "");
  const sorted: Record<string, string> = {};
  for (const key of Object.keys(files).sort()) {
    sorted[key] = files[key];
  }
  return sorted;
}

/**
 * 用户目录当前文件 vs 基线：返回改动文件清单（修改/基线有而现缺/基线无而
 * 新增），posix 相对路径排序稳定。空数组 = 与基线一致（用户未改）。
 */
export function diffAgainstBaseline(
  currentFiles: Record<string, string>,
  baselineFiles: Record<string, string>,
): string[] {
  const changed: string[] = [];
  const keys = new Set([...Object.keys(baselineFiles), ...Object.keys(currentFiles)]);
  for (const key of [...keys].sort()) {
    const base = baselineFiles[key];
    const cur = currentFiles[key];
    if (base !== cur) {
      changed.push(key);
    }
  }
  return changed;
}

/**
 * 供给计划（纯决策；fs 视角经回调注入）：
 *  - 同包版本基线已在 → skipped-current（已就位，零动作）；
 *  - Tier1 命中 → 跳过（skippedTier1 留痕）；
 *  - 无基线条目：用户目录缺失 → install；用户目录在 → keep-conflict
 *    （无基线证据绝不覆盖用户内容——保守面与「宁不删」同向）；
 *  - 有基线条目：未改 → overwrite（覆盖 + 基线翻新）；改过 → keep-conflict
 *    （保留用户版 + 冲突记录；基线条目保持旧值，后续轮仍可对账）。
 * 基线条目存在而用户目录整缺 → 按机械规则视作「改过」（全部基线文件计为
 * 改动），不复活不覆盖，交冲突告警的用户动作处置。
 */
export function planTier2Provisioning(input: {
  seeds: SeedPlugin[];
  tier1: Set<string>;
  existingBaseline: PluginBaseline | null;
  packageVersion: string;
  nowIso: string;
  /** 用户目录当前文件哈希视图；null = 用户目录缺失 */
  readUserFiles: (pluginId: string, dirName: string) => Record<string, string> | null;
  /** 用户目录 plugin.json 版本（缺失/损坏 = 空串） */
  readUserVersion: (pluginId: string, dirName: string) => string;
  /** 种子目录文件哈希视图 */
  readSeedFiles: (pluginId: string, dirName: string) => Record<string, string>;
}): ProvisionPlan {
  const { seeds, tier1, existingBaseline, packageVersion, nowIso } = input;
  if (existingBaseline !== null && existingBaseline.package_version === packageVersion) {
    return {
      status: "skipped-current",
      actions: [],
      skippedTier1: [],
      baseline: existingBaseline,
      conflicts: { schema: CONFLICTS_SCHEMA, generated_at: nowIso, conflicts: [] },
    };
  }

  const actions: ProvisionAction[] = [];
  const skippedTier1: string[] = [];
  const baselinePlugins: Record<string, BaselinePluginEntry> = {};
  const conflicts: UpgradeConflict[] = [];

  for (const seed of seeds) {
    if (tier1.has(seed.dirName) || tier1.has(seed.id)) {
      skippedTier1.push(seed.dirName);
      continue;
    }
    const prior = existingBaseline?.plugins[seed.id] ?? null;
    const userFiles = input.readUserFiles(seed.id, seed.dirName);
    if (prior === null) {
      if (userFiles === null) {
        actions.push({ kind: "install", pluginId: seed.id, dirName: seed.dirName, seedVersion: seed.version });
        baselinePlugins[seed.id] = { tier: 2, version: seed.version, files: input.readSeedFiles(seed.id, seed.dirName) };
      } else {
        // 用户目录在而无基线证据（前代安装残留/用户自放）：保留 + 冲突留痕
        const changedFiles: string[] = [];
        conflicts.push({
          plugin_id: seed.id,
          seed_version: seed.version,
          user_version: input.readUserVersion(seed.id, seed.dirName),
          changed_files: changedFiles,
        });
        actions.push({
          kind: "keep-conflict",
          pluginId: seed.id,
          dirName: seed.dirName,
          seedVersion: seed.version,
          userVersion: conflicts[conflicts.length - 1].user_version,
          changedFiles,
        });
      }
      continue;
    }
    if (userFiles === null) {
      // 基线在而用户目录整缺：机械规则下全部基线文件计为改动（不复活）
      const changedFiles = Object.keys(prior.files).sort();
      conflicts.push({
        plugin_id: seed.id,
        seed_version: seed.version,
        user_version: prior.version,
        changed_files: changedFiles,
      });
      actions.push({
        kind: "keep-conflict",
        pluginId: seed.id,
        dirName: seed.dirName,
        seedVersion: seed.version,
        userVersion: prior.version,
        changedFiles,
      });
      baselinePlugins[seed.id] = prior;
      continue;
    }
    const changedFiles = diffAgainstBaseline(userFiles, prior.files);
    if (changedFiles.length === 0) {
      actions.push({ kind: "overwrite", pluginId: seed.id, dirName: seed.dirName, seedVersion: seed.version });
      baselinePlugins[seed.id] = { tier: 2, version: seed.version, files: input.readSeedFiles(seed.id, seed.dirName) };
    } else {
      const userVersion = input.readUserVersion(seed.id, seed.dirName);
      conflicts.push({ plugin_id: seed.id, seed_version: seed.version, user_version: userVersion, changed_files: changedFiles });
      actions.push({
        kind: "keep-conflict",
        pluginId: seed.id,
        dirName: seed.dirName,
        seedVersion: seed.version,
        userVersion,
        changedFiles,
      });
      baselinePlugins[seed.id] = prior;
    }
  }

  const sortedBaseline: Record<string, BaselinePluginEntry> = {};
  for (const id of Object.keys(baselinePlugins).sort()) {
    sortedBaseline[id] = baselinePlugins[id];
  }
  conflicts.sort((a, b) => (a.plugin_id < b.plugin_id ? -1 : a.plugin_id > b.plugin_id ? 1 : 0));

  return {
    status: "provisioned",
    actions,
    skippedTier1,
    baseline: {
      schema: BASELINE_SCHEMA,
      package_version: packageVersion,
      written_at: nowIso,
      plugins: sortedBaseline,
    },
    conflicts: { schema: CONFLICTS_SCHEMA, generated_at: nowIso, conflicts },
  };
}

// ── 副作用层 ────────────────────────────────────────────────────────────

type ProvisionFs = Pick<
  typeof fs,
  | "existsSync"
  | "mkdirSync"
  | "readdirSync"
  | "readFileSync"
  | "statSync"
  | "copyFileSync"
  | "rmSync"
  | "writeFileSync"
>;

/** 递归复制种子目录 → 目标目录（应用排除规则；目标父链自动创建） */
function copyTree(src: string, dst: string, fsMod: ProvisionFs): void {
  fsMod.mkdirSync(dst, { recursive: true });
  for (const entry of fsMod.readdirSync(src, { withFileTypes: true })) {
    if (isExcludedEntry(entry.name, entry.isDirectory())) {
      continue;
    }
    const from = path.join(src, entry.name);
    const to = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      copyTree(from, to, fsMod);
    } else if (entry.isFile()) {
      fsMod.copyFileSync(from, to);
    }
  }
}

/** 整目录替换：先拆旧树再复制（新版已删文件不得残留；与基线严格一致） */
function replaceTree(src: string, dst: string, fsMod: ProvisionFs): void {
  fsMod.rmSync(dst, { recursive: true, force: true });
  copyTree(src, dst, fsMod);
}

function writeJsonFile(fsMod: ProvisionFs, file: string, value: unknown): void {
  fsMod.mkdirSync(path.dirname(file), { recursive: true });
  fsMod.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, "utf-8");
}

/**
 * 执行供给计划：install/overwrite → 整目录替换；keep-conflict → 不碰用户目录。
 * 计划为 provisioned 时写基线 + 冲突记录（全量刷新）；skipped-current 零写。
 */
export function applyTier2Provisioning(
  plan: ProvisionPlan,
  opts: {
    seedRoot: string;
    targetRoot: string;
    userRoot: string;
    fsMod?: ProvisionFs;
  },
): { replacedCount: number; keptConflicts: number } {
  const fsMod = opts.fsMod ?? (fs as ProvisionFs);
  if (plan.status === "skipped-current") {
    return { replacedCount: 0, keptConflicts: 0 };
  }
  let replacedCount = 0;
  for (const action of plan.actions) {
    if (action.kind === "keep-conflict") {
      continue;
    }
    replaceTree(path.join(opts.seedRoot, action.dirName), path.join(opts.targetRoot, action.dirName), fsMod);
    replacedCount += 1;
  }
  writeJsonFile(fsMod, path.join(opts.userRoot, BASELINE_FILENAME), plan.baseline);
  writeJsonFile(fsMod, path.join(opts.userRoot, CONFLICTS_FILENAME), plan.conflicts);
  return { replacedCount, keptConflicts: plan.conflicts.conflicts.length };
}

/** 装机形态用户根解析（与 kernel-manager resolvePackagedDbPath 同口径） */
export function resolveTier2UserRoot(userRootEnv: string | undefined, appDataDir: string): string {
  const explicit = userRootEnv?.trim();
  return explicit ? explicit : path.join(appDataDir, "agentos");
}

function readBaselineFile(
  userRoot: string,
  fsMod: ProvisionFs,
): PluginBaseline | null {
  const file = path.join(userRoot, BASELINE_FILENAME);
  if (!fsMod.existsSync(file)) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(fsMod.readFileSync(file, "utf-8"));
  } catch (err) {
    throw new Error(
      `${BASELINE_FILENAME} 损坏（不可解析）——拒绝任何覆盖性动作（不可读证据不删），请重装以重建基线: ${String(err)}`,
    );
  }
  if (
    parsed === null ||
    typeof parsed !== "object" ||
    (parsed as { schema?: unknown }).schema !== BASELINE_SCHEMA ||
    typeof (parsed as { package_version?: unknown }).package_version !== "string" ||
    (parsed as { plugins?: unknown }).plugins === null ||
    typeof (parsed as { plugins?: unknown }).plugins !== "object"
  ) {
    throw new Error(
      `${BASELINE_FILENAME} 结构不符合 schema ${BASELINE_SCHEMA}——拒绝任何覆盖性动作（不可读证据不删），请重装以重建基线`,
    );
  }
  return parsed as PluginBaseline;
}

/** ensureTier2Provisioned 的结果摘要（main.ts 记日志用） */
export interface Tier2ProvisionResult {
  status: ProvisionPlan["status"];
  installedCount: number;
  overwrittenCount: number;
  conflictIds: string[];
  skippedTier1: string[];
  baselinePath: string;
  conflictsPath: string;
}

/**
 * 首启供给编排（生产态 boot 专用）：读清单 → 读基线 → 出计划 → 执行。
 *
 * 结构性失败（resourcesPath 缺失 / 清单缺失或非法 / 种子根缺失 / 基线损坏）
 * 一律抛错——调用方（main.ts）try/catch + logDiag 降级，绝不反噬内核拉起；
 * 但绝不静默带着「Tier1 不可辨别」或「基线不可读」的状态做覆盖性动作。
 */
export function ensureTier2Provisioned(input: {
  resourcesPath: string;
  appDataDir: string;
  userRootEnv?: string;
  packageVersion: string;
  nowIso?: string;
  fsMod?: ProvisionFs;
  createHashImpl?: typeof createHash;
}): Tier2ProvisionResult {
  const fsMod = input.fsMod ?? (fs as ProvisionFs);
  const createHashImpl = input.createHashImpl ?? createHash;
  const resourcesPath = input.resourcesPath?.trim();
  if (!resourcesPath) {
    throw new Error("resourcesPath 缺失——非打包形态不应进入 Tier2 供给");
  }
  const seedRoot = path.join(resourcesPath, SEED_PLUGINS_REL);
  const manifestPath = path.join(resourcesPath, TIERS_MANIFEST_REL);
  if (!fsMod.existsSync(manifestPath)) {
    throw new Error(`Tier1 清单缺失: ${manifestPath}（Tier1 不可辨别，拒绝解包）`);
  }
  const tier1 = parseTier1Entries(fsMod.readFileSync(manifestPath, "utf-8"));
  if (!fsMod.existsSync(seedRoot)) {
    throw new Error(`包内插件种子根缺失: ${seedRoot}（安装损坏）`);
  }

  const userRoot = resolveTier2UserRoot(input.userRootEnv, input.appDataDir);
  const targetRoot = path.join(userRoot, "plugins");
  const nowIso = input.nowIso ?? new Date().toISOString();

  const existingBaseline = readBaselineFile(userRoot, fsMod);
  const seeds = listSeedPlugins(seedRoot, fsMod);
  const plan = planTier2Provisioning({
    seeds,
    tier1,
    existingBaseline,
    packageVersion: input.packageVersion,
    nowIso,
    readUserFiles: (_pluginId, dirName) => {
      const dir = path.join(targetRoot, dirName);
      return fsMod.existsSync(dir) ? hashPluginTree(dir, fsMod, createHashImpl) : null;
    },
    readUserVersion: (_pluginId, dirName) => {
      try {
        const meta = JSON.parse(
          fsMod.readFileSync(path.join(targetRoot, dirName, "plugin.json"), "utf-8"),
        ) as { version?: unknown };
        return typeof meta.version === "string" ? meta.version : "";
      } catch {
        return "";
      }
    },
    readSeedFiles: (_pluginId, dirName) =>
      hashPluginTree(path.join(seedRoot, dirName), fsMod, createHashImpl),
  });

  applyTier2Provisioning(plan, { seedRoot, targetRoot, userRoot, fsMod });
  return {
    status: plan.status,
    installedCount: plan.actions.filter((a) => a.kind === "install").length,
    overwrittenCount: plan.actions.filter((a) => a.kind === "overwrite").length,
    conflictIds: plan.conflicts.conflicts.map((c) => c.plugin_id),
    skippedTier1: plan.skippedTier1,
    baselinePath: path.join(userRoot, BASELINE_FILENAME),
    conflictsPath: path.join(userRoot, CONFLICTS_FILENAME),
  };
}
