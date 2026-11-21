// @ci: frontend-test
/**
 * Tier2 插件安装期供给单测（ADR 2026-11-07-plugin-tiered-single-copy-model 刀 3）。
 *
 * 覆盖口径（任务书验收 1/2）：
 *  - 四态：无基线首装 / 未改覆盖 / 改过保留+冲突 / Tier1 跳过；
 *  - 基线与冲突 JSON 按 ADR 决策 2.1 钉死格式逐字段断言；
 *  - 纯函数层注入假视图（无 fs 依赖）；副作用层与 ensureTier2Provisioned
 *    编排走 tmpdir 真实 fs（不 mock 模块，与 kernel-manager 系列同纪律）。
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  BASELINE_FILENAME,
  CONFLICTS_FILENAME,
  applyTier2Provisioning,
  diffAgainstBaseline,
  ensureTier2Provisioned,
  hashPluginTree,
  listSeedPlugins,
  parseTier1Entries,
  planTier2Provisioning,
  resolveTier2UserRoot,
  type PluginBaseline,
  type ProvisionPlan,
  type SeedPlugin,
} from "../plugin-tier2-provision";

let tmpRoot: string;

beforeEach(() => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "tier2-provision-"));
});

afterEach(() => {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
});

/** 造一个插件目录：plugin.json + 若干文件（含子目录） */
function makePlugin(
  root: string,
  dirName: string,
  opts: { id?: string; version?: string; files?: Record<string, string>; manifest?: string },
): string {
  const dir = path.join(root, dirName);
  fs.mkdirSync(dir, { recursive: true });
  if (opts.manifest !== null) {
    fs.writeFileSync(
      path.join(dir, "plugin.json"),
      opts.manifest ??
        JSON.stringify({ id: opts.id ?? dirName, version: opts.version ?? "1.0.0" }),
      "utf-8",
    );
  }
  for (const [rel, content] of Object.entries(opts.files ?? {})) {
    const file = path.join(dir, rel);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, content, "utf-8");
  }
  return dir;
}

const TIER1_YAML = [
  "tier1:",
  "  safety_plugins:",
  "    - approval",
  "    - isolation",
  "  shared_modules:",
  "    - _host",
  "    - _llm_streaming.py",
].join("\n");

describe("parseTier1Entries（Tier1 清单解析）", () => {
  it("标准清单 → safety_plugins + shared_modules 并集", () => {
    expect(parseTier1Entries(TIER1_YAML)).toEqual(
      new Set(["approval", "isolation", "_host", "_llm_streaming.py"]),
    );
  });

  it("缺 tier1 节 → 空集合（不抛错，逐条目跳过由调用方语义定）", () => {
    expect(parseTier1Entries("other: 1\n")).toEqual(new Set());
  });

  it("yaml 语法错误 → 抛错（Tier1 不可辨别绝不解包）", () => {
    expect(() => parseTier1Entries("tier1: [unclosed")).toThrow(/解析失败/);
  });

  it("tier1.safety_plugins 非列表 → 抛错", () => {
    expect(() => parseTier1Entries("tier1:\n  safety_plugins: nope\n")).toThrow(/非列表/);
  });
});

describe("listSeedPlugins（种子清点）", () => {
  it("只收含 plugin.json 的一级目录；id 缺省回落目录名；排序按目录名", () => {
    const seedRoot = path.join(tmpRoot, "shared");
    fs.mkdirSync(seedRoot, { recursive: true });
    makePlugin(seedRoot, "beta", { id: "beta-id", version: "2.0.0" });
    makePlugin(seedRoot, "alpha", { version: "0.1.0" });
    makePlugin(seedRoot, "no-manifest", { manifest: null, files: { "x.py": "1" } });
    makePlugin(seedRoot, "broken-manifest", { manifest: "{not json" });
    fs.writeFileSync(path.join(seedRoot, "loose.py"), "x", "utf-8");

    expect(listSeedPlugins(seedRoot)).toEqual([
      { id: "alpha", dirName: "alpha", version: "0.1.0" },
      { id: "beta-id", dirName: "beta", version: "2.0.0" },
      { id: "broken-manifest", dirName: "broken-manifest", version: "" },
    ]);
  });

  it("种子根不可读 → 抛错（安装损坏显式暴露）", () => {
    expect(() => listSeedPlugins(path.join(tmpRoot, "missing"))).toThrow(/种子根不可读/);
  });
});

