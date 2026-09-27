#!/usr/bin/env node
// 打包前 config 净化门禁：开发机绝对路径不得进安装包（fail-closed）。
//
// 背景（2026-09-27 R295 装机实测）：config/agents/main/agentos.yaml 提示词写死
// dev 仓根绝对路径，随 extraResources 原样进包 → 主 agent 按提示词把工作空间
// 锚到 dev 路径 → 隔离容器挂载源悬空（daemon 静默建空目录掩盖）。种子配置中的
// 可变路径一律用占位符（如 prompt_build 的 {{user_root}}）或示例值表达。
//
// 检测优先于替换：替换规则会失配后静默空转（scripts/sync_open_repo.py 的
// isolation root 替换实证），此处命中即红并点名文件行，逼源头修占位。
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const CONFIG_DIR = "config";
const TEXT_EXT = new Set([".yaml", ".yml", ".json", ".md", ".txt", ".toml", ".cfg", ".ini"]);
// dev 机绝对路径指纹（随实证扩充）：盘符+已知 dev 家目录、仓根目录名。
const DEV_PATH_PATTERNS = [/d:[\\/]myproject/i, /container_e17cc5927dfd/i];

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    let st;
    try {
      st = statSync(p);
    } catch {
      continue;
    }
    if (st.isDirectory()) yield* walk(p);
    else yield p;
  }
}

const hits = [];
for (const file of walk(CONFIG_DIR)) {
  if (file.endsWith(".bak")) {
    hits.push(`${file}:0: 打包残渣 .bak 文件不得进包`);
    continue;
  }
  const dot = file.lastIndexOf(".");
  if (dot < 0 || !TEXT_EXT.has(file.slice(dot).toLowerCase())) continue;
  let lines;
  try {
    lines = readFileSync(file, "utf8").split(/\r?\n/);
  } catch {
    continue; // 非 UTF-8 文本按不可读跳过
  }
  lines.forEach((line, i) => {
    for (const re of DEV_PATH_PATTERNS) {
      if (re.test(line)) {
        hits.push(`${file}:${i + 1}: ${line.trim().slice(0, 120)}`);
        break;
      }
    }
  });
}

if (hits.length) {
  console.error(`[sanitize_packaged_config] config 内检出开发机绝对路径/残渣，拒绝打包：`);
  for (const h of hits) console.error(`  ${h}`);
  console.error(`源头改法：可变路径用占位符（{{user_root}} / <SELF_EVOLVE_ROOT> 等），真值落用户空间。`);
  process.exit(1);
}
console.log(`[sanitize_packaged_config] config 净化检查通过（无 dev 绝对路径、无 .bak 残渣）`);
