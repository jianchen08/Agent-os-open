/** @feature FP-0.2.可观测性 工具卡片执行时长格式化 @ci frontend-test */
/**
 * formatDuration 位数收敛单测：后端 duration_ms 为 f64 浮点（tool_core
 * ToolResult.duration_ms），<1s 取整毫秒、10s 内 1 位小数秒、更长取整秒。
 */
import { describe, expect, it } from 'vitest'
import { formatDuration } from '../activity'

describe('formatDuration（浮点 duration_ms 位数收敛）', () => {
  it('亚秒级浮点 → 取整毫秒（不再输出 872.1098…ms）', () => {
    expect(formatDuration(872.1098728179932)).toBe('872ms')
    expect(formatDuration(0.4839)).toBe('0ms')
    expect(formatDuration(999.6)).toBe('1000ms')
  })

  it('秒级：10s 内 1 位小数，10s-60s 取整秒', () => {
    expect(formatDuration(1234.5678)).toBe('1.2s')
    expect(formatDuration(9523.7)).toBe('9.5s')
    expect(formatDuration(15400)).toBe('15s')
    expect(formatDuration(59999)).toBe('60s')
  })

  it('分钟级 m + s；非法输入兜底 0ms', () => {
    expect(formatDuration(125000)).toBe('2m 5s')
    expect(formatDuration(120000)).toBe('2m')
    expect(formatDuration(Number.NaN)).toBe('0ms')
    expect(formatDuration(-5)).toBe('0ms')
  })
})
