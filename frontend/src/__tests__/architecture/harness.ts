/**
 * 架构契约测试 harness —— 基于源码扫描的 rules-as-tests 工具
 *
 * 用途：把"架构不变量"（如某 API 必须有消费者、某禁令模式不得出现）表达为
 * CI 失败级测试。lint 只能 warn 拦新增；本 harness 让不变量成为硬约束。
 *
 * 关联：frontend-design-unification-execution-plan.md §一.2 / §三 M0
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// harness 位于 frontend/src/__tests__/architecture/，向上 3 级到 frontend 根
const HARNESS_DIR = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = path.resolve(HARNESS_DIR, '..', '..', '..')

/** 把工程相对路径解析为绝对路径 */
function resolveSrc(rel: string): string {
  return path.resolve(FRONTEND_ROOT, rel)
}

/** 递归收集目录下所有 .ts/.tsx 文件（工程相对路径） */
export function listSourceFiles(dirRel: string): string[] {
  const abs = resolveSrc(dirRel)
  const out: string[] = []
  const stack: string[] = [abs]
  while (stack.length > 0) {
    const cur = stack.pop()!
    let entries: string[]
    try {
      entries = readdirSync(cur)
    } catch {
      continue
    }
    for (const name of entries) {
      const full = path.join(cur, name)
      const rel = path.relative(FRONTEND_ROOT, full).replace(/\\/g, '/')
      const st = statSync(full)
      if (st.isDirectory()) {
        if (name === 'node_modules' || name === 'dist' || name === '__tests__') continue
        stack.push(full)
      } else if (st.isFile() && /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) {
        out.push(rel)
      }
    }
  }
  return out
}

/** 读取工程相对路径文件内容（文件不存在返回空串） */
export function readSource(rel: string): string {
  try {
    return readFileSync(resolveSrc(rel), 'utf8')
  } catch {
    return ''
  }
}

export interface SourceHit {
  file: string
  line: number
}

/** 在指定文件中扫描文本模式，返回命中（文件 + 行号） */
export function scanSourceForPattern(pattern: string, filesRel: string[]): SourceHit[] {
  const re = new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const hits: SourceHit[] = []
  for (const rel of filesRel) {
    const content = readSource(rel)
    if (!content) continue
    content.split('\n').forEach((text, idx) => {
      if (re.test(text)) hits.push({ file: rel, line: idx + 1 })
    })
  }
  return hits
}

/** 统计目录树下某模式的命中次数 */
export function countPatternInDir(pattern: string, dirRel: string): number {
  const files = listSourceFiles(dirRel)
  return scanSourceForPattern(pattern, files).length
}

/** 正则版扫描（模式本身是正则时使用） */
export function scanSourceForRegex(regex: RegExp, filesRel: string[]): SourceHit[] {
  const hits: SourceHit[] = []
  for (const rel of filesRel) {
    const content = readSource(rel)
    if (!content) continue
    content.split('\n').forEach((text, idx) => {
      if (regex.test(text)) hits.push({ file: rel, line: idx + 1 })
    })
  }
  return hits
}