describe("hashPluginTree（全树哈希）", () => {
  it("posix 相对路径 + 内容 sha256；venv/缓存/pyc 排除", () => {
    const dir = path.join(tmpRoot, "plugin");
    fs.mkdirSync(path.join(dir, "sub"), { recursive: true });
    fs.mkdirSync(path.join(dir, "__pycache__"), { recursive: true });
    fs.mkdirSync(path.join(dir, ".venv", "Scripts"), { recursive: true });
    fs.mkdirSync(path.join(dir, "node_modules"), { recursive: true });
    fs.writeFileSync(path.join(dir, "server.py"), "print(1)", "utf-8");
    fs.writeFileSync(path.join(dir, "root.pyc"), "junk", "utf-8");
    fs.writeFileSync(path.join(dir, "sub", "util.py"), "print(2)", "utf-8");
    fs.writeFileSync(path.join(dir, "__pycache__", "x.pyc"), "junk", "utf-8");
    fs.writeFileSync(path.join(dir, ".venv", "Scripts", "python.exe"), "junk", "utf-8");
    fs.writeFileSync(path.join(dir, "node_modules", "m.js"), "junk", "utf-8");

    const files = hashPluginTree(dir);
    expect(Object.keys(files)).toEqual(["server.py", "sub/util.py"]);
    // sha256("print(1)") 的十六进制小写
    expect(files["server.py"]).toMatch(/^[0-9a-f]{64}$/);
    expect(files["server.py"]).not.toBe(files["sub/util.py"]);
  });

  it("内容变化 → 哈希变化（区分度）", () => {
    const dir = path.join(tmpRoot, "p");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "a.py"), "v1", "utf-8");
    const h1 = hashPluginTree(dir)["a.py"];
    fs.writeFileSync(path.join(dir, "a.py"), "v2", "utf-8");
    expect(hashPluginTree(dir)["a.py"]).not.toBe(h1);
  });
});

describe("diffAgainstBaseline（基线 diff）", () => {
  it("修改/缺失/新增全部入改动清单，排序稳定", () => {
    const changed = diffAgainstBaseline(
      { "a.py": "h1", "b.py": "CHANGED", "d.py": "NEW" },
      { "a.py": "h1", "b.py": "h2", "c.py": "h3" },
    );
    expect(changed).toEqual(["b.py", "c.py", "d.py"]);
  });

  it("完全一致 → 空清单（未改）", () => {
    expect(diffAgainstBaseline({ "a.py": "h" }, { "a.py": "h" })).toEqual([]);
  });
});

