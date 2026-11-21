/**
 * electron-builder afterPack 钩子：为 Windows 安装包生成部署回读清单。
 *
 * 设计：docs/decisions/2026-11-10-deploy-freshness-verification.md
 *
 * 为什么挂在 afterPack 而不是从源码树预生成：本钩子执行时 extraResources 已
 * 照搬完成、app.asar 已产出，清单哈希就是「用户安装位上应有的字节」——不从
 * 源码树猜过滤器，消灭 package.json extraResources 与清单之间的漂移。
 *
 * 输出：<appOutDir>/resources/deploy-manifest.json，随后被 NSIS 打进安装包；
 * 装完/升级后由 Electron 首启（electron/deploy-verify.ts）与 CI 装后冒烟
 * （scripts/install_smoke_windows.py）回读比对。
 *
 * build_head 取 AGENTOS_BUILD_HEAD（deploy/release_all.sh 注入），缺省回落到
 * 当前仓库 HEAD——发布链与本地打包都能溯源。
 *
 * fail-closed：Python 不可用 / 清单生成失败一律抛错中止出包（清单缺失会让
 * 装后校验静默降级，属不可接受的包装缺陷）。
 */

"use strict";

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const REPO_ROOT = path.resolve(__dirname, "..");
const BUILDER = path.join(REPO_ROOT, "deploy", "verify_deploy_manifest.py");
const PROFILE = "windows";

/** 逐候选探测可用解释器（Windows 用 python，类 Unix 用 python3）。 */
function resolvePython() {
  const candidates = process.platform === "win32" ? ["python", "py"] : ["python3", "python"];
  for (const candidate of candidates) {
    try {
      execFileSync(candidate, ["-c", "pass"], { stdio: "ignore" });
      return candidate;
    } catch {
      // 下一个候选
    }
  }
  return null;
}

function resolveBuildHead() {
  const fromEnv = (process.env.AGENTOS_BUILD_HEAD || "").trim();
  if (fromEnv) return fromEnv;
  try {
    return execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "";
  }
}

/** resources 根：Windows 为 <appOutDir>/resources（本钩子只服务 win32）。 */
function resourcesDirOf(context) {
  return path.join(context.appOutDir, "resources");
}

/**
 * 包内随包 venv 剥离（fail-closed：剥离失败即中止出包）。
 *
 * 随包 venv 无法防挪位（2026-11-12 实证）：pyvenv.cfg home 绝对=装机挪位死；
 * 相对=CWD 依赖（异 cwd 启动即 No Python at）；uv trampoline 另烘创建机基座
 * 路径。现役设计=resources 不带 venv，装后由 boot autoprovision 按包内
 * prereqs/python + wheelhouse 在安装位原地自建。
 */
function stripPackagedVenvs(resourcesDir, python) {
  const stripper = path.join(REPO_ROOT, "scripts", "strip_packaged_venvs.py");
  if (!fs.existsSync(stripper)) {
    throw new Error(`[venv-strip] 剥离脚本缺失: ${stripper}`);
  }
  try {
    execFileSync(python, [stripper, resourcesDir], { cwd: REPO_ROOT, stdio: "inherit" });
  } catch (err) {
    throw new Error(`[venv-strip] 随包 venv 剥离失败：${err && err.message}`);
  }
}

/**
 * 包内 editable 安装重定位（fail-closed：无法重定位即中止出包）。
 *
 * 仓内 venv 整目录照搬进包，``__editable__*`` 里仍是开发机绝对路径；
 * 打包机该路径存在所以 CI 全绿，用户机不存在则 import 失败。
 */
function relocateEditable(resourcesDir, python) {
  const relocator = path.join(REPO_ROOT, "scripts", "relocate_packaged_editable.py");
  if (!fs.existsSync(relocator)) {
    throw new Error(`[editable-relocate] 重定位脚本缺失: ${relocator}`);
  }
  try {
    execFileSync(python, [relocator, resourcesDir], { cwd: REPO_ROOT, stdio: "inherit" });
  } catch (err) {
    throw new Error(
      `[editable-relocate] editable 重定位失败（存在包内无法解析的越界路径）：${err && err.message}`,
    );
  }
}

module.exports = async function afterPack(context) {
  const platform = context.electronPlatformName || process.platform;
  if (platform !== "win32") {
    // 非 Windows 桌面包不构成本仓交付面（NSIS 是唯一桌面装机产物），
    // 无清单时 Electron 首启校验自动降级（日志 skip，不拦启动）。
    console.log(`[deploy-manifest] 跳过（platform=${platform}，仅 win32 出清单）`);
    return;
  }
  const resourcesDir = resourcesDirOf(context);
  if (!fs.existsSync(resourcesDir)) {
    throw new Error(`[deploy-manifest] resources 目录不存在: ${resourcesDir}`);
  }
  const python = resolvePython();
  if (!python) {
    throw new Error("[deploy-manifest] 找不到 python 解释器（清单生成必需；CI 需 setup-python）");
  }
  // 随包 venv 剥离必须在 editable 重定位之前（venv 剥离后其 site-packages 内
  // editable 载体一并消失，重定位只需处理随包解释器侧）；两者都必须在清单
  // 生成之前——清单哈希的是「用户安装位上应有的字节」，改写/剥离后的形态
  // 才是最终态。
  stripPackagedVenvs(resourcesDir, python);
  relocateEditable(resourcesDir, python);
  const version = context.packager?.appInfo?.version || "";
  if (!version) {
    throw new Error("[deploy-manifest] 无法解析应用版本（packager.appInfo.version 为空）");
  }
  const buildHead = resolveBuildHead();
  const outPath = path.join(resourcesDir, "deploy-manifest.json");
  const args = [
    BUILDER,
    "build",
    "--root",
    resourcesDir,
    "--out",
    outPath,
    "--profile",
    PROFILE,
    "--version",
    version,
    "--build-head",
    buildHead,
  ];
  try {
    execFileSync(python, args, { cwd: REPO_ROOT, stdio: "inherit" });
  } catch (err) {
    throw new Error(
      `[deploy-manifest] 清单生成失败（${path.basename(python)} ${BUILDER}）：${err && err.message}`,
    );
  }
  if (!fs.existsSync(outPath)) {
    throw new Error(`[deploy-manifest] 清单未落盘: ${outPath}`);
  }
};
