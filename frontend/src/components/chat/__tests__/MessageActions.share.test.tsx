/**
 * @feature FP-T12 前端组件补测 | @ci: frontend-test
 * 分享卡链路测试（handleShare 四路）。
 *
 * 行为契约（MessageActions.tsx handleShare + shareCard 接入面）：
 *  - 找不到消息卡片 → toast.error('未找到消息卡片')，不进入捕获流程；
 *  - 捕获成功 + 剪贴板写入成功 → toast.success('分享卡已复制到剪贴板')；
 *  - 剪贴板写入失败 → 降级 downloadDataUrl + toast.success('剪贴板不可用，已下载分享卡图片')；
 *  - 捕获抛错 → toast.error('分享卡生成失败: …')，不触发降级下载。
 * shareCard 模块与 sonner 均为外部依赖，按惯例 vi.mock 桩替。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MessageActions } from '../MessageActions'
import type { Message } from '@/types/models'

const toastErrorMock = vi.fn()
const toastSuccessMock = vi.fn()
vi.mock('sonner', () => ({
  toast: {
    error: (...args: unknown[]) => toastErrorMock(...args),
    success: (...args: unknown[]) => toastSuccessMock(...args),
  },
}))

const findMessageCardMock = vi.fn<() => HTMLElement | null>()
const captureMessageCardMock = vi.fn<() => Promise<string>>()
const copyPngToClipboardMock = vi.fn<() => Promise<void>>()
const downloadDataUrlMock = vi.fn()
vi.mock('@/lib/shareCard', () => ({
  findMessageCard: () => findMessageCardMock(),
  captureMessageCard: (el: HTMLElement) => captureMessageCardMock(el),
  copyPngToClipboard: (url: string) => copyPngToClipboardMock(url),
  downloadDataUrl: (url: string, name: string) => downloadDataUrlMock(url, name),
}))

function makeMessage(role: Message['role'], overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-share-1',
    sessionId: 'session-1',
    sequence: 1,
    role,
    content: '分享内容',
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

function renderActions(overrides: Partial<Message> = {}) {
  render(
    <MessageActions message={makeMessage('assistant', overrides)} sessionId="session-1" />,
  )
  return screen.getByTestId('share-card-button')
}

const CARD = document.createElement('div')

beforeEach(() => {
  vi.clearAllMocks()
  findMessageCardMock.mockReturnValue(CARD)
  captureMessageCardMock.mockResolvedValue('data:image/png;base64,AAA')
  copyPngToClipboardMock.mockResolvedValue(undefined)
})

describe('MessageActions 分享卡链路', () => {
  it('找不到消息卡片 → toast.error 且不进入捕获流程', async () => {
    findMessageCardMock.mockReturnValue(null)
    const btn = renderActions()
    fireEvent.click(btn)
    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledWith('未找到消息卡片'))
    expect(captureMessageCardMock).not.toHaveBeenCalled()
    expect(toastSuccessMock).not.toHaveBeenCalled()
  })

  it('捕获成功 + 剪贴板写入成功 → toast 提示已复制，不触发下载', async () => {
    const btn = renderActions()
    fireEvent.click(btn)
    await waitFor(() =>
      expect(toastSuccessMock).toHaveBeenCalledWith('分享卡已复制到剪贴板'),
    )
    expect(captureMessageCardMock).toHaveBeenCalledWith(CARD)
    expect(copyPngToClipboardMock).toHaveBeenCalledWith('data:image/png;base64,AAA')
    expect(downloadDataUrlMock).not.toHaveBeenCalled()
    expect(toastErrorMock).not.toHaveBeenCalled()
    // sharing 复位：按钮恢复可用
    await waitFor(() => expect(btn).not.toBeDisabled())
  })

  it('剪贴板写入失败 → 降级下载分享卡 PNG 并 toast 提示', async () => {
    copyPngToClipboardMock.mockRejectedValue(new Error('clipboard image unsupported'))
    const btn = renderActions()
    fireEvent.click(btn)
    await waitFor(() =>
      expect(toastSuccessMock).toHaveBeenCalledWith('剪贴板不可用，已下载分享卡图片'),
    )
    expect(downloadDataUrlMock).toHaveBeenCalledWith(
      'data:image/png;base64,AAA',
      `share-card-${makeMessage('assistant').id}.png`,
    )
    expect(toastErrorMock).not.toHaveBeenCalled()
    await waitFor(() => expect(btn).not.toBeDisabled())
  })

  it('捕获抛错 → toast.error 携带错误串，不写剪贴板不下载', async () => {
    captureMessageCardMock.mockRejectedValue(new Error('render boom'))
    const btn = renderActions()
    fireEvent.click(btn)
    await waitFor(() =>
      expect(toastErrorMock).toHaveBeenCalledWith('分享卡生成失败: Error: render boom'),
    )
    expect(copyPngToClipboardMock).not.toHaveBeenCalled()
    expect(downloadDataUrlMock).not.toHaveBeenCalled()
    expect(toastSuccessMock).not.toHaveBeenCalled()
    await waitFor(() => expect(btn).not.toBeDisabled())
  })
})