describe("planTier2Provisioning（四态计划）", () => {
  const seeds: SeedPlugin[] = [
    { id: "approval", dirName: "approval", version: "1.0.0" },
    { id: "t2a", dirName: "t2a", version: "2.0.0" },
    { id: "t2b", dirName: "t2b", version: "0.1.0" },
  ];
  const tier1 = new Set(["approval"]);
  const seedFilesOf = {
    t2a: { "server.py": "seed-a", "sub/u.py": "seed-a2" },
    t2b: { "server.py": "seed-b" },
  };
  const baseInput = {
    seeds,
    tier1,
    packageVersion: "9.9.9",
    nowIso: "2026-11-07T00:00:00.000Z",
    readUserVersion: (_id: string, _dir: string) => "",
    readSeedFiles: (_id: string, dir: string) => seedFilesOf[dir as "t2a" | "t2b"],
  };

  it("态 1 首装：无基线 + 用户目录缺失 → 全部 install；Tier1 跳过；基线条目精确", () => {
    const plan = planTier2Provisioning({
      ...baseInput,
      existingBaseline: null,
      readUserFiles: () => null,
    });
    expect(plan.status).toBe("provisioned");
    expect(plan.skippedTier1).toEqual(["approval"]);
    expect(plan.actions).toEqual([
      { kind: "install", pluginId: "t2a", dirName: "t2a", seedVersion: "2.0.0" },
      { kind: "install", pluginId: "t2b", dirName: "t2b", seedVersion: "0.1.0" },
    ]);
    // ADR 钉死格式逐字段
    expect(plan.baseline).toEqual({
      schema: 1,
      package_version: "9.9.9",
      written_at: "2026-11-07T00:00:00.000Z",
      plugins: {
        t2a: { tier: 2, version: "2.0.0", files: seedFilesOf.t2a },
        t2b: { tier: 2, version: "0.1.0", files: seedFilesOf.t2b },
      },
    });
    expect(plan.conflicts).toEqual({
      schema: 1,
      generated_at: "2026-11-07T00:00:00.000Z",
      conflicts: [],
    });
  });

  it("态 2 未改覆盖：基线一致 → overwrite + 基线翻新（版本随种子走）", () => {
    const existingBaseline: PluginBaseline = {
      schema: 1,
      package_version: "9.9.8",
      written_at: "2026-11-06T00:00:00.000Z",
      plugins: {
        t2a: { tier: 2, version: "2.0.0", files: seedFilesOf.t2a },
        t2b: { tier: 2, version: "0.1.0", files: seedFilesOf.t2b },
      },
    };
    const plan = planTier2Provisioning({
      ...baseInput,
      existingBaseline,
      readUserFiles: (_id, dir) => seedFilesOf[dir as "t2a" | "t2b"],
    });
    expect(plan.actions).toEqual([
      { kind: "overwrite", pluginId: "t2a", dirName: "t2a", seedVersion: "2.0.0" },
      { kind: "overwrite", pluginId: "t2b", dirName: "t2b", seedVersion: "0.1.0" },
    ]);
    expect(plan.conflicts.conflicts).toEqual([]);
    expect(plan.baseline.package_version).toBe("9.9.9");
    expect(plan.baseline.plugins.t2a).toEqual({ tier: 2, version: "2.0.0", files: seedFilesOf.t2a });
  });

  it("态 3 改过保留+冲突：diff 命中 → keep-conflict；基线条目保持旧值", () => {
    const existingBaseline: PluginBaseline = {
      schema: 1,
      package_version: "9.9.8",
      written_at: "2026-11-06T00:00:00.000Z",
      plugins: {
        t2a: { tier: 2, version: "2.0.0", files: seedFilesOf.t2a },
        t2b: { tier: 2, version: "0.1.0", files: seedFilesOf.t2b },
      },
    };
    const plan = planTier2Provisioning({
      ...baseInput,
      existingBaseline,
      readUserFiles: (_id, dir) => {
        const files = seedFilesOf[dir as "t2a" | "t2b"];
        // 用户只改了 t2a/server.py
        return dir === "t2a" ? { ...files, "server.py": "user-edited" } : files;
      },
      readUserVersion: (_id, dir) => (dir === "t2a" ? "2.0.0-custom" : "0.1.0"),
    });
    expect(plan.actions).toEqual([
      {
        kind: "keep-conflict",
        pluginId: "t2a",
        dirName: "t2a",
        seedVersion: "2.0.0",
        userVersion: "2.0.0-custom",
        changedFiles: ["server.py"],
      },
      { kind: "overwrite", pluginId: "t2b", dirName: "t2b", seedVersion: "0.1.0" },
    ]);
    // ADR 钉死冲突记录逐字段
    expect(plan.conflicts.conflicts).toEqual([
      {
        plugin_id: "t2a",
        seed_version: "2.0.0",
        user_version: "2.0.0-custom",
        changed_files: ["server.py"],
      },
    ]);
    // 改过的插件基线保持旧值（后续轮仍可对账）
    expect(plan.baseline.plugins.t2a).toBe(existingBaseline.plugins.t2a);
    // 未改的插件基线照常翻新
    expect(plan.baseline.plugins.t2b).toEqual({ tier: 2, version: "0.1.0", files: seedFilesOf.t2b });
  });

  it("态 4 边界：基线条目缺失而用户目录在 → 保守保留 + 冲突（无据不覆盖）", () => {
    const plan = planTier2Provisioning({
      ...baseInput,
      existingBaseline: null,
      readUserFiles: (_id, dir) => (dir === "t2a" ? { "server.py": "user-own" } : null),
      readUserVersion: (_id, dir) => (dir === "t2a" ? "0.0.1" : ""),
    });
    expect(plan.actions).toEqual([
      {
        kind: "keep-conflict",
        pluginId: "t2a",
        dirName: "t2a",
        seedVersion: "2.0.0",
        userVersion: "0.0.1",
        changedFiles: [],
      },
      { kind: "install", pluginId: "t2b", dirName: "t2b", seedVersion: "0.1.0" },
    ]);
    expect(plan.conflicts.conflicts).toEqual([
      { plugin_id: "t2a", seed_version: "2.0.0", user_version: "0.0.1", changed_files: [] },
    ]);
    // 保留的用户目录不入新基线（基线只描述安装器写过的内容）
    expect(plan.baseline.plugins.t2a).toBeUndefined();
  });

  it("边界：基线条目在而用户目录整缺 → 全部基线文件计改动，不复活", () => {
    const existingBaseline: PluginBaseline = {
      schema: 1,
      package_version: "9.9.8",
      written_at: "2026-11-06T00:00:00.000Z",
      plugins: { t2a: { tier: 2, version: "2.0.0", files: seedFilesOf.t2a } },
    };
    const plan = planTier2Provisioning({
      ...baseInput,
      seeds: [seeds[1]],
      existingBaseline,
      readUserFiles: () => null,
    });
    expect(plan.actions[0]).toMatchObject({ kind: "keep-conflict", pluginId: "t2a" });
    expect(plan.conflicts.conflicts[0].changed_files).toEqual([...Object.keys(seedFilesOf.t2a)].sort());
    expect(plan.baseline.plugins.t2a).toBe(existingBaseline.plugins.t2a);
  });

  it("同包版本基线已在 → skipped-current（已就位零动作）", () => {
    const existingBaseline: PluginBaseline = {
      schema: 1,
      package_version: "9.9.9",
      written_at: "2026-11-06T00:00:00.000Z",
      plugins: {},
    };
    const plan = planTier2Provisioning({
      ...baseInput,
      existingBaseline,
      readUserFiles: () => null,
    });
    expect(plan.status).toBe("skipped-current");
    expect(plan.actions).toEqual([]);
  });

  it("Tier1 按 id 命中同样跳过（目录名与 manifest id 双口径）", () => {
    const plan = planTier2Provisioning({
      ...baseInput,
      seeds: [{ id: "approval", dirName: "approval-dir", version: "1.0.0" }],
      existingBaseline: null,
      readUserFiles: () => null,
    });
    expect(plan.skippedTier1).toEqual(["approval-dir"]);
    expect(plan.actions).toEqual([]);
  });
});

