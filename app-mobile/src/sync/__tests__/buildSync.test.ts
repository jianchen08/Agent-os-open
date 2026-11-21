// @vitest-environment jsdom
/**
 * build-sync 漂移闸契约测试：现产 ≡ 源构建为绿；删值实验（改产物一字节）红。
 * 对账走 LINGXI_SYNC_OUT 临时副本，不动真树（syncweb --check 闸同款执法形）。
 * 另含在库产物冒烟：IIFE 在 jsdom 可执行、暴露编程入口、重复注入幂等。
 */
import { execFile } from 'node:child_process'
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, describe, expect, it } from 'vitest'

const syncRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
const scriptPath = join(syncRoot, 'scripts', 'build-sync.mjs')
const bundledAsset = join(syncRoot, 'android', 'app', 'src', 'main', 'assets', 'shell-sync.js')

function runCheck(outPath: string) {
  return new Promise<{ status: number; stderr: string; stdout: string }>((resolveRun) => {
    execFile(
      process.execPath,
      [scriptPath, '--check'],
      { env: { ...process.env, LINGXI_SYNC_OUT: outPath }, encoding: 'utf8' },
      (_err, stdout, stderr) => {
        // 红路径以 exit 1 终止（execFile 的 err 即非零退出），状态以 close code 为准
        resolveRun({ status: _err && typeof _err.code === 'number' ? _err.code : 0, stderr, stdout })
      },
    )
  })
}

let scratch: string | null = null

afterEach(() => {
  if (scratch) {
    rmSync(scratch, { recursive: true, force: true })
    scratch = null
  }
})

function newScratch(): string {
  scratch = mkdtempSync(join(tmpdir(), 'build-sync-check-'))
  return scratch
}

describe('build-sync 漂移闸', () => {
  it('在库产物 ≡ 源构建（绿；同时锁定在库产物存在且由本链生成）', async () => {
    const dir = newScratch()
    const outPath = join(dir, 'shell-sync.js')
    copyFileSync(bundledAsset, outPath)
    const result = await runCheck(outPath)
    expect(result.status).toBe(0)
    expect(result.stdout).toContain('绿')
  })

  it('删值实验：产物与源漂移一字节即红（红路径显式 exit 1）', async () => {
    const dir = newScratch()
    const outPath = join(dir, 'shell-sync.js')
    copyFileSync(bundledAsset, outPath)
    writeFileSync(outPath, readFileSync(outPath, 'utf8').replace('scripts/build-sync.mjs', 'scripts/build-sync.mJ'))
    const result = await runCheck(outPath)
    expect(result.status).toBe(1)
    expect(result.stderr).toContain('漂移')
  })

  it('产物缺失红（重建指引路径）', async () => {
    const dir = newScratch()
    const result = await runCheck(join(dir, 'absent.js'))
    expect(result.status).toBe(1)
    expect(result.stderr).toContain('缺失')
  })

  it('在库产物冒烟：IIFE 于 jsdom 可执行、暴露 LingxiSync、无浮窗、重复注入幂等', () => {
    const bundle = readFileSync(bundledAsset, 'utf8')
    // jsdom 的 localStorage 受 opaque origin 限制，探测写入失败会触发模块停用路径
    // （fail-closed 设计）；本冒烟锁主路径，先换装内存存储再注入。
    const backing = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => (backing.has(key) ? backing.get(key)! : null),
        setItem: (key: string, value: string) => void backing.set(key, String(value)),
        removeItem: (key: string) => void backing.delete(key),
        key: (index: number) => [...backing.keys()][index] ?? null,
        get length() {
          return backing.size
        },
        clear: () => void backing.clear(),
      },
    })
    new Function(bundle)()
    const api = (window as unknown as { LingxiSync?: { engine: unknown } }).LingxiSync
    expect(api).toBeDefined()
    expect(api?.engine).toBeDefined()
    // 壳侧零 UI（浮动状态面板已移除）：状态面唯一可见面在共享前端设置中枢。
    expect(document.querySelectorAll('#lingxi-sync-chip')).toHaveLength(0)
    expect(document.querySelectorAll('#lingxi-sync-panel-host')).toHaveLength(0)
    // 幂等护栏：重复注入（MainActivity 每次页面加载都会执行）不重建 API。
    new Function(bundle)()
    const apiAgain = (window as unknown as { LingxiSync?: { engine: unknown } }).LingxiSync
    expect(apiAgain).toBe(api)
  })
})
