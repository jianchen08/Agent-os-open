#!/usr/bin/env node
/**
 * build-sync —— 壳层同步模块构建链：src/sync/*.ts → esbuild IIFE →
 * android/app/src/main/assets/shell-sync.js（MainActivity 页面加载完成后注入，
 * shell-push.js 同款机制）。
 *
 * 产物入库（shell-push.js 注入资产同律）：deploy/build_mobile.sh 现役 APK 链
 * 零改动可打包；漂移由 `--check` 闸执法（现产与在库逐字节对账，红=改源后未重建）。
 * 用法：npm run build:sync [-- --check]
 */
import { build } from 'esbuild'
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const syncRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
// LINGXI_SYNC_OUT 覆盖产物路径（漂移闸测试喂临时副本对账，不动真树）；
// 缺省 = 注入资产位（shell-push.js 同目录）。
const outFile = process.env.LINGXI_SYNC_OUT
  ? resolve(process.env.LINGXI_SYNC_OUT)
  : join(syncRoot, 'android', 'app', 'src', 'main', 'assets', 'shell-sync.js')

const BANNER = `/*
 * 由 scripts/build-sync.mjs 生成（源单点 src/sync/），勿手改；
 * MainActivity 页面加载完成后注入，shell-push.js 同款机制。
 */`

function render() {
  return build({
    entryPoints: [join(syncRoot, 'src', 'sync', 'index.ts')],
    bundle: true,
    format: 'iife',
    target: 'es2020',
    minify: false,
    legalComments: 'none',
    banner: { js: BANNER },
    outfile: outFile,
    logLevel: 'silent',
    write: false,
  }).then((result) => result.outputFiles[0].text)
}

const rendered = await render()

if (process.argv.slice(2).includes('--check')) {
  let current = null
  try {
    current = readFileSync(outFile, 'utf8')
  } catch {
    console.error('[build-sync --check] 红：在库产物缺失——跑 npm run build:sync 重建')
    process.exit(1)
  }
  if (current !== rendered) {
    console.error('[build-sync --check] 红：在库产物与源（src/sync/）漂移——跑 npm run build:sync 重建后一并提交')
    process.exit(1)
  }
  console.log('[build-sync --check] 绿：在库产物 ≡ 源构建')
  process.exit(0)
}

writeFileSync(outFile, rendered)
console.log(`[build-sync] 生成 ${outFile}（${rendered.length} bytes）`)
