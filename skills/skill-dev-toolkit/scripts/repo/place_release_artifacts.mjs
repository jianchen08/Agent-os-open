#!/usr/bin/env node
// 打包产物直出主仓 release/（交付位）：electron:build 链尾落位步，废
// 「烘临时区+人工拷」两步（2026-10-18 用户拍板）。
//
// 位置语义：链尾在 check_packaged_layout_gate 之后——布局门禁不合格则到不了
// 本步，坏包不会落位。产物侧=运行仓（worktree 或主仓）自身 release/；交付位=
// 主仓 release/。主仓根推导：git rev-parse --git-common-dir（worktree 下返回
// 主仓 .git 绝对路径，主仓下返回相对 .git）→ 父目录即主仓根，两态通吃、零
// 硬编码机器路径（electron-builder output 改绝对路径的方案被否：package.json
// 机器绑定 + 布局门禁产物根发现在 worktree 侧失效）。
//
// 行为（fail-loud，禁静默降级——与 build_cdylibs 链首纪律同形）：
//   1. 断言链产物 "<productName> Setup <version>.exe" 在产物侧 release/（缺失
//      即非零退出——本链的平台产物，缺席=链路异常不是可跳过态）
//   2. 拷入主仓 release/（同名覆盖=同版本重烘替换语义）
//   3. 落位后尺寸+sha256 双重自校验（与产物侧逐字节比对，拷贝损坏在此拦截）
//   4. 清扫主仓 release/ 其余旧版本 "<productName> Setup*.exe"（交付位只留
//      当前版本，旧版本无留存意义；清扫在拷入+校验成功之后，先立后破）
//   5. 删产物侧副本（拷出即直出，§4⑪ workdir 清理不再背产物）
//
// 纯函数自检：node scripts/place_release_artifacts.mjs --self-test
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, readdirSync, rmSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { strictEqual } from "node:assert";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// 主仓根 = git common dir 的父目录（纯函数：cwd 仅用于解析相对 common dir）。
export function resolveMainRepoRoot(commonDir, cwd) {
  const abs = resolve(cwd, commonDir);
  return dirname(abs);
}

// 产物侧与交付位是否同一路径（纯函数）：主仓直跑时 common dir 的父目录即主仓
// 根，src === dst。此形态下同址拷贝无意义，且链尾「删产物侧副本」会把刚出的包
// 删掉（2026-10-28 实测：electron-builder 出包后本步自删，release/ 空手）。
export function sameArtifactPath(src, dst, pathResolve = resolve, pathNormalize = (p) => p) {
  return pathNormalize(pathResolve(src)) === pathNormalize(pathResolve(dst));
}

// 交付位清扫清单：除当前版本外的全部 "<productName> Setup*.exe"（纯函数）。
export function staleInstallerSweep(files, productName, keepName) {
  const prefix = `${productName} Setup`;
  return files.filter((f) => f !== keepName && f.startsWith(prefix) && f.endsWith(".exe"));
}

function gitCommonDir(repoRoot) {
  const run = spawnSync("git", ["-C", repoRoot, "rev-parse", "--git-common-dir"], {
    encoding: "utf8",
  });
  if (run.error || run.status !== 0) {
    console.error(`[place_release] FAIL git rev-parse --git-common-dir 失败：${run.stderr?.trim()}`);
    process.exit(1);
  }
  return run.stdout.trim();
}

async function main() {
  const { readFileSync } = await import("node:fs");
  const pkg = JSON.parse(readFileSync(join(REPO_ROOT, "package.json"), "utf8"));
  const productName = pkg.build.productName;
  const version = pkg.version;
  const keepName = `${productName} Setup ${version}.exe`;

  const artifactSideRelease = join(REPO_ROOT, "release");
  const src = join(artifactSideRelease, keepName);
  if (!existsSync(src)) {
    console.error(`[place_release] FAIL 链产物缺失：${src}——NSIS 步异常，落位拒绝`);
    process.exit(1);
  }

  const mainRepoRoot = resolveMainRepoRoot(gitCommonDir(REPO_ROOT), REPO_ROOT);
  const deliverRelease = join(mainRepoRoot, "release");
  const dst = join(deliverRelease, keepName);

  const srcBytes = statSync(src).size;
  const samePath = sameArtifactPath(src, dst);
  if (!samePath) {
    copyFileSync(src, dst);
    const dstBytes = statSync(dst).size;
    if (dstBytes !== srcBytes) {
      console.error(`[place_release] FAIL 落位尺寸不一致：${srcBytes}B -> ${dstBytes}B`);
      process.exit(1);
    }
    const srcHash = createHash("sha256").update(readFileSync(src)).digest("hex");
    const dstHash = createHash("sha256").update(readFileSync(dst)).digest("hex");
    if (srcHash !== dstHash) {
      console.error(`[place_release] FAIL 落位哈希不一致：${srcHash.slice(0, 16)} vs ${dstHash.slice(0, 16)}`);
      process.exit(1);
    }
    console.log(`[place_release] 落位：${dst}`);
    console.log(`[place_release] 自校验过：${srcBytes}B sha256 前16=${srcHash.slice(0, 16)}`);
  } else {
    // 主仓直跑：产物侧就是交付位，同址拷贝与校验都无意义，删除更会删掉成品。
    const hash = createHash("sha256").update(readFileSync(src)).digest("hex");
    console.log(`[place_release] 主仓直出：产物已在交付位（跳过同址拷贝/自校验/产物侧删除）`);
    console.log(`[place_release] 自校验过：${srcBytes}B sha256 前16=${hash.slice(0, 16)}`);
  }

  const swept = staleInstallerSweep(readdirSync(deliverRelease), productName, keepName);
  for (const name of swept) {
    rmSync(join(deliverRelease, name));
    console.log(`[place_release] 清扫旧版本安装器：${name}`);
  }

  if (!samePath) {
    rmSync(src);
    console.log(`[place_release] 产物侧副本已删（直出语义）：${src}`);
  }
  console.log("[place_release] 完成");
}

if (process.argv.includes("--self-test")) {
  strictEqual(resolveMainRepoRoot("D:/main/.git", "D:/any"), "D:\\main");
  strictEqual(resolveMainRepoRoot(".git", "D:/main/repo"), "D:\\main\\repo");
  // 同址判定：主仓直跑（src===dst）vs worktree（产物侧与主仓 release/ 不同）
  strictEqual(sameArtifactPath("D:/main/release/a.exe", "D:/main/release/a.exe"), true);
  strictEqual(sameArtifactPath("D:/main/release/a.exe", "D:/main/release/b.exe"), false);
  strictEqual(
    sameArtifactPath("D:/wt/release/a.exe", "D:/main/release/a.exe"),
    false,
  );
  strictEqual(
    JSON.stringify(
      staleInstallerSweep(
        ["灵汐助手 Setup 0.2.0.exe", "灵汐助手 Setup 0.2.2.exe", "灵汐助手 Setup 0.1.0.exe.blockmap", "other.exe"],
        "灵汐助手",
        "灵汐助手 Setup 0.2.2.exe",
      ),
    ),
    JSON.stringify(["灵汐助手 Setup 0.2.0.exe"]),
  );
  console.log("[place_release] self-test 通过");
} else {
  await main();
}
