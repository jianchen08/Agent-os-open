// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * handleIteration 迭代事件处理器测试
 *
 * 迭代事件（管道引擎迭代开始/结束时由后端发送）仅作日志记录，不写 parts[]。
 * 验证：
 * 1. 完整事件（pipeline_id + message_id + iteration/max_iterations）→ debug 日志
 * 2. pipeline_id 缺失 → warn 且直接返回（不取 _threadId 顶替）
 * 3. message_id 缺失 → 返回（不崩溃）
 * 4. iteration 字段两种位置（顶层/data）都解析
 * 5. 事件不影响 store（无任何副作用）
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const mockDebug = vi.hoisted(() => vi.fn())
const mockWarn = vi.hoisted(() => vi.fn())

vi.mock('@/utils/logger', () => ({
  loggers: {
    websocket: { debug: mockDebug, info: vi.fn(), warn: mockWarn, error: vi.fn() },
  },
}))

vi.mock('../router', () => ({
  resolvePipelineId: (data: any) => {
    const pid = data?.data?.pipeline_id ?? data?.pipeline_id
    return typeof pid === 'string' && pid.length > 0 ? pid : null
  },
}))

import { handleIteration } from '../iterationHandler'

describe('handleIteration 迭代事件（日志性质，不写 store）', () => {
  beforeEach(() => {
    mockDebug.mockClear()
    mockWarn.mockClear()
  })

  it('完整事件：pipeline_id + message_id + iteration 顶层字段 → debug 日志含 iter/max', () => {
    handleIteration({
      pipeline_id: 'pipe-iter-001',
      message_id: 'msg-iter-001',
      iteration: 2,
      max_iterations: 5,
    })
    expect(mockDebug).toHaveBeenCalledWith(
      expect.stringContaining('[ITERATION]'),
      expect.any(String), expect.any(String), 2, 5,
    )
    expect(mockWarn).not.toHaveBeenCalled()
  })

  it('iteration/max_iterations 位于 data 嵌套层 → 同样解析（默认 0 兜底）', () => {
    handleIteration({
      data: {
        pipeline_id: 'pipe-iter-002',
        message_id: 'msg-iter-002',
        iteration: 1,
        max_iterations: 3,
      },
    })
    expect(mockDebug).toHaveBeenCalledWith(
      expect.stringContaining('[ITERATION]'),
      expect.any(String), expect.any(String), 1, 3,
    )
  })

  it('iteration/max_iterations 缺失 → 默认 0（不崩溃）', () => {
    handleIteration({ pipeline_id: 'pipe-iter-003', message_id: 'msg-iter-003' })
    expect(mockDebug).toHaveBeenCalledWith(
      expect.stringContaining('[ITERATION]'),
      expect.any(String), expect.any(String), 0, 0,
    )
  })

  it('pipeline_id 缺失 → warn（含 _threadId/msgId 截断）并返回，无 debug 日志', () => {
    handleIteration({
      data: { _threadId: 'thread-abcdef123456', message_id: 'msg-abcdef123456' },
    })
    expect(mockWarn).toHaveBeenCalledWith(
      expect.stringContaining('[ITERATION] pipeline_id missing'),
      'thread-abcde', 'msg-abcdef12',
    )
    expect(mockDebug).not.toHaveBeenCalled()
  })

  it('pipeline_id 存在但 message_id 缺失 → 静默返回（不 warn 不 debug）', () => {
    handleIteration({ pipeline_id: 'pipe-iter-004' })
    expect(mockDebug).not.toHaveBeenCalled()
    expect(mockWarn).not.toHaveBeenCalled()
  })

  it('message_id 位于 data 嵌套层 → 正常解析（extractMessageId 多来源）', () => {
    handleIteration({ pipeline_id: 'pipe-iter-005', data: { message_id: 'msg-nested-01' } })
    expect(mockDebug).toHaveBeenCalledTimes(1)
  })
})