describe("applyTier2Provisioning（副作用层，真实 fs）", () => {
  it("install 复制种子（排除垃圾）+ overwrite 整目录替换（残留文件清掉）+ keep-conflict 不碰用户目录", () => {
    const seedRoot = path.join(tmpRoot, "seed");
    const targetRoot = path.join(tmpRoot, "target");
    const userRoot = path.join(tmpRoot, "user");
    // 种子：t2a 带垃圾子目录；t2b 新版只有 new.py
    makePlugin(seedRoot, "t2a", { files: { "server.py": "s", "sub/u.py": "u", "__pycache__/x.pyc": "j" } });
    makePlugin(seedRoot, "t2b", { version: "2.0.0", files: { "new.py": "n" } });
    // 用户既有：t2b 旧版残留 stale.py；t2c 用户改过（keep-conflict）
    makePlugin(targetRoot, "t2b", { files: { "stale.py": "old" } });
    makePlugin(targetRoot, "t2c", { files: { "user.py": "precious" } });

    const plan: ProvisionPlan = {
      status: "provisioned",
      actions: [
        { kind: "install", pluginId: "t2a", dirName: "t2a", seedVersion: "1.0.0" },
        { kind: "overwrite", pluginId: "t2b", dirName: "t2b", seedVersion: "2.0.0" },
        {
          kind: "keep-conflict",
          pluginId: "t2c",
          dirName: "t2c",
          seedVersion: "3.0.0",
          userVersion: "3.0.0-custom",
          changedFiles: ["user.py"],
        },
      ],
      skippedTier1: [],
      baseline: {
        schema: 1,
        package_version: "9.9.9",
        written_at: "2026-11-07T00:00:00.000Z",
        plugins: {},
      },
      conflicts: {
        schema: 1,
        generated_at: "2026-11-07T00:00:00.000Z",
        conflicts: [
          { plugin_id: "t2c", seed_version: "3.0.0", user_version: "3.0.0-custom", changed_files: ["user.py"] },
        ],
      },
    };

    const result = applyTier2Provisioning(plan, { seedRoot, targetRoot, userRoot });
    expect(result).toEqual({ replacedCount: 2, keptConflicts: 1 });

    // install：种子内容落位，垃圾排除
    expect(fs.readFileSync(path.join(targetRoot, "t2a", "server.py"), "utf-8")).toBe("s");
    expect(fs.readFileSync(path.join(targetRoot, "t2a", "sub", "u.py"), "utf-8")).toBe("u");
    expect(fs.existsSync(path.join(targetRoot, "t2a", "__pycache__"))).toBe(false);
    // overwrite：新版落位且残留清掉
    expect(fs.readFileSync(path.join(targetRoot, "t2b", "new.py"), "utf-8")).toBe("n");
    expect(fs.existsSync(path.join(targetRoot, "t2b", "stale.py"))).toBe(false);
    // keep-conflict：用户内容原样
    expect(fs.readFileSync(path.join(targetRoot, "t2c", "user.py"), "utf-8")).toBe("precious");
    // 基线 + 冲突记录落盘（格式由 ensure 编排测试逐字段断言，此处验在位）
    expect(fs.existsSync(path.join(userRoot, BASELINE_FILENAME))).toBe(true);
    expect(fs.existsSync(path.join(userRoot, CONFLICTS_FILENAME))).toBe(true);
  });

  it("skipped-current → 零写（不落基线不落冲突）", () => {
    const userRoot = path.join(tmpRoot, "user2");
    const plan: ProvisionPlan = {
      status: "skipped-current",
      actions: [],
      skippedTier1: [],
      baseline: { schema: 1, package_version: "1", written_at: "x", plugins: {} },
      conflicts: { schema: 1, generated_at: "x", conflicts: [] },
    };
    const result = applyTier2Provisioning(plan, {
      seedRoot: path.join(tmpRoot, "s"),
      targetRoot: path.join(tmpRoot, "t"),
      userRoot,
    });
    expect(result).toEqual({ replacedCount: 0, keptConflicts: 0 });
    // 零写：用户根连目录都不该被创建
    expect(fs.existsSync(userRoot)).toBe(false);
  });
});

