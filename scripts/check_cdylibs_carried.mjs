#!/usr/bin/env node
// electron-builder 前的存在性断言：三件 cdylib 产物必须在插件目录根且非空（fail-closed）。
//
// 背景：见 build_cdylibs.mjs 头注。本脚本是打包链最后一道闸——extraResources 从
// 工作树拷贝，插件目录根缺件即安装包必缺；此处红 = 链上构建步骤被绕过或产物被清。
import { statSync } from "node:fs";
import { join } from "node:path";
import { resolveCdylibTargets } from "./cdylib_common.mjs";

const failures = [];
for (const target of resolveCdylibTargets()) {
  let size = 0;
  try {
    size = statSync(join(target.dir, target.artifactName)).size;
  } catch {
    /* 缺失按 0 记失败 */
  }
  if (size <= 0) failures.push(`${target.rel}/${target.artifactName}`);
}

if (failures.length) {
  console.error("[check_cdylibs_carried] cdylib 产物缺失/为空，拒绝打包：");
  for (const f of failures) console.error(`  ${f}`);
  console.error("先跑 npm run build:cdylibs（或 node scripts/build_cdylibs.mjs）再打包。");
  process.exit(1);
}
console.log("[check_cdylibs_carried] 三件 cdylib 产物在插件目录根且非空");
