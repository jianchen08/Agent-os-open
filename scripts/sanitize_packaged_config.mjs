#!/usr/bin/env node
// 打包前 config 净化门禁：出厂面零私有信息，fail-closed。
//
// 背景（2026-09-27 R295 装机实测）：config/agents/main/agentos.yaml 提示词写死
// dev 仓根绝对路径，随 extraResources 原样进包 → 主 agent 按提示词把工作空间
// 锚到 dev 路径 → 隔离容器挂载源悬空（daemon 静默建空目录掩盖）。种子配置中的
// 可变路径一律用占位符（如 prompt_build 的 {{user_root}}）或示例值表达。
//
// 2026-11-18 个人信息出仓处置新增两路（与 deploy/sanitize_seed_config.py 断言面
// 同律，禁两处漂移）：
// - 出厂画像字段实填：config/users/**/profile.md 的个人信息字段（出生年份/性别/
//   身份/专业）必须为空或（开头的全角括号）占位形态——真值在用户空间副本。
// - config 文本面公网 IPv4：可路由公网地址即拦（保留/私网/环回段放行）。
//
// 检测优先于替换：替换规则会失配后静默空转（scripts/sync_open_repo.py 的
// isolation root 替换实证），此处命中即红并点名文件行，逼源头修占位。
import { readdirSync, readFileSync, statSync } from "node:fs";
import { basename, join } from "node:path";

const CONFIG_DIR = "config";
const TEXT_EXT = new Set([".yaml", ".yml", ".json", ".md", ".txt", ".toml", ".cfg", ".ini"]);
// dev 机绝对路径指纹（随实证扩充）：盘符+已知 dev 家目录、仓根目录名。
const DEV_PATH_PATTERNS = [/d:[\\/]myproject/i, /container_e17cc5927dfd/i];

// 画像个人信息字段（出厂占位契约；真值属用户空间 <USER_ROOT>/config/users/）
const PROFILE_FIELD_RE = /^- (出生年份|性别|身份|专业)：(.*)$/;
const PLACEHOLDER_PREFIX = "（";

const IPV4_RE = /\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b/g;

// IPv4 非公网判定（ipaddress.is_global 的 js 镜像；与种子闸同律）：
// 环回/未指定/私网/链路本地/CGNAT/TEST-NET×3/保留组播段均放行。
function isPrivateOrReservedV4(a, b, c, d) {
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && (b === 168 || b === 0)) return true; // 192.168/16 + 192.0.0/24
  if (a === 192 && b === 2) return true; // TEST-NET-1
  if (a === 198 && (b === 18 || b === 19)) return true; // 基准测试段
  if (a === 198 && b === 51 && c === 100) return true; // TEST-NET-2
  if (a === 203 && b === 0 && c === 113) return true; // TEST-NET-3
  if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT
  if (a >= 224) return true; // 组播+保留+广播
  return false;
}

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
    lines = readFileSync(file, "utf-8").split(/\r?\n/);
  } catch {
    continue; // 非 UTF-8 文本按不可读跳过
  }
  const isProfile = basename(file) === "profile.md";
  lines.forEach((line, i) => {
    for (const re of DEV_PATH_PATTERNS) {
      if (re.test(line)) {
        hits.push(`${file}:${i + 1}: ${line.trim().slice(0, 120)}`);
        break;
      }
    }
    if (isProfile) {
      const m = PROFILE_FIELD_RE.exec(line);
      const value = m ? m[2].trim() : "";
      if (value && !value.startsWith(PLACEHOLDER_PREFIX)) {
        hits.push(`${file}:${i + 1}: 画像字段实填（真值属用户空间副本）: ${value.slice(0, 40)}`);
      }
    }
    for (const m of line.matchAll(IPV4_RE)) {
      const [, a, b, c, d] = m;
      if (!isPrivateOrReservedV4(+a, +b, +c, +d)) {
        hits.push(`${file}:${i + 1}: 公网 IPv4 入出厂 config 面: ${m[0]}`);
        break;
      }
    }
  });
}

if (hits.length) {
  console.error(`[sanitize_packaged_config] config 内检出私有信息/开发机路径/残渣，拒绝打包：`);
  for (const h of hits) console.error(`  ${h}`);
  console.error(`源头改法：可变路径用占位符（{{user_root}} / <SELF_EVOLVE_ROOT> 等），画像/端点真值落用户空间。`);
  process.exit(1);
}
console.log("[sanitize_packaged_config] config 净化检查通过（无 dev 绝对路径、无画像实填、无公网 IPv4、无 .bak 残渣）");
