/** @feature FP-0.2.四 前端主题 | @ci: frontend-test */
/**
 * 区域 muted 桥契约门禁（index.css 静态文本）
 *
 * 区域桥把 --muted-foreground / --secondary-foreground 重绑定为
 * var(--region-x-muted-fg, <根值>)。fallback 若直接引用被声明变量自身，
 * region 变量未声明时走 fallback 分支 → 自引用成环 → guaranteed-invalid
 * （region 子树 muted 类恒退化继承色，跨端真缺陷，R8 修复）。本门禁锁：
 * 1) 桥的 fallback 必须经 :root 中间层（--muted-foreground-root 等），
 *    且 fallback 不得等于被声明变量自身（通用自引用检测）；
 * 2) 中间层必须在 :root 声明且引用根级变量（主题切换同步）。
 * CSS 运行时行为无单测引擎，静态文本契约 + 浏览器活体取证兜底。
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const indexCss = readFileSync(resolve(process.cwd(), 'src', 'index.css'), 'utf-8')

/** 三处区域桥（muted + secondary-foreground 共 6 条声明）的声明行 */
const BRIDGE_LINES = indexCss
  .split('\n')
  .filter((line) => /--(muted|secondary)-foreground:\s*var\(--region-(sidebar|chat|workspace)-muted-fg/.test(line))

describe('区域 muted 桥契约（index.css）', () => {
  it('三区域 × muted/secondary-foreground 共 6 条桥声明在场', () => {
    expect(BRIDGE_LINES).toHaveLength(6)
  })

  it('fallback 不得引用被声明变量自身（自引用 → guaranteed-invalid 回归锁）', () => {
    for (const line of BRIDGE_LINES) {
      const decl = line.match(/--([\w-]+):\s*var\(--region-[\w-]+-muted-fg,\s*var\((--[\w-]+)\)\)/)
      expect(decl, `桥声明解析失败: ${line.trim()}`).toBeTruthy()
      const [, declared, fallback] = decl!
      expect(fallback, `自引用 fallback 回归（${declared}）`).not.toBe(`--${declared}`)
    }
  })

  it('fallback 统一经 :root 中间层（子树内重绑不影响已求值的根值）', () => {
    for (const line of BRIDGE_LINES) {
      const [, declared, fallback] = line.match(/--([\w-]+):\s*var\(--region-[\w-]+-muted-fg,\s*var\((--[\w-]+)\)\)/)!
      expect(fallback).toBe(`--${declared}-root`)
    }
  })

  it('中间层在 :root 声明且引用根级变量（主题切换同步）', () => {
    const rootBlock = indexCss.match(/:root\s*\{[^}]*--muted-foreground-root[^}]*\}/)
    expect(rootBlock, ':root 缺 --muted-foreground-root 中间层').toBeTruthy()
    expect(rootBlock![0]).toContain('--muted-foreground-root: var(--muted-foreground)')
    expect(rootBlock![0]).toContain('--secondary-foreground-root: var(--secondary-foreground)')
  })
})
