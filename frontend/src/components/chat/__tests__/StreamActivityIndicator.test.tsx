// @feature: FP-0.2.流式活性统一 2026-11-06 用户裁定 | @ci: frontend-test
/**
 * 统一流式活性指示测试：
 * - 锚点解析：最近前置 user 消息发出时刻（跨 tool/system 消息、通知注入
 *   消息、多轮 assistant 均归同一触发源）；无前置 user / 消息缺席 / 非法
 *   时间戳 → null（耗时面静默降级）
 * - 指示行：统一图标（animate-spin）+ 状态文案 + 实时总耗时（秒级自跳）；
 *   锚点缺席不渲染耗时
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearRunStartedAt, getRunStartedAt, noteRunStartedAt } from '@/utils/streamRunAnchor'
import { StreamActivityIndicator } from '../StreamActivityIndicator'
import { resolveStreamAnchorMs, resolveStreamDisplayAnchorMs } from '../streamAnchor'
import type { Message } from '@/types/models'

function makeMessage(overrides: Partial<Message> & { id: string; role: Message['role'] }): Message {
  return {
    sessionId: 'session-1',
    content: '',
    timestamp: new Date().toISOString(),
    ...overrides,
  } as Message
}

describe('resolveStreamAnchorMs — 锚点解析', () => {
  it('最近前置 user 消息的发出时刻为锚点', () => {
    const anchor = Date.parse('2026-11-06T10:00:00Z')
    const messages = [
      makeMessage({ id: 'u1', role: 'user', timestamp: '2026-11-06T10:00:00Z' }),
      makeMessage({ id: 'a1', role: 'assistant', timestamp: '2026-11-06T10:00:03Z' }),
    ]
    expect(resolveStreamAnchorMs(messages, 'a1')).toBe(anchor)
  })

  it('user 与 assistant 之间的 tool/system 消息不挡锚点（跨轮归同一触发源）', () => {
    const anchor = Date.parse('2026-11-06T10:00:00Z')
    const messages = [
      makeMessage({ id: 'u1', role: 'user', timestamp: '2026-11-06T10:00:00Z' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
      makeMessage({ id: 's1', role: 'system' }),
      makeMessage({ id: 'a2', role: 'assistant' }),
    ]
    expect(resolveStreamAnchorMs(messages, 'a2')).toBe(anchor)
  })

  it('多条 user 消息取最近一条前置（队列续发场景锚点随轮切换）', () => {
    const second = Date.parse('2026-11-06T10:05:00Z')
    const messages = [
      makeMessage({ id: 'u1', role: 'user', timestamp: '2026-11-06T10:00:00Z' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
      makeMessage({ id: 'u2', role: 'user', timestamp: '2026-11-06T10:05:00Z' }),
      makeMessage({ id: 'a2', role: 'assistant' }),
    ]
    expect(resolveStreamAnchorMs(messages, 'a2')).toBe(second)
  })

  it('无前置 user 消息 → null（耗时面静默降级，指示行只留图标与文案）', () => {
    const messages = [makeMessage({ id: 'a1', role: 'assistant' })]
    expect(resolveStreamAnchorMs(messages, 'a1')).toBeNull()
  })

  it('清单缺席 / 消息不在清单 → null', () => {
    expect(resolveStreamAnchorMs(undefined, 'a1')).toBeNull()
    expect(resolveStreamAnchorMs([], 'a1')).toBeNull()
    expect(
      resolveStreamAnchorMs([makeMessage({ id: 'a1', role: 'assistant' })], 'missing'),
    ).toBeNull()
  })

  it('前置 user 时间戳非法 → null（不折算成天文数字假耗时）', () => {
    const messages = [
      makeMessage({ id: 'u1', role: 'user', timestamp: 'not-a-date' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
    ]
    expect(resolveStreamAnchorMs(messages, 'a1')).toBeNull()
  })
})

describe('streamRunAnchor + resolveStreamDisplayAnchorMs — run 级执行起点', () => {
  const PID = 'pipe-run-anchor'
  afterEach(() => {
    clearRunStartedAt(PID)
    clearRunStartedAt('pipe-cap')
  })

  it('登记/查询/清除：run 开始覆盖写，终止清除', () => {
    noteRunStartedAt(PID, 1000)
    expect(getRunStartedAt(PID)).toBe(1000)
    // 同管道再次 run（重新生成）覆盖写
    noteRunStartedAt(PID, 2000)
    expect(getRunStartedAt(PID)).toBe(2000)
    clearRunStartedAt(PID)
    expect(getRunStartedAt(PID)).toBeUndefined()
  })

  it('登记上限：超限按插入序清扫最旧 1/4，新登记保留', () => {
    for (let i = 0; i < 200; i++) noteRunStartedAt(`pipe-cap-${i}`, i)
    noteRunStartedAt('pipe-cap-new', 1)
    // 200 满额 + 新登记触发清扫 → 最旧 50 条被清，其余与新登记并存
    expect(getRunStartedAt('pipe-cap-new')).toBe(1)
    expect(getRunStartedAt('pipe-cap-199')).toBe(199)
    expect(getRunStartedAt('pipe-cap-0')).toBeUndefined()
    for (let i = 0; i < 200; i++) clearRunStartedAt(`pipe-cap-${i}`)
    clearRunStartedAt('pipe-cap-new')
  })

  it('重新生成场景：run 起点较新接管锚点（旧 user 消息时间戳不再拖计时）', () => {
    noteRunStartedAt(PID, Date.parse('2026-11-07T10:10:00Z'))
    const messages = [
      makeMessage({ id: 'u1', role: 'user', timestamp: '2026-11-07T10:00:00Z' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
    ]
    expect(resolveStreamDisplayAnchorMs(messages, 'a1', PID)).toBe(
      Date.parse('2026-11-07T10:10:00Z'),
    )
  })

  it('队列续发场景：后发 user 消息较新仍归位各自触发消息（run 未换不误占）', () => {
    noteRunStartedAt(PID, Date.parse('2026-11-07T10:00:00Z'))
    const messages = [
      makeMessage({ id: 'u1', role: 'user', timestamp: '2026-11-07T10:00:00Z' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
      makeMessage({ id: 'u2', role: 'user', timestamp: '2026-11-07T10:05:00Z' }),
      makeMessage({ id: 'a2', role: 'assistant' }),
    ]
    expect(resolveStreamDisplayAnchorMs(messages, 'a2', PID)).toBe(
      Date.parse('2026-11-07T10:05:00Z'),
    )
  })

  it('run 起点缺席（刷新后未登记）回落 user 消息锚点；无 user 消息时 run 起点兜底', () => {
    const messages = [
      makeMessage({ id: 'u1', role: 'user', timestamp: '2026-11-07T10:00:00Z' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
    ]
    expect(resolveStreamDisplayAnchorMs(messages, 'a1', PID)).toBe(
      Date.parse('2026-11-07T10:00:00Z'),
    )

    noteRunStartedAt(PID, Date.parse('2026-11-07T10:10:00Z'))
    const orphaned = [makeMessage({ id: 'a2', role: 'assistant' })]
    expect(resolveStreamDisplayAnchorMs(orphaned, 'a2', PID)).toBe(
      Date.parse('2026-11-07T10:10:00Z'),
    )
  })

  it('pipelineId 缺省：纯 user 消息锚点（子 Tab 渲染非激活管道的回落形态）', () => {
    const messages = [
      makeMessage({ id: 'u1', role: 'user', timestamp: '2026-11-07T10:00:00Z' }),
      makeMessage({ id: 'a1', role: 'assistant' }),
    ]
    expect(resolveStreamDisplayAnchorMs(messages, 'a1', null)).toBe(
      Date.parse('2026-11-07T10:00:00Z'),
    )
  })
})

describe('StreamActivityIndicator — 指示行', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('渲染统一转圈图标与状态文案；锚点缺席不渲染耗时', () => {
    const { container } = render(
      <StreamActivityIndicator label="执行中..." anchorMs={null} testId="tail" />,
    )
    expect(screen.getByText('执行中...')).toBeInTheDocument()
    expect(container.querySelector('.animate-spin')).toBeInTheDocument()
    expect(screen.queryByTestId('stream-elapsed')).not.toBeInTheDocument()
  })

  it('带锚点：0.1s 粒度自跳——小数位每 0.1s 进位，满 1s 秒位才进', () => {
    vi.setSystemTime(new Date('2026-11-06T10:00:10Z'))
    const anchor = Date.parse('2026-11-06T10:00:07.5Z')
    render(<StreamActivityIndicator label="思考中..." anchorMs={anchor} testId="tail" />)
    expect(screen.getByTestId('stream-elapsed')).toHaveTextContent('2.5s')

    act(() => {
      vi.advanceTimersByTime(100)
    })
    expect(screen.getByTestId('stream-elapsed')).toHaveTextContent('2.6s')

    act(() => {
      vi.advanceTimersByTime(400)
    })
    expect(screen.getByTestId('stream-elapsed')).toHaveTextContent('3.0s')
  })

  it('锚点在前（时钟回拨防护）：显示 0 起步而非负耗时', () => {
    vi.setSystemTime(new Date('2026-11-06T10:00:00Z'))
    render(
      <StreamActivityIndicator
        label="思考中..."
        anchorMs={Date.parse('2026-11-06T10:00:05Z')}
        testId="tail"
      />,
    )
    expect(screen.getByTestId('stream-elapsed')).toHaveTextContent('0ms')
  })
})
