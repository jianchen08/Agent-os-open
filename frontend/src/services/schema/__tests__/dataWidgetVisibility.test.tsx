// @feature: FP-0.2.四 前端Schema驱动 | @ci: frontend-test
/**
 * useDataWidget 离屏暂停契约测试（WS 推送通道）
 *
 * - visible=false：WS 推送不触发状态更新（隐藏面板重渲染纯负载），仅缓存最新一帧
 * - 恢复 visible=true：缓存帧立即应用（数据不丢只延迟）
 * - visible 缺省（=true）：行为与旧版一致（既有用例回归保障）
 */
import { act, render, screen, waitFor } from '@testing-library/react'
import React, { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { resetSharedFetchCache, useDataWidget } from '@/services/schema/dataWidget'

const apiGet = vi.fn()
const { wsSubscribe, wsUnsubscribe } = vi.hoisted(() => ({
  wsSubscribe: vi.fn(),
  wsUnsubscribe: vi.fn(),
}))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { subscribe: wsSubscribe, unsubscribe: wsUnsubscribe },
}))
vi.mock('@/services/api/client', () => ({
  default: Object.assign(vi.fn(), { get: (...args: unknown[]) => apiGet(...args) }),
}))

/** 取出 WS 订阅回调（单通道） */
function wsHandler(): (payload: unknown) => void {
  return wsSubscribe.mock.calls.at(-1)![1] as (payload: unknown) => void
}

const SERIES = { labels: ['a', 'b'], datasets: [{ data: [1, 2] }] }

function Host({ visible }: { visible?: boolean }) {
  const r = useDataWidget({ refresh: { type: 'ws', channel: 'monitor' } }, 'series', 0, visible)
  return <div data-testid="out">{r.data ? JSON.stringify(r.data) : 'empty'}</div>
}

/** 可切换 visible 的宿主（模拟面板离屏/回屏） */
function ToggleHost() {
  const [visible, setVisible] = useState(true)
  return (
    <>
      <button onClick={() => setVisible(false)} data-testid="hide">
        hide
      </button>
      <button onClick={() => setVisible(true)} data-testid="show">
        show
      </button>
      <Host visible={visible} />
    </>
  )
}

beforeEach(() => {
  wsSubscribe.mockReset()
  wsUnsubscribe.mockReset()
  apiGet.mockReset()
  apiGet.mockResolvedValue({ data: {} })
  resetSharedFetchCache()
})

describe('useDataWidget 离屏暂停（WS）', () => {
  it('不可见时推送不更新渲染，仅缓存最新帧', () => {
    render(<Host visible={false} />)
    expect(screen.getByTestId('out').textContent).toBe('empty')

    act(() => {
      wsHandler()({ ...SERIES, datasets: [{ data: [7, 8] }] })
    })
    // 隐藏面板不重渲染
    expect(screen.getByTestId('out').textContent).toBe('empty')
  })

  it('恢复可见即应用最新帧（只应用最后一帧，中间帧丢弃）', () => {
    render(<ToggleHost />)
    expect(wsSubscribe).toHaveBeenCalled()
    act(() => {
      screen.getByTestId('hide').click()
    })
    act(() => {
      wsHandler()({ ...SERIES, datasets: [{ data: [1, 1] }] })
      wsHandler()({ ...SERIES, datasets: [{ data: [9, 9] }] })
    })
    expect(screen.getByTestId('out').textContent).toBe('empty')

    act(() => {
      screen.getByTestId('show').click()
    })
    const out = JSON.parse(screen.getByTestId('out').textContent!)
    expect(out.datasets[0].data).toEqual([9, 9])
  })

  it('恢复可见后新推送实时更新（回到在线语义）', async () => {
    render(<ToggleHost />)
    act(() => {
      screen.getByTestId('hide').click()
    })
    act(() => {
      screen.getByTestId('show').click()
    })
    act(() => {
      wsHandler()(SERIES)
    })
    await waitFor(() =>
      expect(screen.getByTestId('out').textContent).toContain('datasets'),
    )
  })

  it('visible 缺省 = 可见：推送即更新（旧行为不变）', () => {
    render(<Host />)
    act(() => {
      wsHandler()(SERIES)
    })
    expect(screen.getByTestId('out').textContent).toContain('"data":[1,2]')
  })
})
