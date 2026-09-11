/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * WS 事件名单一真值源契约测试（解耦方案 P2-3）
 *
 * 前端 WS_SERVER_EVENTS 是前端侧事件名单一真值；本测试把它与内核/插件
 * 源码中的事件名字面量对账（内核事件名清单的生产码导出属内核改动，本仓
 * 阶段不动内核生产码，故以源码扫描对账代替——留待办：内核导出事件名
 * 清单 → 生成物投影 + 漂移闸，见解耦方案 P2-3）。
 *
 * 锁两件事：
 * 1. 幽灵事件不上名单：WS_SERVER_EVENTS 不得含已知无发射源的事件名
 *    （订阅无发射源的事件名是死代码）；
 * 2. 真值对账：名单内每个事件名（显式豁免除外）必须在内核/插件源码中
 *    有同名字面量发射点——内核改名/删除事件时本测试变红。
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { WS_SERVER_EVENTS } from '@/constants/websocket'

const REPO_ROOT = resolve(__dirname, '../../../..')

/** 扫描根：内核 api/session 事件族 + 插件 event-bus.emit 全集 */
const SCAN_ROOTS = [
  'kernel/crates/api/src',
  'kernel/crates/session/src',
  'plugins/shared/system',
  'plugins/shared/pipeline',
  'plugins/shared/tools',
]

/** 目录名黑名单（重依赖/非源码目录不可进） */
const EXCLUDED_DIRS = new Set([
  '__pycache__',
  'node_modules',
  'runtime',
  'tests',
  '__tests__',
  'dist',
])

/** 单文件读取上限（防超大产物拖垮扫描） */
const MAX_FILE_BYTES = 1_000_000

/** 收集扫描范围内的 .py/.rs 源码文本（一次性，供字面量包含断言） */
function collectBackendSourceText(): string {
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
      if (!name.endsWith('.py') && !name.endsWith('.rs')) continue
      if (stat.size > MAX_FILE_BYTES) continue
      try {
        chunks.push(readFileSync(full, 'utf-8'))
      } catch {
        // 单文件不可读跳过（Windows 文件锁等），不影响整体对账
      }
    }
  }
  for (const root of SCAN_ROOTS) walk(join(REPO_ROOT, root))
  return chunks.join('\n')
}

/**
 * 发射源豁免名单（显式 + 理由；新增豁免须在此说明升级触发条件）：
 * - task_status_update / task_status_changed：后端 tasks/service.py 推送路径
 *   当前静默跳过，待 SDK frontend.emit capability 落地后恢复（前端订阅保留）。
 */
const EMITTER_EXEMPTIONS = new Set(['task_status_update', 'task_status_changed'])

/** 已知幽灵词汇：后端（kernel 事件族 + 插件 event-bus.emit）均无发射源 */
const KNOWN_GHOST_EVENTS = [
  'state_change',
  'schema_updated',
  'execution_start',
  'execution_progress',
  'execution_done',
  'execution_cancelled',
  'execution_output',
  'sub_agent_created',
  'sub_agent_waiting_input',
  'sub_agent_completed',
  'agent_level_changed',
  'session_update',
  'pipeline_received',
  'approval_request',
  'node_status_update',
  'execution_status_update',
  'execution_event',
  'workflow_step_update',
  'execution_control_response',
  'agent_inject_response',
]

describe('WS 事件名单一真值源对账（P2-3）', () => {
  it('订阅型事件常量在名单内（裸字符串订阅的四个事件名全部收口）', () => {
    // pending_inputs_changed / compression_failed = 内核/插件真实发射的后端事件
    expect(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED).toBe('pending_inputs_changed')
    expect(WS_SERVER_EVENTS.COMPRESSION_FAILED).toBe('compression_failed')
  })

  it('幽灵事件零残留：已知无发射源词汇不得回流事件表', () => {
    const values = Object.values(WS_SERVER_EVENTS)
    for (const ghost of KNOWN_GHOST_EVENTS) {
      expect(values).not.toContain(ghost)
    }
  })

  it('名单内每个事件名在内核/插件源码中有发射点（豁免名单除外）', () => {
    const source = collectBackendSourceText()
    expect(source.length).toBeGreaterThan(0)
    const missing: string[] = []
    for (const value of Object.values(WS_SERVER_EVENTS)) {
      if (EMITTER_EXEMPTIONS.has(value)) continue
      if (!source.includes(`"${value}"`) && !source.includes(`'${value}'`)) {
        missing.push(value)
      }
    }
    expect(missing).toEqual([])
  }, 30_000)
})
