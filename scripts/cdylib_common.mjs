// cdylib 打包链共享推导：三件原生插件 crate 的定位、Cargo.toml 包名解析与
// 平台产物文件名。打包链两个脚本（build_cdylibs / check_cdylibs_carried）共用
// 同一份推导，防两处各写一份后漂移（产物名漂移 = 静默丢 dll 的同类事故面）。
//
// 产物命名与内核 NativePluginLoader::platform_artifact_name 对齐：裸包名连字符
// 转下划线后，Windows `X.dll` / Linux `libX.so` / macOS `libX.dylib`；Linux 形态
// 与云装链 deploy/build.sh 的 lib<pkg下划线>.so 约定同源。
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { strictEqual, throws } from "node:assert";

export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// 三件 cdylib crate（Cargo.toml + src/ 就在插件目录内），产物拷到插件目录根随
// extraResources 进包（target/ 被 electron-builder filter 排除；*.dll/*.so 被
// .gitignore——HEAD-worktree 打包必丢三件产物即 2026-10-15 装机事故根因）。
// 新增 cdylib crate 只需在此追加目录，包名与产物名从其 Cargo.toml 推导。
export const CDYLIB_PLUGIN_DIRS = [
  "plugins/shared/sensitive_checker",
  "plugins/shared/spill_guard",
  "plugins/shared/tool_core",
];

// 从 Cargo.toml 文本解析 [package] name（纯函数，自检可注入合成文本）。
export function parseCargoPackageName(tomlText) {
  let section = "";
  for (const rawLine of tomlText.split(/\r?\n/)) {
    const line = rawLine.trim();
    const header = line.match(/^\[(.+)\]$/);
    if (header) {
      section = header[1].trim();
      continue;
    }
    if (section !== "package") continue;
    const name = line.match(/^name\s*=\s*"([^"]+)"/);
    if (name) return name[1];
  }
  throw new Error("Cargo.toml 缺少 [package] name，无法推导 cdylib 产物名");
}

// 包名 → 当前平台 cdylib 产物文件名（纯函数；未知平台 fail-loud 不猜）。
export function platformArtifactName(pkgName, platform = process.platform) {
  const stem = pkgName.replaceAll("-", "_");
  if (platform === "win32") return `${stem}.dll`;
  if (platform === "linux") return `lib${stem}.so`;
  if (platform === "darwin") return `lib${stem}.dylib`;
  throw new Error(`不支持的平台 ${platform}：无 cdylib 产物名映射`);
}

// 插件目录清单 → 解析后的构建目标（读 Cargo.toml，缺文件即抛错 fail-loud）。
export function resolveCdylibTargets(platform = process.platform) {
  return CDYLIB_PLUGIN_DIRS.map((rel) => {
    const dir = join(REPO_ROOT, rel);
    const pkgName = parseCargoPackageName(readFileSync(join(dir, "Cargo.toml"), "utf8"));
    return { rel, dir, pkgName, artifactName: platformArtifactName(pkgName, platform) };
  });
}

// 纯函数自检：node scripts/cdylib_common.mjs --self-test
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const sample = [
    "# 顶部注释",
    "[package]",
    'name = "pipeline-tool-core-native"',
    'version = "3.0.0"',
    "",
    "[lib]",
    'crate-type = ["cdylib"]',
    "",
  ].join("\n");
  strictEqual(parseCargoPackageName(sample), "pipeline-tool-core-native");
  strictEqual(parseCargoPackageName('[package]\nname = "x-y" # 行内注释\n'), "x-y");
  throws(() => parseCargoPackageName('[lib]\ncrate-type = ["cdylib"]\n'), /缺少 \[package\] name/);

  strictEqual(platformArtifactName("pipeline-tool-core-native", "win32"), "pipeline_tool_core_native.dll");
  strictEqual(platformArtifactName("pipeline-spill-guard-native", "linux"), "libpipeline_spill_guard_native.so");
  strictEqual(platformArtifactName("pipeline-sensitive-checker-native", "darwin"), "libpipeline_sensitive_checker_native.dylib");
  throws(() => platformArtifactName("x", "freebsd"), /不支持的平台/);

  // 与仓内三件真实 Cargo.toml 交叉核对（推导面 vs 实物名，防与 loader 约定漂移）。
  const targets = resolveCdylibTargets("win32");
  deepStrictEqualTargets(targets);
  console.log("[cdylib_common] 自检通过（包名解析 + 三平台产物名推导 + 实物交叉核对）");
}

function deepStrictEqualTargets(targets) {
  strictEqual(targets.length, 3);
  strictEqual(targets[0].rel, "plugins/shared/sensitive_checker");
  strictEqual(targets[0].artifactName, "pipeline_sensitive_checker_native.dll");
  strictEqual(targets[1].rel, "plugins/shared/spill_guard");
  strictEqual(targets[1].artifactName, "pipeline_spill_guard_native.dll");
  strictEqual(targets[2].rel, "plugins/shared/tool_core");
  strictEqual(targets[2].artifactName, "pipeline_tool_core_native.dll");
}
