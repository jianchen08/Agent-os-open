#!/usr/bin/env node
// 打包链备料预检（electron:build 链首）：多端挂载依赖实物在位性 + 自动补齐。
//
// 契约：ADR 2026-10-18-unified-namespace-fs-mount-and-mcp-transfer（桌面挂载 =
// rclone mount，WinFsp 硬依赖；NSIS 预带刀，2026-10-18 用户拍板"预安装放到
// 安装包里"）。版本钉选与升级策略：deploy/multi_device/README.md §版本钉选。
//
// 规则（与 scripts/build_cdylibs.mjs 链首模式同形，fail-loud 禁静默降级）：
//   - electron/prereqs/ 实物在位且过大小下限 → 跳过下载（正常路径；备料为
//     大二进制，不入 git，目录整体 gitignore）。
//   - 缺失 → 自动直连下载补齐（禁代理口径：fetch 不走 env 代理，与维护轮
//     `curl --noproxy '*'` 同效；rclone 需 zip 解包，走 PowerShell
//     Expand-Archive——打包链本就 Windows-only）。
//   - 下载失败 / 大小异常（低于下限=失败页或截断）→ 删除残件、非零退出并给
//     换源指引，打包链在此止步。
//   - 单源钉选：msi 文件名含版本号，electron/nsis/installer-custom.nsh 的
//     ExecWait 引用必须与本文件钉选逐字一致——此处机械断言，防两处漂移。
import { spawnSync } from "node:child_process";
import { createWriteStream, existsSync, readdirSync, renameSync, statSync } from "node:fs";
import { mkdir, readFile, rm } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PREREQS_DIR = join(REPO_ROOT, "electron", "prereqs");
const NSH_PATH = join(REPO_ROOT, "electron", "nsis", "installer-custom.nsh");

// 版本钉面（唯一单源）。升级 = 改此处 + nsh 引用联动（下方断言拦截漏改）。
export const PREREQS = [
  {
    file: "winfsp-2.1.25156.msi",
    url: "https://github.com/winfsp/winfsp/releases/download/v2.1/winfsp-2.1.25156.msi",
    // 官方 msi 实测 2,191,360 B（~2.1MB）；下限只拦截失败页/截断件
    minBytes: 1024 * 1024,
    source: "WinFsp v2.1 stable（2025-06-05；GitHub 最新非 prerelease，2.2 线尚在 Beta）",
  },
  {
    file: "rclone.exe",
    url: "https://downloads.rclone.org/v1.75.2/rclone-v1.75.2-windows-amd64.zip",
    zipMember: "rclone-v1.75.2-windows-amd64/rclone.exe",
    // v1.75.2 实测 85,500,928 B（~85MB）；下限只拦截失败页/截断件
    minBytes: 10 * 1024 * 1024,
    source: "rclone v1.75.2 windows-amd64（multi_device README §1 钉选）",
  },
  {
    file: "uv.exe",
    // 随包 uv 引导（2026-10-26 用户裁定「安装上就能用」）：外部净装机无 uv 则
    // 内核 autoprovision 全线瘫痪（venv_provision 裸 PATH 拉 uv）。版本与仓内
    // venv 构建版对齐（pyvenv.cfg uv=0.11.14）。zip 内 uv.exe/uvx.exe 平铺根目录。
    url: "https://github.com/astral-sh/uv/releases/download/0.11.14/uv-x86_64-pc-windows-msvc.zip",
    zipMember: "uv.exe",
    // v0.11.14 实测 67,823,616 B（~67MB）；下限只拦截失败页/截断件
    minBytes: 10 * 1024 * 1024,
    source: "uv v0.11.14 x86_64-pc-windows-msvc（astral-sh/uv releases，仓内 venv 构建版对齐）",
  },
];

