/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * 产物 diff 聚合测试（任务现场批 4，纯函数层）。
 *
 * 判据（方案 §七 批 4）：
 * 1. 同一文件写两次 → 只有一行，`+N -M` 取末轮（非累加）；L1 用首末净差
 * 2. 新建（old_content 为空串）→「新增」；修改 →「修改」；delete_file →「删除」
 *    且无内容（如实降级）
 * 3. copy_file / move_file 不产生 diff 条目（无假 diff）
 * 4. 产出行带 producedBy（谁产出）
 */
import { describe, expect, it } from 'vitest'
import {
  buildFileChanges,
  computeFileNetStat,
  summarizeChanges,
} from '@/components/workspace/taskScene/sceneDiff'
import type { SceneActivity } from '@/components/workspace/taskScene/sceneActivities'

function activity(p: Partial<SceneActivity> & { toolName: string }): SceneActivity {
  return {
    callId: `call-${Math.random().toString(36).slice(2, 8)}`,
    args: {},
    state: 'done',
    category: 'file',
    summary: '',
    messageId: 'm1',
    startedAt: '2026-11-16T11:00:00.000Z',
    ...p,
  } as SceneActivity
}

describe('buildFileChanges — 按文件聚合', () => {
  it('同一文件两轮写入归并为一行，增删取末轮（非累加）', () => {
    const changes = buildFileChanges([
      activity({
        toolName: 'file_write',
        args: { path: 'src/a.ts' },
        resultData: { file: 'src/a.ts', old_content: 'old', new_content: 'old\n+1', added: 1, removed: 0, size: 100 },
      }),
      activity({
        toolName: 'file_write',
        args: { path: 'src/a.ts' },
        resultData: {
          file: 'src/a.ts',
          old_content: 'old\n+1',
          new_content: 'old\n+1\n+2\n+3',
          added: 2,
          removed: 0,
          size: 140,
        },
      }),
    ])

    expect(changes).toHaveLength(1)
    const change = changes[0]!
    expect(change.path).toBe('src/a.ts')
    expect(change.rounds).toHaveLength(2)
    // L0 取末轮（2 行），不是累计 3 行
    expect(change.added).toBe(2)
    expect(change.size).toBe(140)
    // 首轮写前 = 文件初始内容
    expect(change.firstOldContent).toBe('old')
    expect(change.lastNewContent).toBe('old\n+1\n+2\n+3')
    expect(change.status).toBe('modified')
    expect(change.producedBy).toBe('file_write')
  })

  it('新建文件（old_content 为空串）→ 新增状态', () => {
    const changes = buildFileChanges([
      activity({
        toolName: 'file_write',
        args: { path: 'docs/new.md' },
        resultData: { file: 'docs/new.md', old_content: '', new_content: 'hello', added: 1, removed: 0 },
      }),
    ])
    expect(changes[0]?.status).toBe('added')
    expect(changes[0]?.firstOldContent).toBe('')
  })

  it('delete_file → 删除且无内容（如实降级，不造假 diff）', () => {
    const changes = buildFileChanges([
      activity({
        toolName: 'delete_file',
        args: { path: 'tmp/x.log' },
        resultData: { results: [{ path: 'tmp/x.log', deleted: true }] },
      }),
    ])
    expect(changes).toHaveLength(1)
    expect(changes[0]?.status).toBe('deleted')
    expect(changes[0]?.lastNewContent).toBeNull()
    expect(changes[0]?.rounds).toHaveLength(0)
  })

  it('delete_file 未成功（deleted=false）不算删除', () => {
    const changes = buildFileChanges([
      activity({
        toolName: 'delete_file',
        args: { path: 'tmp/y.log' },
        resultData: { results: [{ path: 'tmp/y.log', deleted: false, error: '权限不足' }] },
      }),
    ])
    expect(changes).toHaveLength(0)
  })

  it('copy_file / move_file 不产生 diff 条目（无假 diff）', () => {
    const changes = buildFileChanges([
      activity({ toolName: 'copy_file', args: { source: 'a', destination: 'b' }, resultData: { source: 'a', destination: 'b' } }),
      activity({ toolName: 'move_file', args: { source: 'c', destination: 'd' }, resultData: { source: 'c', destination: 'd' } }),
    ])
    expect(changes).toHaveLength(0)
  })

  it('先写后删：状态落删除但保留写入历史', () => {
    const changes = buildFileChanges([
      activity({
        toolName: 'file_write',
        args: { path: 'src/gone.ts' },
        resultData: { file: 'src/gone.ts', old_content: '', new_content: 'x', added: 1 },
      }),
      activity({
        toolName: 'delete_file',
        args: { path: 'src/gone.ts' },
        resultData: { results: [{ path: 'src/gone.ts', deleted: true }] },
      }),
    ])
    expect(changes).toHaveLength(1)
    expect(changes[0]?.status).toBe('deleted')
    expect(changes[0]?.rounds).toHaveLength(1)
    expect(changes[0]?.producedBy).toBe('delete_file')
  })

  it('多个不同文件各自一行', () => {
    const changes = buildFileChanges([
      activity({
        toolName: 'file_write',
        args: { path: 'a.ts' },
        resultData: { file: 'a.ts', old_content: 'x', new_content: 'y', added: 1, removed: 1 },
      }),
      activity({
        toolName: 'file_write',
        args: { path: 'b.ts' },
        resultData: { file: 'b.ts', old_content: '', new_content: 'z', added: 1 },
      }),
    ])
    expect(changes.map((c) => c.path)).toEqual(['a.ts', 'b.ts'])
    expect(summarizeChanges(changes)).toEqual({ added: 1, modified: 1, deleted: 0 })
  })
})

describe('computeFileNetStat — 首末净差（L1 展开才算）', () => {
  it('首末内容给出正确增删行数', () => {
    const change = buildFileChanges([
      activity({
        toolName: 'file_write',
        args: { path: 'a.txt' },
        resultData: { file: 'a.txt', old_content: 'a\nb\nc', new_content: 'a\nB\nc\nd', added: 2, removed: 1 },
      }),
    ])[0]!
    const stat = computeFileNetStat(change)
    expect(stat?.added).toBeGreaterThan(0)
    expect(stat?.removed).toBeGreaterThan(0)
  })

  it('内容缺失（删除/未带全文）返回 undefined，不编造数字', () => {
    const change = buildFileChanges([
      activity({
        toolName: 'delete_file',
        args: { path: 'x.log' },
        resultData: { results: [{ path: 'x.log', deleted: true }] },
      }),
    ])[0]!
    expect(computeFileNetStat(change)).toBeUndefined()
  })
})
