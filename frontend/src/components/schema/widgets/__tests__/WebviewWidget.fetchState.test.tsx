/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * WebviewWidget 取数状态机测试（BUG-42 第二层）
 *
 * 契约（可观察行为）：
 * - 取数成功 → 渲染 iframe（既有 AC 用例已覆盖，此处经重试链路回归）
 * - 取数失败 → 错误态显式可见（含错误原因）+ 重试按钮；点重试重新发请求，
 *   成功后进入 iframe —— 失败绝不静默停留「加载 Webview...」
 * - 取数挂起超时（请求在浏览器队列/内核饱和中静默悬挂）→ 显式超时错误态 + 重试，
 *   不允许无限期停留加载占位
 * - 空 message 错误 → 回退失败文案可见（空串不得被当 falsy 吞回加载态）
 *
 * mock 仅用于外部依赖（axios apiClient = 网络）；挂起超时用 fake clock 注入
 * 可调延迟断言时序行为，不使用零延迟 mock。
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import type { Mock } from 'vitest'

vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { WebviewWidget } from '../WebviewWidget'

const apiGet = apiClient.get as unknown as Mock

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('WebviewWidget 取数状态机（BUG-42 第二层）', () => {
  it('失败 → 错误态+重试按钮可见；重试重新发请求，成功后渲染 iframe', async () => {
    apiGet.mockRejectedValueOnce(new Error('kernel 504'))
    apiGet.mockResolvedValueOnce({ data: '<p>ok</p>' })
    render(<WebviewWidget pluginId="demo" />)

    // 终态显式可见：错误原因 + 重试入口
    expect(await screen.findByText(/Webview 加载失败/)).toBeInTheDocument()
    expect(screen.getByText(/kernel 504/)).toBeInTheDocument()
    const retry = screen.getByRole('button', { name: '重试' })
    // 性质断言：失败后加载占位必须让位给错误态
    expect(screen.queryByText('加载 Webview...')).not.toBeInTheDocument()

    fireEvent.click(retry)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    // 重试语义：同一 endpoint 第二次取数（首次失败 + 本次成功）
    expect(apiGet).toHaveBeenCalledTimes(2)
    expect(String(apiGet.mock.calls[0][0])).toBe(String(apiGet.mock.calls[1][0]))
  })

  it('挂起永不落定 → 超时转显式错误态+重试（fake clock 推进取数窗），重试成功进入 iframe', async () => {
    apiGet.mockImplementationOnce(() => new Promise(() => {}))
    apiGet.mockResolvedValueOnce({ data: '<p>late-ok</p>' })

    vi.useFakeTimers()
    render(<WebviewWidget pluginId="demo" />)
    expect(screen.getByText('加载 Webview...')).toBeInTheDocument()

    // 推进整个取数挂起窗（组件级 30s）——请求静默悬挂时用户必须看到终态
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })

    expect(screen.getByText(/Webview 加载失败/)).toBeInTheDocument()
    expect(screen.getByText(/加载超时/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    expect(screen.queryByText('加载 Webview...')).not.toBeInTheDocument()

    // 恢复真实时钟走重试链路（mock 微任务即可落定，无需计时器）
    vi.useRealTimers()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect(screen.queryByText(/Webview 加载失败/)).not.toBeInTheDocument()
  })

  it('空 message 错误 → 回退失败文案可见，不回落加载占位', async () => {
    apiGet.mockRejectedValue(new Error(''))
    render(<WebviewWidget pluginId="demo" />)

    expect(await screen.findByText(/Webview 加载失败/)).toBeInTheDocument()
    expect(screen.getByText(/加载插件 HTML 失败/)).toBeInTheDocument()
    expect(screen.queryByText('加载 Webview...')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })
})
