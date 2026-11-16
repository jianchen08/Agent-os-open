// @ci: frontend-test
/**
 * uv 引导（2026-10-26 用户裁定：安装上就能用——本机有 uv 用本机的，没有就帮他
 * 安装随包副本）纯函数层单测 + 落盘副作用单测（node 环境，真实 fs 走 tmpdir）。
 *
 * 覆盖口径：断行为（裁定三分支 + PATH 前置幂等 + 副本落位）。外部依赖（fs/存在
 * 性判定）注入，不 mock 模块。
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { describe, expect, it } from "vitest";

import {
  applyOfflineVenvEnv,
  ensureUvForPackagedBoot,
  resolveUvBootstrapDecision,
} from "../kernel-manager";

const SEP = process.platform === "win32" ? ";" : ":";

function existsIn(root: string, name: string): boolean {
  return fs.existsSync(path.join(root, name));
}

describe("resolveUvBootstrapDecision", () => {
  const uvName = process.platform === "win32" ? "uv.exe" : "uv";

  it("PATH 命中 → none（本机有 uv 不另装）", () => {
    const dirWithUv = path.join("m", "tools");
    const decision = resolveUvBootstrapDecision({
      pathEnv: [dirWithUv, "other"].join(SEP),
      sep: SEP,
      uvExecutableName: uvName,
      exists: (p) => p === path.join(dirWithUv, uvName),
      bundledUvPath: path.join("r", "prereqs", "uv.exe"),
      bundledUvExists: true,
      binDir: path.join("u", "bin"),
    });
    expect(decision).toEqual({ action: "none" });
  });

  it("PATH 缺 + 随包在 → materialize（目标=用户 bin/uv）", () => {
    const bundled = path.join("r", "prereqs", "uv.exe");
    const binDir = path.join("u", "agentos", "bin");
    const decision = resolveUvBootstrapDecision({
      pathEnv: ["other"].join(SEP),
      sep: SEP,
      uvExecutableName: uvName,
      exists: (p) => p === bundled,
      bundledUvPath: bundled,
      bundledUvExists: true,
      binDir,
    });
    expect(decision).toEqual({
      action: "materialize",
      bundledUvPath: bundled,
      binDir,
      targetUvPath: path.join(binDir, uvName),
    });
  });

  it("PATH 缺 + 随包缺 → unavailable（附原因）", () => {
    const decision = resolveUvBootstrapDecision({
      pathEnv: undefined,
      sep: SEP,
      uvExecutableName: uvName,
      exists: () => false,
      bundledUvPath: null,
      bundledUvExists: false,
      binDir: path.join("u", "bin"),
    });
    expect(decision.action).toBe("unavailable");
    if (decision.action === "unavailable") {
      expect(decision.reason).toContain("uv");
    }
  });

  it("PATH 空串视为未设（空白条目跳过）", () => {
    const decision = resolveUvBootstrapDecision({
      pathEnv: `${SEP}${SEP}`,
      sep: SEP,
      uvExecutableName: uvName,
      exists: () => true,
      bundledUvPath: null,
      bundledUvExists: false,
      binDir: path.join("u", "bin"),
    });
    expect(decision.action).toBe("unavailable");
  });
});

describe("ensureUvForPackagedBoot", () => {
  function makeSandbox(): {
    root: string;
    resourcesPath: string;
    appDataDir: string;
    bundled: string;
  } {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "uv-bootstrap-"));
    const resourcesPath = path.join(root, "resources");
    const appDataDir = path.join(root, "appdata");
    const bundled = path.join(resourcesPath, "prereqs", "uv.exe");
    fs.mkdirSync(path.dirname(bundled), { recursive: true });
    fs.writeFileSync(bundled, "fake-uv-bytes");
    return { root, resourcesPath, appDataDir, bundled };
  }

  it("本机 PATH 有 uv：不动 PATH 不落副本", () => {
    const { root, resourcesPath, appDataDir, bundled } = makeSandbox();
    const pathDir = path.join(root, "machine-tools");
    fs.mkdirSync(pathDir, { recursive: true });
    fs.writeFileSync(path.join(pathDir, "uv.exe"), "machine-uv");
    const env: NodeJS.ProcessEnv = { PATH: pathDir };
    const decision = ensureUvForPackagedBoot({
      resourcesPath,
      appDataDir,
      userRootEnv: undefined,
      env,
      exists: (p) => p === bundled || fs.existsSync(p),
    });
    expect(decision.action).toBe("none");
    expect(env.PATH).toBe(pathDir);
    expect(fs.existsSync(path.join(appDataDir, "agentos", "bin", "uv.exe"))).toBe(false);
  });

  it("PATH 缺：副本落 <用户根>/bin 并前置 PATH（幂等）", () => {
    const { resourcesPath, appDataDir, bundled } = makeSandbox();
    const env: NodeJS.ProcessEnv = { PATH: "C:\\somewhere" };
    const first = ensureUvForPackagedBoot({
      resourcesPath,
      appDataDir,
      userRootEnv: undefined,
      env,
      exists: (p) => p === bundled || fs.existsSync(p),
    });
    expect(first.action).toBe("materialize");
    const binDir = path.join(appDataDir, "agentos", "bin");
    expect(fs.readFileSync(path.join(binDir, "uv.exe"), "utf-8")).toBe("fake-uv-bytes");
    expect(env.PATH).toBe(`${binDir}${SEP}C:\\somewhere`);
    // 幂等：二次调用不重复前置
    ensureUvForPackagedBoot({
      resourcesPath,
      appDataDir,
      userRootEnv: undefined,
      env,
      exists: (p) => p === bundled || fs.existsSync(p),
    });
    expect(env.PATH).toBe(`${binDir}${SEP}C:\\somewhere`);
  });

  it("AGENTOS_USER_ROOT 优先于 appData 默认", () => {
    const { root, resourcesPath, appDataDir, bundled } = makeSandbox();
    const customRoot = path.join(root, "custom-root");
    const env: NodeJS.ProcessEnv = { PATH: "" };
    const decision = ensureUvForPackagedBoot({
      resourcesPath,
      appDataDir,
      userRootEnv: customRoot,
      env,
      exists: (p) => p === bundled || fs.existsSync(p),
    });
    expect(decision.action).toBe("materialize");
    expect(fs.existsSync(path.join(customRoot, "bin", "uv.exe"))).toBe(true);
    expect(env.PATH?.startsWith(path.join(customRoot, "bin"))).toBe(true);
    expect(fs.existsSync(path.join(appDataDir, "agentos"))).toBe(false);
  });
});

describe("applyOfflineVenvEnv", () => {
  function makeSandbox(withPython: boolean, withWheels: boolean): {
    root: string;
    resourcesPath: string;
  } {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "offline-venv-"));
    const resourcesPath = path.join(root, "resources");
    if (withPython) {
      const py = path.join(resourcesPath, "prereqs", "python", "python.exe");
      fs.mkdirSync(path.dirname(py), { recursive: true });
      fs.writeFileSync(py, "fake-python");
    }
    if (withWheels) {
      fs.mkdirSync(path.join(resourcesPath, "prereqs", "wheels"), { recursive: true });
    }
    return { root, resourcesPath };
  }

  it("两件随包在场 → UV_PYTHON 与 AGENTOS_UV_WHEELHOUSE 注入", () => {
    const { resourcesPath } = makeSandbox(true, true);
    const env: NodeJS.ProcessEnv = {};
    const result = applyOfflineVenvEnv({ resourcesPath, env });
    expect(result).toEqual({ pythonShipped: true, wheelhouseShipped: true });
    expect(env.UV_PYTHON).toBe(path.join(resourcesPath, "prereqs", "python", "python.exe"));
    expect(env.AGENTOS_UV_WHEELHOUSE).toBe(path.join(resourcesPath, "prereqs", "wheels"));
  });

  it("两件皆缺（旧包/dev）→ env 零改动", () => {
    const { resourcesPath } = makeSandbox(false, false);
    const env: NodeJS.ProcessEnv = {};
    const result = applyOfflineVenvEnv({ resourcesPath, env });
    expect(result).toEqual({ pythonShipped: false, wheelhouseShipped: false });
    expect(env.UV_PYTHON).toBeUndefined();
    expect(env.AGENTOS_UV_WHEELHOUSE).toBeUndefined();
  });
});
