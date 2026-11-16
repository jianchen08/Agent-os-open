#!/usr/bin/env node
// 打包链 cdylib 构建门禁：三件原生插件 crate 编译并把产物落到插件目录根（fail-loud）。
//
// 背景（docs/working/装机版缺陷清偿计划_20261015.md §四）：*.dll/*.so 被 .gitignore，
// HEAD-worktree 打包（electron-builder extraResources 从工作树拷贝）必丢三件 cdylib
// → 装机后 autonomous 编译永久失败，当日第四包靠手工补拷三次才愈。根修 = 打包链
// 自动构建并携带（同云装链 deploy/build.sh cdylib 段形态，本脚本只读参照其约定）。
//
// 规则：
//   - 包名从各 crate 的 Cargo.toml 解析、产物名按平台推导（cdylib_common.mjs），
//     禁硬编码三件名；新增 cdylib crate 只改 CDYLIB_PLUGIN_DIRS。
//   - cargo 缺失 / 编译失败 / 产物缺失或为空 → 非零退出，打包链在此止步
//     （静默降级正是本次装机事故的根因反面）。
//   - 插件目录根产物已存在且新于全部源（src/**、Cargo.toml、Cargo.lock）→ 跳过
//     重编省打包时长；产物缺失不在跳过之列，必走真实构建补齐。
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { resolveCdylibTargets } from "./cdylib_common.mjs";

// crate 源文件集合：Cargo.toml + Cargo.lock（如有）+ src/** 递归。
function collectSources(dir) {
  const out = [join(dir, "Cargo.toml")];
  const lock = join(dir, "Cargo.lock");
  if (existsSync(lock)) out.push(lock);
  const stack = [join(dir, "src")];
  while (stack.length) {
    const cur = stack.pop();
    for (const name of readdirSync(cur)) {
      const p = join(cur, name);
      if (statSync(p).isDirectory()) stack.push(p);
      else out.push(p);
    }
  }
  return out;
}

let built = 0;
let skipped = 0;
for (const target of resolveCdylibTargets()) {
  const artifactAtRoot = join(target.dir, target.artifactName);
  const newestSourceMs = Math.max(...collectSources(target.dir).map((p) => statSync(p).mtimeMs));
  const rootArtifactMs = existsSync(artifactAtRoot) ? statSync(artifactAtRoot).mtimeMs : -1;
  if (rootArtifactMs >= newestSourceMs) {
    skipped += 1;
    console.log(`[build_cdylibs] ${target.rel}/${target.artifactName} 新于全部源，跳过重编`);
    continue;
  }
  console.log(`[build_cdylibs] cargo build --release: ${target.rel} (${target.pkgName})...`);
  const run = spawnSync("cargo", ["build", "--release"], { cwd: target.dir, stdio: "inherit" });
  if (run.error) {
    console.error(`[build_cdylibs] cargo 无法启动（${run.error.message}）：确认 cargo 在 PATH 后重试`);
    process.exit(1);
  }
  if (run.status !== 0) {
    console.error(`[build_cdylibs] ${target.rel} 编译失败（exit ${run.status ?? "null"}），拒绝打包`);
    process.exit(run.status ?? 1);
  }
  const builtArtifact = join(target.dir, "target", "release", target.artifactName);
  if (!existsSync(builtArtifact)) {
    console.error(`[build_cdylibs] 编译成功但产物缺失：${builtArtifact}`);
    process.exit(1);
  }
  copyFileSync(builtArtifact, artifactAtRoot);
  built += 1;
  console.log(`[build_cdylibs] 产物已落插件目录根: ${target.rel}/${target.artifactName}`);
}

// 收尾统一断言：三件产物在根且非空（跳过路径同样过这道断言，不可绕）。
for (const target of resolveCdylibTargets()) {
  const artifactAtRoot = join(target.dir, target.artifactName);
  let size = 0;
  try {
    size = statSync(artifactAtRoot).size;
  } catch {
    /* 缺失按 0 落入下方失败 */
  }
  if (size <= 0) {
    console.error(`[build_cdylibs] 产物缺失或为空：${target.rel}/${target.artifactName}`);
    process.exit(1);
  }
}
console.log(`[build_cdylibs] 完成：重编 ${built} 件、跳过 ${skipped} 件，三件产物均在插件目录根`);
