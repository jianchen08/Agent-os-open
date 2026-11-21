// @ci: frontend-test
/**
 * 部署回读校验单测（ADR 2026-11-10-deploy-freshness-verification）。
 *
 * 覆盖口径：
 *  - 关键项同步校验：通过 / 缺失 / 哈希不符 / 清单版本混版（normal 抛错、warn 放行）；
 *  - 降级面：无清单（旧包）→ no-manifest；AGENTOS_DEPLOY_VERIFY=off → disabled；
 *  - 幂等：状态文件 manifest_sha256 命中且 critical_ok → skipped-current，
 *    always → 强制重跑；
 *  - 全量异步报告：非关键项 mismatch 分类、变更集 diff（上一版清单）、状态/
 *    上一版清单落盘；首次安装 changed=null。
 *
 * 纪律：走真实 tmpdir fs（与 plugin-tier2-provision.test.ts 同形），不 mock 模块；
 * 哈希用真实 sha256 对照，不钉死实现。
 */

import { createHash } from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  DEPLOY_MANIFEST_FILENAME,
  DeployVerifyError,
  PREV_MANIFEST_FILENAME,
  VERIFY_STATE_FILENAME,
  changedPaths,
  ensureDeployVerified,
  isAsarArchivePath,
  readFileBytes,
  runFullVerification,
  sha256FileSync,
  summarizeFullReport,
  type DeployManifest,
} from "../deploy-verify";

let tmpRoot: string;
let resources: string;
let userRoot: string;

beforeEach(() => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "deploy-verify-"));
  resources = path.join(tmpRoot, "resources");
  userRoot = path.join(tmpRoot, "user_root");
  fs.mkdirSync(resources, { recursive: true });
  fs.mkdirSync(userRoot, { recursive: true });
});

afterEach(() => {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
});

function writeFiles(root: string, files: Record<string, string>): void {
  for (const [rel, content] of Object.entries(files)) {
    const file = path.join(root, ...rel.split("/"));
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, content, "utf-8");
  }
}

const TREE: Record<string, string> = {
  "kernel/agentos-kernel.exe": "kernel-binary",
  "app.asar": "asar",
  "frontend/dist/index.html": "<html></html>",
  "plugins/shared/alpha/plugin.json": '{"id":"alpha"}',
  "plugins/shared/alpha/main.py": "print('alpha')",
  "plugins/shared/_host/host.py": "host",
  "config/kernel/instance.yaml": "schema: 1",
  "skills/demo/SKILL.md": "# demo",
};

function sha256(content: string): string {
  return createHash("sha256").update(Buffer.from(content, "utf-8")).digest("hex");
}

function makeManifest(
  files: Record<string, string>,
  opts: { version?: string; critical?: string[]; buildHead?: string } = {},
): DeployManifest {
  const critical = opts.critical ?? [
    "kernel/agentos-kernel.exe",
    "app.asar",
    "frontend/dist/index.html",
    "plugins/shared/alpha/plugin.json",
    "plugins/shared/_host/host.py",
    "config/kernel/instance.yaml",
  ];
  return {
    schema: 1,
    profile: "windows",
    version: opts.version ?? "0.2.2",
    build_head: opts.buildHead ?? "deadbeef",
    managed_roots: ["bin", "config", "frontend", "kernel", "plugins", "prereqs", "skills"],
    critical,
    files: Object.fromEntries(
      Object.entries(files).map(([rel, content]) => [rel, { sha256: sha256(content), bytes: Buffer.byteLength(content) }]),
    ),
  };
}

function writeManifest(manifest: DeployManifest, root = resources): string {
  const file = path.join(root, DEPLOY_MANIFEST_FILENAME);
  fs.writeFileSync(file, JSON.stringify(manifest, null, 2) + "\n", "utf-8");
  return file;
}

