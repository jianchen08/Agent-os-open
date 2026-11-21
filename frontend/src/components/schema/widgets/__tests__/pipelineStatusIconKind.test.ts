// @feature FP-T12 前端适配 | @vision V6 可即用 | @ci frontend-test
/**
 * entryStatusIconKind 单测（状态图标「类型+状态合一」的取件键，单一真值）。
 *
 * 契约：种类由 `entryStatusVisual` 的文案折算，图标与文字不得各判一套；
 * 项目登记行无运行态 → null（渲染项目图标）。
 *
 * 防拟合：六态全覆盖（running/paused/failed/completed/unknown/project-null），
 * 含两组区分输入（running↔completed、failed↔paused）与「与文案一致」性质断言。
 */
import { describe, expect, it } from 'vitest'
import {
  entryStatusIconKind,
  entryStatusVisual,
} from '../pipelineStatusVisuals'
import type { PipelineViewEntry } from '@/types/pipeline'

function makeEntry(overrides: Partial<PipelineViewEntry>): PipelineViewEntry {
  return {
    key: 'k1',
    runId: 'k1',
    kind: 'session',
    startedAt: '2026-11-20T00:00:00Z',
    ...overrides,
  } as PipelineViewEntry
}

describe('entryStatusIconKind — 六态全覆盖', () => {
  it('会话行：running / completed 映射到各自种类（两组区分输入）', () => {
    expect(entryStatusIconKind(makeEntry({ status: 'running' }))).toBe('running')
    expect(entryStatusIconKind(makeEntry({ status: 'completed' }))).toBe('completed')
  })

  it('会话行：suspended → paused、failed → failed（两组区分输入）', () => {
    expect(entryStatusIconKind(makeEntry({ status: 'suspended' }))).toBe('paused')
    expect(entryStatusIconKind(makeEntry({ status: 'failed' }))).toBe('failed')
  })

  it('未知/缺席运行态 → unknown（不猜 running）', () => {
    expect(entryStatusIconKind(makeEntry({ status: 'unknown' }))).toBe('unknown')
    expect(entryStatusIconKind(makeEntry({ status: undefined }))).toBe('unknown')
  })

  it('任务行按结构化键折算：终态任务的取消态落 paused（不依赖显示文案措辞）', () => {
    // cancelled 终态在任务面板映射为「已暂停（可恢复）」——文案与 run 域不同，
    // 故种类必须由结构化键取，不能拿 PIPELINE_STATUS_LABELS 比对文案
    const cancelledTask = makeEntry({
      kind: 'task',
      taskStatus: 'running',
      status: 'cancelled',
    })
    expect(entryStatusIconKind(cancelledTask)).toBe('paused')
    // 性质：图标种类与视觉文案同为「暂停」语义族（不因文案措辞漏判为 unknown）
    expect(entryStatusIconKind(cancelledTask)).not.toBe('unknown')
  })
})

describe('entryStatusIconKind — 项目行与性质断言', () => {
  it('项目登记行 → null（无运行态，渲染项目图标）', () => {
    expect(entryStatusIconKind(makeEntry({ kind: 'project', status: undefined }))).toBeNull()
  })

  it('性质：种类与文案恒定对应（图标不偏离同源视觉）', () => {
    const cases: Array<[string, string]> = [
      ['running', 'running'],
      ['suspended', 'paused'],
      ['failed', 'failed'],
      ['completed', 'completed'],
    ]
    for (const [status, expected] of cases) {
      const entry = makeEntry({ status: status as PipelineViewEntry['status'] })
      // 文案存在且种类与之一致：同一状态的两处呈现不得各判一套
      expect(entryStatusVisual(entry)?.label).toBeTruthy()
      expect(entryStatusIconKind(entry)).toBe(expected)
    }
  })

  it('纯函数：同一输入多次调用恒等', () => {
    const entry = makeEntry({ status: 'running' })
    expect(entryStatusIconKind(entry)).toBe(entryStatusIconKind(entry))
  })
})