async function downloadTo(url, destPath) {
  const res = await fetch(url); // 直连，不走代理（undici 默认不信 env 代理）
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} ${res.statusText}`);
  }
  await pipeline(Readable.fromWeb(res.body), createWriteStream(destPath));
}

// rclone 藏在 zip 内：下载 zip 后解包取成员。走 python zipfile（打包链本就依赖
// python；Expand-Archive 实测过失败页静默+路径形态坑，python 按成员名精确取）。
function extractZipMember(zipPath, memberName, destPath, minBytes) {
  const run = spawnSync(
    "python",
    [
      "-c",
      [
        "import sys, zipfile",
        "zip_path, member, dest, min_bytes = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])",
        "z = zipfile.ZipFile(zip_path)",
        "names = [n for n in z.namelist() if n.replace('\\\\', '/') == member]",
        "assert len(names) == 1, f'member 不唯一或缺失: {names}'",
        "data = z.read(names[0])",
        "assert len(data) >= min_bytes, f'成员过小: {len(data)}B'",
        "open(dest, 'wb').write(data)",
      ].join("; "),
      zipPath,
      memberName,
      destPath,
      String(minBytes),
    ],
    { stdio: "inherit" },
  );
  if (run.error || run.status !== 0) {
    throw new Error(`zip 解包失败（exit ${run.status ?? "spawn-error"}）`);
  }
}

async function main() {
  // 单源断言：nsh 引用的 msi 文件名必须与本钉面一致（版本 bump 漏改 nsh 在此拦截）。
  const nsh = await readFile(NSH_PATH, "utf8");
  const msiFile = PREREQS[0].file;
  if (!nsh.includes(msiFile)) {
    console.error(
      `[check_prereqs] FAIL ${NSH_PATH} 未引用钉选的 ${msiFile}——版本 bump 后两处必须联动`,
    );
    process.exit(1);
  }

  await mkdir(PREREQS_DIR, { recursive: true });
  let fetched = 0;
  for (const item of PREREQS) {
    const dest = join(PREREQS_DIR, item.file);
    const present = existsSync(dest) && statSync(dest).size >= item.minBytes;
    if (!present) {
      console.log(`[check_prereqs] ${item.file} 缺失或过小，下载补齐：${item.url}`);
      const tmp = dest + ".download";
      try {
        await downloadTo(item.url, tmp);
        if (item.zipMember) {
          await extractZipMember(tmp, item.zipMember, dest, item.minBytes);
          await rm(tmp, { force: true });
        } else {
          await rm(dest, { force: true });
          renameSync(tmp, dest);
        }
      } catch (err) {
        await rm(tmp, { force: true }).catch(() => {});
        await rm(dest, { force: true }).catch(() => {});
        console.error(`[check_prereqs] FAIL ${item.file} 补齐失败：${err.message}`);
        console.error(`  来源：${item.source}`);
        console.error(
          "  处置：重试或换源/手动放置实物到 electron/prereqs/（大小下限 " +
            `${item.minBytes}B）后重跑打包链`,
        );
        process.exit(1);
      }
      fetched += 1;
    }
    const size = statSync(dest).size;
    if (size < item.minBytes) {
      console.error(`[check_prereqs] FAIL ${item.file} 大小异常（${size}B < 下限）——拒绝打包`);
      process.exit(1);
    }
    console.log(`[check_prereqs] ${item.file} 在位（${size}B）`);
  }
  // ── 离线 venv 供给负载（2026-10-26 用户裁定「都放进去/离线也能用」）────
  // 组装器 scripts/assemble_offline_payload.py 的就地产物（python 整树 +
  // wheel 库，gitignored；无静态下载源故不自动补，缺了给组装指引硬拦）。
  // uv 管理 python 布局：python.exe 是小启动器（~91KB），真身=python312.dll
  // （~6.7MB）+ Lib/——判据用 DLL 大小，不拍 exe 体积。
  const pythonDll = join(PREREQS_DIR, "python", "python312.dll");
  if (!existsSync(pythonDll) || statSync(pythonDll).size < 4 * 1024 * 1024) {
    console.error("[check_prereqs] FAIL 离线负载缺 python/（python312.dll 缺失或过小）");
    console.error("  处置：python scripts/assemble_offline_payload.py 后重跑打包链");
    process.exit(1);
  }
  const wheelsDir = join(PREREQS_DIR, "wheels");
  let wheelCount = 0;
  if (existsSync(wheelsDir)) {
    wheelCount = readdirSync(wheelsDir).filter((f) => f.endsWith(".whl")).length;
  }
  if (wheelCount < 10) {
    console.error(`[check_prereqs] FAIL 离线负载缺 wheels/（现 ${wheelCount} 个 whl，<10）`);
    console.error("  处置：python scripts/assemble_offline_payload.py --skip-python 后重跑打包链");
    process.exit(1);
  }
  console.log(`[check_prereqs] 离线负载在位：python/ + wheels/（${wheelCount} whl）`);
  console.log(`[check_prereqs] 完成：新取 ${fetched} 件、现位 ${PREREQS.length - fetched} 件`);
}

main();
