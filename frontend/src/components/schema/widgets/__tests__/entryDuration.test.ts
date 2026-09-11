/**
 * entryDurationMs 回归（GUI 黑盒测试 2026-09-11）：
 *
 * state 独有条目曾以 new Date(0)（epoch-0）充当 startedAt 占位，endedAt -
 * epoch0 算出 ~496969h 的天文数字假耗时（stress2-10 已完成却显示 56 年级
 * 时长）。契约：起点无真值（空串/非法日期）→ null → 展示 '--'；合法起点
 * 时正常差值（ended 缺席 = 进行中，now 起算）。
 */

import { describe, expect, it } from 'vitest'
import { entryDurationMs } from '@/types/activity'

const NOW = Date.parse('2026-09-11T02:00:00Z')

describe('entryDurationMs', () => {
  it('startedAt 为空串返回 null（未知耗时）', () => {
    expect(entryDurationMs({ startedAt: '', endedAt: '2026-09-10T00:00:00Z' }, NOW)).toBeNull()
  })

  it('startedAt 为非法日期返回 null', () => {
    expect(entryDurationMs({ startedAt: 'not-a-date' }, NOW)).toBeNull()
  })

  it('合法起点 + endedAt → 差值', () => {
    const d = entryDurationMs(
      { startedAt: '2026-09-11T01:59:40Z', endedAt: '2026-09-11T02:00:00Z' },
      NOW,
    )
    expect(d).toBe(20_000)
  })

  it('合法起点无 endedAt → now 起算（进行中）', () => {
    const d = entryDurationMs({ startedAt: '2026-09-11T01:59:50Z' }, NOW)
    expect(d).toBe(10_000)
  })
})