function manifestDigest(file: string): string {
  return createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function verify(opts: { version?: string; env?: NodeJS.ProcessEnv } = {}) {
  return ensureDeployVerified({
    resourcesPath: resources,
    appDataDir: tmpRoot,
    userRootEnv: userRoot,
    packageVersion: opts.version ?? "0.2.2",
    env: opts.env ?? {},
  });
}

function setupHappyPath(): DeployManifest {
  writeFiles(resources, TREE);
  const manifest = makeManifest(TREE);
  writeManifest(manifest);
  return manifest;
}

describe("关键项同步校验", () => {
  it("无清单（旧包）→ no-manifest 降级", () => {
    const result = verify();
    expect(result.status).toBe("no-manifest");
    expect(result.failures).toEqual([]);
  });

  it("AGENTOS_DEPLOY_VERIFY=off → disabled 且不读清单", () => {
    writeFiles(resources, TREE);
    const manifest = makeManifest(TREE);
    manifest.version = "0.0.1"; // 版本混版本应抛错，off 下必须完全跳过
    writeManifest(manifest);
    const result = verify({ env: { AGENTOS_DEPLOY_VERIFY: "off" } });
    expect(result.status).toBe("disabled");
  });

  it("全部匹配 → ok", () => {
    setupHappyPath();
    const result = verify();
    expect(result.status).toBe("ok");
    expect(result.failures).toEqual([]);
    expect(result.manifest?.build_head).toBe("deadbeef");
  });

  it("关键项哈希不符 → 抛 DeployVerifyError 并列出文件", () => {
    setupHappyPath();
    fs.writeFileSync(path.join(resources, "kernel", "agentos-kernel.exe"), "tampered");
    expect(() => verify()).toThrowError(DeployVerifyError);
    try {
      verify();
    } catch (err) {
      const typed = err as DeployVerifyError;
      expect(typed.findings.map((f) => f.path)).toContain("kernel/agentos-kernel.exe");
      expect(typed.findings.every((f) => f.critical)).toBe(true);
      expect(typed.message).toContain("覆盖安装");
    }
  });

  it("关键项缺失 → 抛错（missing 分类）", () => {
    setupHappyPath();
    fs.rmSync(path.join(resources, "plugins", "shared", "_host", "host.py"));
    try {
      verify();
      throw new Error("应当抛错");
    } catch (err) {
      const typed = err as DeployVerifyError;
      expect(typed.findings[0].kind).toBe("missing");
      expect(typed.findings[0].path).toBe("plugins/shared/_host/host.py");
    }
  });

  it("清单版本与应用版本混版 → 抛错", () => {
    setupHappyPath();
    expect(() => verify({ version: "0.2.3" })).toThrowError(/混版/);
  });

  it("warn 模式：关键项不符只报告不拦", () => {
    setupHappyPath();
    fs.writeFileSync(path.join(resources, "app.asar"), "tampered");
    const result = verify({ env: { AGENTOS_DEPLOY_VERIFY: "warn" } });
    expect(result.status).toBe("failed");
    expect(result.failures.map((f) => f.path)).toEqual(["app.asar"]);
  });

  it("清单损坏 → 抛错（fail-closed）", () => {
    writeFiles(resources, TREE);
    fs.writeFileSync(path.join(resources, DEPLOY_MANIFEST_FILENAME), "{not json", "utf-8");
    expect(() => verify()).toThrowError(DeployVerifyError);
  });
});

describe("幂等状态", () => {
  it("状态文件命中同清单且 critical_ok → skipped-current", () => {
    setupHappyPath();
    const digest = manifestDigest(path.join(resources, DEPLOY_MANIFEST_FILENAME));
    fs.writeFileSync(
      path.join(userRoot, VERIFY_STATE_FILENAME),
      JSON.stringify({
        schema: 1,
        manifest_sha256: digest,
        version: "0.2.2",
        build_head: "deadbeef",
        verified_at: "2026-11-10T00:00:00.000Z",
        critical_ok: true,
        findings: { missing: [], mismatch: [], extra: [] },
      }),
      "utf-8",
    );
    expect(verify().status).toBe("skipped-current");
    expect(verify({ env: { AGENTOS_DEPLOY_VERIFY: "always" } }).status).toBe("ok");
  });

  it("状态文件损坏 → 视为未校验（重跑全量，不拦启动）", () => {
    setupHappyPath();
    fs.writeFileSync(path.join(userRoot, VERIFY_STATE_FILENAME), "{broken", "utf-8");
    expect(verify().status).toBe("ok");
  });
});

describe("全量异步回读", () => {
  it("非关键项 mismatch 进 findings，状态与上一版清单落盘", async () => {
    const manifest = setupHappyPath();
    fs.writeFileSync(path.join(resources, "skills", "demo", "SKILL.md"), "# changed");
    const digest = manifestDigest(path.join(resources, DEPLOY_MANIFEST_FILENAME));

    const report = await runFullVerification({ resourcesPath: resources, userRoot, manifest, manifestDigest: digest });
    expect(report.checked).toBe(Object.keys(manifest.files).length);
    expect(report.findings.map((f) => f.path)).toEqual(["skills/demo/SKILL.md"]);
    expect(report.findings[0].kind).toBe("mismatch");
    expect(report.findings[0].critical).toBe(false);
    expect(report.changed).toBeNull(); // 无上一版清单 = 首次安装

    const state = JSON.parse(fs.readFileSync(path.join(userRoot, VERIFY_STATE_FILENAME), "utf-8"));
    expect(state.manifest_sha256).toBe(digest);
    expect(state.critical_ok).toBe(true);
    expect(state.findings.mismatch).toEqual(["skills/demo/SKILL.md"]);
    expect(fs.existsSync(path.join(userRoot, PREV_MANIFEST_FILENAME))).toBe(true);
  });

  it("有上一版清单时给出变更集与未落地数", async () => {
    const prevManifest = makeManifest(TREE);
    writeFiles(resources, { ...TREE, "skills/demo/SKILL.md": "# v2" });
    const manifest = makeManifest({ ...TREE, "skills/demo/SKILL.md": "# v2" });
    writeManifest(manifest);
    fs.writeFileSync(path.join(userRoot, PREV_MANIFEST_FILENAME), JSON.stringify(prevManifest), "utf-8");
    fs.writeFileSync(path.join(resources, "skills", "demo", "SKILL.md"), "# v3-not-deployed");
    const digest = manifestDigest(path.join(resources, DEPLOY_MANIFEST_FILENAME));

    const report = await runFullVerification({ resourcesPath: resources, userRoot, manifest, manifestDigest: digest });
    expect(report.changed).toEqual(["skills/demo/SKILL.md"]);
    expect(report.failedChanged).toEqual(["skills/demo/SKILL.md"]);
    expect(summarizeFullReport(report)).toContain("本次更新变更 1 项，未落地 1 项");
  });

  it("changedPaths 排序稳定且覆盖新增/删除", () => {
    const prev = makeManifest({ "a.txt": "1", "b.txt": "2" }, { critical: [] });
    const cur = makeManifest({ "b.txt": "2", "c.txt": "3" }, { critical: [] });
    expect(changedPaths(prev, cur)).toEqual(["a.txt", "c.txt"]);
  });
});

describe("asar 归档本体读取", () => {
  it("只认 .asar 归档本体（归档内路径与近似名不命中）", () => {
    expect(isAsarArchivePath(path.join("resources", "app.asar"))).toBe(true);
    expect(isAsarArchivePath("APP.ASAR")).toBe(true);
    expect(isAsarArchivePath("app.asar.bak")).toBe(false);
    expect(isAsarArchivePath(path.join("app.asar", "inside.js"))).toBe(false);
  });

  it("普通文件读取返回原文且不改全局 noAsar", () => {
    const file = path.join(tmpRoot, "plain.txt");
    fs.writeFileSync(file, "hello", "utf-8");
    expect(readFileBytes(file).toString("utf-8")).toBe("hello");
    expect((process as NodeJS.Process & { noAsar?: boolean }).noAsar).toBeUndefined();
  });

  it("归档本体读取返回真实字节，noAsar 在读取后复位", () => {
    const file = path.join(tmpRoot, "app.asar");
    fs.writeFileSync(file, "archive-bytes", "utf-8");
    expect(readFileBytes(file).toString("utf-8")).toBe("archive-bytes");
    expect((process as NodeJS.Process & { noAsar?: boolean }).noAsar).toBeUndefined();
    expect(sha256FileSync(file)).toBe(sha256("archive-bytes"));
  });

  it("读取前已置位的 noAsar 原值被保留（不误清外部状态）", () => {
    const proc = process as NodeJS.Process & { noAsar?: boolean };
    const file = path.join(tmpRoot, "app.asar");
    fs.writeFileSync(file, "x", "utf-8");
    proc.noAsar = true;
    try {
      expect(readFileBytes(file).toString("utf-8")).toBe("x");
      expect(proc.noAsar).toBe(true);
    } finally {
      delete proc.noAsar;
    }
  });
});
