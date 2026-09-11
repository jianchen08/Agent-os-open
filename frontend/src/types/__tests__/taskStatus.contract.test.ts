/** @ci: frontend-test */
/**
 * 任务状态词表契约测试（总纲 #12）
 *
 * 单一真值源 = types/taskStatus（tasks 插件 TaskStatus 七态）。本测试把前端
 * 词表与内核/插件写面状态字面量对账（源码扫描对账，同 wsEventNames.contract）：
 * 1. 词表对账：TASK_STATUSES 必须与 task_types.py TaskStatus enum 逐一相等；
 * 2. 写面收口：插件源码中所有 `task.status` 字面量写值必须落在词表（七态）内；
 * 3. 别名证据：TASK_STATUS_ALIASES 每个旧值在 tasks 插件源码中必须有读面/文档
 *    证据（无证据词不得回流词表）；
 * 4. 死词零残留：planning/blocked/deleted 等无写面词汇不得出现在词表派生面。
 *
 * 行为测试锁：未知状态归一为 'unknown'（绝不猜 running/completed），同一未知值
 * 只 console.warn 一次。
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  TASK_STATUSES,
  TASK_STATUS_ALIASES,
  TASK_STATUS_LABELS,
  normalizeTaskStatus,
  taskStatusLabel,
  taskStatusToPipelineStatus,
  taskStatusToAgentTabStatus,
} from '../taskStatus'

const REPO_ROOT = resolve(__dirname, '../../../..')

/** 目录名黑名单（重依赖/非源码目录不可进） */
const EXCLUDED_DIRS = new Set(['__pycache__', 'node_modules', 'runtime', 'tests', '__tests__', 'dist'])

/** 单文件读取上限（防超大产物拖垮扫描） */
const MAX_FILE_BYTES = 1_000_000

/** 收集扫描范围内 .py 源码文本 */
function collectSourceText(root: string): string {
  const chunks: string[] = []
  const walk = (dir: string): void => {
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      return
    }
    for (const name of entries) {
      const full = join(dir, name)
      let stat
      try {
        stat = statSync(full)
      } catch {
        continue
      }
      if (stat.isDirectory()) {
        if (!EXCLUDED_DIRS.has(name) && !name.startsWith('.')) walk(full)
        continue
      }
      if (!name.endsWith('.py')) continue
      // 测试文件（插件目录内平铺的 test_*.py 等）不算生产写面
      if (name.startsWith('test_') || name.endsWith('_test.py') || name === 'conftest.py') continue
      if (stat.size > MAX_FILE_BYTES) continue
      try {
        chunks.push(readFileSync(full, 'utf-8'))
      } catch {
        // 单文件不可读跳过（Windows 文件锁等），不影响整体对账
      }
    }
  }
  walk(join(REPO_ROOT, root))
  return chunks.join('\n')
}

describe('任务状态词表 ↔ 后端写面对账（契约）', () => {
  it('TASK_STATUSES 与 task_types.py TaskStatus enum 逐一相等', () => {
    const source = readFileSync(
      join(REPO_ROOT, 'plugins/shared/system/tasks/task_types.py'),
      'utf-8',
    )
    const block = source.slice(
      source.indexOf('class TaskStatus'),
      source.indexOf('@dataclass', source.indexOf('class TaskStatus')),
    )
    const enumValues = [...block.matchAll(/=\s*"(\w+)"/g)].map((m) => m[1])
    expect(enumValues.length).toBeGreaterThan(0)
    expect([...TASK_STATUSES].sort()).toEqual([...new Set(enumValues)].sort())
  })

  it('插件写面所有 task.status 字面量值都落在七态词表内', () => {
    const source = collectSourceText('plugins/shared')
    expect(source.length).toBeGreaterThan(0)
    const written = new Set<string>()
    for (const m of source.matchAll(/"task\.status"\s*:\s*"(\w+)"/g)) {
      written.add(m[1])
    }
    expect(written.size).toBeGreaterThan(0)
    const outside = [...written].filter((v) => !(TASK_STATUSES as readonly string[]).includes(v))
    expect(outside).toEqual([])
  })

  it('别名旧值在 tasks 插件源码中有读面/文档证据', () => {
    const source = collectSourceText('plugins/shared/system/tasks')
    for (const alias of Object.keys(TASK_STATUS_ALIASES)) {
      expect(source.includes(`"${alias}"`) || source.includes(`'${alias}'`)).toBe(true)
    }
    // pending_evaluation：评估未决读面值（reconcile 未决集）
    expect(source.includes('pending_evaluation')).toBe(true)
  })

  it('死词零残留：无写面词汇不得回流标签词表', () => {
    const labelKeys = Object.keys(TASK_STATUS_LABELS)
    for (const dead of ['planning', 'blocked', 'deleted', 'canceled']) {
      expect(labelKeys).not.toContain(dead)
    }
    // 标签键集合 = 七态 ∪ 别名 ∪ pending_evaluation，不得有其他派生键
    const allowed = new Set([
      ...TASK_STATUSES,
      ...Object.keys(TASK_STATUS_ALIASES),
      'pending_evaluation',
    ])
    for (const key of labelKeys) {
      expect(allowed.has(key)).toBe(true)
    }
  })
})

describe('任务状态归一行为（未知 ≠ running/completed）', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('未知状态归一为 unknown，不猜 running/completed', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(normalizeTaskStatus('mystery_state')).toBe('unknown')
    expect(taskStatusToPipelineStatus('mystery_state')).toBe('unknown')
    expect(taskStatusToPipelineStatus('planning')).toBe('unknown')
    expect(taskStatusToAgentTabStatus('mystery_state')).toBe('unknown')
    expect(warn).toHaveBeenCalled()
  })

  it('同一未知值只警告一次', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    normalizeTaskStatus('one_shot_dead_word')
    normalizeTaskStatus('one_shot_dead_word')
    normalizeTaskStatus('one_shot_dead_word')
    const calls = warn.mock.calls.filter((args) => String(args[0]).includes('one_shot_dead_word'))
    expect(calls).toHaveLength(1)
  })

  it('七态原样透传，别名折叠到七态且不告警', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    for (const s of TASK_STATUSES) {
      expect(normalizeTaskStatus(s)).toBe(s)
    }
    expect(normalizeTaskStatus('suspended')).toBe('stopped')
    expect(normalizeTaskStatus('paused')).toBe('stopped')
    expect(normalizeTaskStatus('cancelled')).toBe('stopped')
    expect(warn).not.toHaveBeenCalled()
  })

  it('未知状态文案回退原串，不伪装成合法态文案', () => {
    expect(taskStatusLabel('weird_value')).toBe('weird_value')
    expect(taskStatusLabel('completed')).toBe('已完成')
    expect(taskStatusLabel('timeout')).toBe('已超时')
  })

  it('视图映射：七态 → 管道运行态 / Agent 标签页态（语义投影）', () => {
    expect(taskStatusToPipelineStatus('running')).toBe('running')
    expect(taskStatusToPipelineStatus('stopped')).toBe('suspended')
    expect(taskStatusToPipelineStatus('timeout')).toBe('failed')
    expect(taskStatusToPipelineStatus('completed')).toBe('completed')

    expect(taskStatusToAgentTabStatus('evaluating')).toBe('running')
    expect(taskStatusToAgentTabStatus('stopped')).toBe('waiting_input')
    expect(taskStatusToAgentTabStatus('failed')).toBe('failed')
    expect(taskStatusToAgentTabStatus('timeout')).toBe('failed')
    expect(taskStatusToAgentTabStatus('completed')).toBe('completed')
  })
})