describe("ensureTier2Provisioned（编排，真实 fs）", () => {
  /** 造假包 resources：清单 + 种子根 */
  function makeResources(): string {
    const resources = path.join(tmpRoot, "resources");
    fs.mkdirSync(path.join(resources, "config", "kernel"), { recursive: true });
    fs.writeFileSync(
      path.join(resources, "config", "kernel", "plugin_tiers.yaml"),
      "tier1:\n  safety_plugins:\n    - approval\n  shared_modules:\n    - _libs\n",
      "utf-8",
    );
    const seedRoot = path.join(resources, "plugins", "shared");
    makePlugin(seedRoot, "approval", { files: { "server.py": "safety" } });
    makePlugin(seedRoot, "_libs", { manifest: null, files: { "helpers.py": "h" } });
    makePlugin(seedRoot, "demo", { version: "1.0.0", files: { "server.py": "v1", "sub/u.py": "u1" } });
    return resources;
  }

  const run = (resources: string, packageVersion: string) =>
    ensureTier2Provisioned({
      resourcesPath: resources,
      appDataDir: tmpRoot,
      userRootEnv: "",
      packageVersion,
      nowIso: "2026-11-07T12:00:00.000Z",
    });

  it("首装 → provisioned：Tier2 解包、Tier1/共享根不落用户目录；基线/冲突 JSON 逐字段符合 ADR", () => {
    const resources = makeResources();
    const result = run(resources, "0.2.2-test");

    expect(result.status).toBe("provisioned");
    expect(result.installedCount).toBe(1);
    expect(result.overwrittenCount).toBe(0);
    expect(result.conflictIds).toEqual([]);
    expect(result.skippedTier1).toEqual(["approval"]);
    const userRoot = path.join(tmpRoot, "agentos");
    // Tier2 落位
    expect(fs.readFileSync(path.join(userRoot, "plugins", "demo", "server.py"), "utf-8")).toBe("v1");
    // Tier1 目录与无 plugin.json 共享根都不落用户目录
    expect(fs.existsSync(path.join(userRoot, "plugins", "approval"))).toBe(false);
    expect(fs.existsSync(path.join(userRoot, "plugins", "_libs"))).toBe(false);

    // 基线逐字段（ADR 决策 2.1）
    const baseline = JSON.parse(fs.readFileSync(result.baselinePath, "utf-8")) as PluginBaseline;
    expect(baseline.schema).toBe(1);
    expect(baseline.package_version).toBe("0.2.2-test");
    expect(baseline.written_at).toBe("2026-11-07T12:00:00.000Z");
    expect(Object.keys(baseline.plugins)).toEqual(["demo"]);
    expect(baseline.plugins.demo.tier).toBe(2);
    expect(baseline.plugins.demo.version).toBe("1.0.0");
    // 基线 files 含 plugin.json 本体（全树哈希），键为 posix 相对路径
    expect(Object.keys(baseline.plugins.demo.files)).toEqual(["plugin.json", "server.py", "sub/u.py"]);
    expect(baseline.plugins.demo.files["server.py"]).toMatch(/^[0-9a-f]{64}$/);

    // 冲突记录逐字段（首装=空冲突集，全量刷新语义）
    const conflicts = JSON.parse(fs.readFileSync(result.conflictsPath, "utf-8")) as {
      schema: number;
      generated_at: string;
      conflicts: unknown[];
    };
    expect(conflicts).toEqual({ schema: 1, generated_at: "2026-11-07T12:00:00.000Z", conflicts: [] });
  });

  it("同包版本二启 → skipped-current（零重装）", () => {
    const resources = makeResources();
    run(resources, "0.2.2-test");
    const second = run(resources, "0.2.2-test");
    expect(second.status).toBe("skipped-current");
    expect(second.installedCount).toBe(0);
  });

  it("升级未改 → 覆盖 + 基线翻新", () => {
    const resources = makeResources();
    run(resources, "0.2.2-test");
    // 新包：demo 种子升到 1.1.0 且改内容
    fs.writeFileSync(
      path.join(resources, "plugins", "shared", "demo", "plugin.json"),
      JSON.stringify({ id: "demo", version: "1.1.0" }),
      "utf-8",
    );
    fs.writeFileSync(path.join(resources, "plugins", "shared", "demo", "server.py"), "v2", "utf-8");

    const result = run(resources, "0.2.3-test");
    expect(result.status).toBe("provisioned");
    expect(result.overwrittenCount).toBe(1);
    expect(result.conflictIds).toEqual([]);
    const userRoot = path.join(tmpRoot, "agentos");
    expect(fs.readFileSync(path.join(userRoot, "plugins", "demo", "server.py"), "utf-8")).toBe("v2");
    const baseline = JSON.parse(fs.readFileSync(result.baselinePath, "utf-8")) as PluginBaseline;
    expect(baseline.package_version).toBe("0.2.3-test");
    expect(baseline.plugins.demo.version).toBe("1.1.0");
  });

  it("升级遇用户更改 → 保留用户版 + 冲突记录（seed/user 版本与 changed_files 全量入账）", () => {
    const resources = makeResources();
    run(resources, "0.2.2-test");
    // 用户改了副本（内容 + 版本号）
    const userDemo = path.join(tmpRoot, "agentos", "plugins", "demo");
    fs.writeFileSync(path.join(userDemo, "server.py"), "user-hack", "utf-8");
    fs.writeFileSync(
      path.join(userDemo, "plugin.json"),
      JSON.stringify({ id: "demo", version: "1.0.0-mine" }),
      "utf-8",
    );
    // 新包种子升级
    fs.writeFileSync(
      path.join(resources, "plugins", "shared", "demo", "plugin.json"),
      JSON.stringify({ id: "demo", version: "1.1.0" }),
      "utf-8",
    );
    fs.writeFileSync(path.join(resources, "plugins", "shared", "demo", "server.py"), "v2", "utf-8");
    fs.writeFileSync(path.join(resources, "plugins", "shared", "demo", "extra.py"), "e", "utf-8");

    const result = run(resources, "0.2.3-test");
    expect(result.status).toBe("provisioned");
    expect(result.overwrittenCount).toBe(0);
    expect(result.conflictIds).toEqual(["demo"]);

    // 用户版原样保留
    const userRoot = path.join(tmpRoot, "agentos");
    expect(fs.readFileSync(path.join(userRoot, "plugins", "demo", "server.py"), "utf-8")).toBe("user-hack");
    expect(fs.existsSync(path.join(userRoot, "plugins", "demo", "extra.py"))).toBe(false);

    // 冲突记录逐字段。changed_files = 用户相对基线的改动面：server.py 改内容、
    // plugin.json 改版本号字段（两文件哈希均偏离基线）；extra.py 是种子侧新增，
    // 不属用户改动面，不在此列
    const conflicts = JSON.parse(fs.readFileSync(result.conflictsPath, "utf-8")) as {
      schema: number;
      generated_at: string;
      conflicts: Array<{
        plugin_id: string;
        seed_version: string;
        user_version: string;
        changed_files: string[];
      }>;
    };
    expect(conflicts.schema).toBe(1);
    expect(conflicts.conflicts).toEqual([
      {
        plugin_id: "demo",
        seed_version: "1.1.0",
        user_version: "1.0.0-mine",
        changed_files: ["plugin.json", "server.py"],
      },
    ]);

    // 基线保持旧条目（package_version 已翻新为本次包版本）
    const baseline = JSON.parse(fs.readFileSync(result.baselinePath, "utf-8")) as PluginBaseline;
    expect(baseline.package_version).toBe("0.2.3-test");
    expect(baseline.plugins.demo.version).toBe("1.0.0");
  });

  it("基线损坏 → 抛错且不写任何文件（不可读证据不删）", () => {
    const resources = makeResources();
    run(resources, "0.2.2-test");
    const baselinePath = path.join(tmpRoot, "agentos", BASELINE_FILENAME);
    fs.writeFileSync(baselinePath, "{corrupt", "utf-8");
    expect(() => run(resources, "0.2.3-test")).toThrow(/损坏/);
    // 用户目录未被覆盖动作波及
    expect(fs.readFileSync(path.join(tmpRoot, "agentos", "plugins", "demo", "server.py"), "utf-8")).toBe("v1");
  });

  it("结构缺面 → 抛错：Tier1 清单缺失 / 种子根缺失", () => {
    const resources = path.join(tmpRoot, "partial-resources");
    fs.mkdirSync(path.join(resources, "plugins", "shared"), { recursive: true });
    expect(() => run(resources, "0.2.2-test")).toThrow(/Tier1 清单缺失/);

    const noSeed = path.join(tmpRoot, "no-seed-resources");
    fs.mkdirSync(path.join(noSeed, "config", "kernel"), { recursive: true });
    fs.writeFileSync(
      path.join(noSeed, "config", "kernel", "plugin_tiers.yaml"),
      "tier1:\n  safety_plugins: []\n",
      "utf-8",
    );
    expect(() => run(noSeed, "0.2.2-test")).toThrow(/种子根缺失/);
  });

  it("resourcesPath 缺失 → 抛错（非打包形态不应进入）", () => {
    expect(() =>
      ensureTier2Provisioned({
        resourcesPath: "",
        appDataDir: tmpRoot,
        packageVersion: "x",
      }),
    ).toThrow(/resourcesPath 缺失/);
  });
});

describe("resolveTier2UserRoot（用户根口径）", () => {
  it("AGENTOS_USER_ROOT trim 非空优先；否则回落 appData/agentos", () => {
    expect(resolveTier2UserRoot("  D:\\custom-root  ", "C:\\AD")).toBe("D:\\custom-root");
    expect(resolveTier2UserRoot(undefined, "C:\\AD")).toBe(path.join("C:\\AD", "agentos"));
    expect(resolveTier2UserRoot("", "C:\\AD")).toBe(path.join("C:\\AD", "agentos"));
    expect(resolveTier2UserRoot("   ", "C:\\AD")).toBe(path.join("C:\\AD", "agentos"));
  });
});
