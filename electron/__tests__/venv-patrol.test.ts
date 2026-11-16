import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  enumerateVenvHealthTargets,
  patrolPluginVenvs,
  venvInterpreterPath,
} from "../kernel-manager";

/** 造插件目录：manifest/pyproject/venv 解释器桩可各自开关。 */
function makePlugin(
  root: string,
  name: string,
  opts: {
    manifest?: Record<string, unknown> | null;
    declaresSdk?: boolean;
    interpreter?: boolean;
  } = {},
): string {
  const dir = path.join(root, name);
  fs.mkdirSync(dir, { recursive: true });
  if (opts.manifest !== null) {
    fs.writeFileSync(
      path.join(dir, "plugin.json"),
      JSON.stringify(opts.manifest ?? { id: name }),
      "utf-8",
    );
  }
  const sdkLine = opts.declaresSdk === false ? "" : '"agentos-plugin-sdk>=0.2.0",';
  fs.writeFileSync(
    path.join(dir, "pyproject.toml"),
    `[project]\nname = "${name}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = [${sdkLine}]\n`,
    "utf-8",
  );
  if (opts.interpreter !== false) {
    const interp = venvInterpreterPath(path.join(dir, ".venv"));
    fs.mkdirSync(path.dirname(interp), { recursive: true });
    fs.writeFileSync(interp, "", "utf-8");
  }
  return dir;
}

describe("enumerateVenvHealthTargets", () => {
  it("win32 解释器路径 Scripts/python.exe，其余 bin/python", () => {
    const p = venvInterpreterPath(path.join("x", ".venv"));
    expect(path.basename(path.dirname(p))).toBe(
      process.platform === "win32" ? "Scripts" : "bin",
    );
  });

  it("_host 恒首个；host_group 成员跳过；SDK 声明驱动 requiresSdk", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "venvpatrol-"));
    try {
      makePlugin(root, "_host", { manifest: { id: "_host" } });
      makePlugin(root, "approval", {
        manifest: { id: "approval", host_group: "light" },
      });
      makePlugin(root, "bash", { manifest: { id: "bash" } });
      makePlugin(root, "plain", { manifest: { id: "plain" }, declaresSdk: false });

      const targets = enumerateVenvHealthTargets(root);
      expect(targets.map((t) => t.key)).toEqual(["_host", "bash", "plain"]);
      expect(targets.find((t) => t.key === "_host")?.requiresSdk).toBe(true);
      expect(targets.find((t) => t.key === "plain")?.requiresSdk).toBe(false);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("patrolPluginVenvs（launcher venv 自愈断言面）", () => {
  let root = "";
  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), "venvpatrol-"));
  });
  afterEach(() => {
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("核心回归：venv 目录存在但 SDK 缺失 → 补装并再断言 → repaired", async () => {
    makePlugin(root, "alpha");
    const syncs: string[] = [];
    let importResult = false; // 首探缺失，sync 后恢复
    const report = await patrolPluginVenvs({
      pluginsDir: root,
      syncRunner: async (dir) => {
        syncs.push(dir);
        return { ok: true };
      },
      importRunner: async () => importResult,
      onSynced: () => {
        importResult = true; // 建模补装生效
      },
    });
    expect(report.repaired).toEqual(["alpha"]);
    expect(syncs).toHaveLength(1);
    expect(report.broken).toEqual([]);
  });

  it("补装两轮后仍缺 → broken 上报（不得静默绿）", async () => {
    makePlugin(root, "alpha");
    const syncs: string[] = [];
    const report = await patrolPluginVenvs({
      pluginsDir: root,
      syncRunner: async (dir) => {
        syncs.push(dir);
        return { ok: true };
      },
      importRunner: async () => false,
    });
    expect(report.broken.map((b) => b.key)).toEqual(["alpha"]);
    expect(syncs).toHaveLength(2);
    expect(report.repaired).toEqual([]);
  });

  it("健康 venv（SDK 可 import）零补装", async () => {
    makePlugin(root, "alpha");
    const report = await patrolPluginVenvs({
      pluginsDir: root,
      syncRunner: async () => {
        throw new Error("健康目标不得触发补装");
      },
      importRunner: async () => true,
    });
    expect(report.ok).toEqual(["alpha"]);
  });

  it("解释器缺失 → 重建（补装触发，不依赖 import 探测）", async () => {
    makePlugin(root, "alpha", { interpreter: false });
    const syncs: string[] = [];
    let probed = 0;
    const report = await patrolPluginVenvs({
      pluginsDir: root,
      syncRunner: async (dir) => {
        syncs.push(dir);
        return { ok: true };
      },
      // 建模真实 uv sync 效果：重建出解释器（此后健康判定走 import 探测）
      onSynced: (dir) => {
        const interp = venvInterpreterPath(path.join(dir, ".venv"));
        fs.mkdirSync(path.dirname(interp), { recursive: true });
        fs.writeFileSync(interp, "", "utf-8");
      },
      importRunner: async () => {
        probed += 1;
        return true;
      },
    });
    expect(report.repaired).toEqual(["alpha"]);
    expect(syncs).toHaveLength(1);
    expect(probed).toBe(1);
  });

  it("故障键进日志（可见性契约：坏态必须可见，装机三犯的直接缺口）", async () => {
    makePlugin(root, "alpha");
    const lines: string[] = [];
    await patrolPluginVenvs({
      pluginsDir: root,
      syncRunner: async () => ({ ok: false, error: "uv 失败" }),
      importRunner: async () => false,
      log: (msg) => lines.push(msg),
    });
    expect(lines.some((l) => l.includes("alpha"))).toBe(true);
  });
});
