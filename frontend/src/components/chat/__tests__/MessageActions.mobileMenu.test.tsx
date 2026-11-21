// @feature FP-0.2.mobile_visual_p1 @ci: frontend-test
/**
 * 消息动作行移动端收纳（P1-5，MuMu 实测 shots/09 每条消息一份 4 图标行）：
 * 移动视口只留「⋯」单入口（44px 触控位），复制/分享/编辑/回退/重新生成收进
 * 菜单且功能可达；桌面平铺组契约不变；时间戳归 MessageItem 元信息行（不在本
 * 组件，收纳不动它）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MessageActions } from '@/components/chat/MessageActions'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import type { Message } from '@/types/models'

function makeMessage(over: Partial<Message> = {}): Message {
  return {
    id: 'm1',
    role: 'user',
    content: '你好',
    status: 'completed',
    timestamp: new Date().toISOString(),
    sessionId: 's1',
    ...over,
  } as Message
}

function mount(msg: Message, handlers: Partial<Parameters<typeof MessageActions>[0]> = {}) {
  return render(
    <MessageActions
      message={msg}
      sessionId="s1"
      onCopy={handlers.onCopy}
      onEdit={handlers.onEdit}
      onRollbackTo={handlers.onRollbackTo}
      onRegenerate={handlers.onRegenerate}
      isLastMessage
    />,
  )
}

afterEach(() => {
  window.innerWidth = 1024
  vi.restoreAllMocks()
})

describe('MessageActions — 移动端「⋯」收纳（P1-5）', () => {
  it('移动视口：平铺图标退场、「⋯」触发器在场（44px 触控位）', () => {
    window.innerWidth = 375
    mount(makeMessage(), { onEdit: vi.fn(), onRollbackTo: vi.fn() })
    expect(screen.queryByTitle('复制')).toBeNull()
    expect(screen.queryByTitle('分享为图片')).toBeNull()
    expect(screen.queryByTitle('编辑并重新发送')).toBeNull()
    const more = screen.getByTestId('message-actions-more')
    expect(more.className).toContain('h-11')
  })

  it('收纳后功能可达：复制/编辑/回退条目在菜单内并回调原链路', () => {
    window.innerWidth = 375
    const onCopy = vi.fn()
    const onEdit = vi.fn()
    const onRollbackTo = vi.fn()
    mount(makeMessage(), { onCopy, onEdit, onRollbackTo })
    fireEvent.pointerDown(screen.getByTestId('message-actions-more'))
    fireEvent.click(screen.getByTestId('message-menu-copy'))
    expect(onCopy).toHaveBeenCalledTimes(1)
    fireEvent.pointerDown(screen.getByTestId('message-actions-more'))
    fireEvent.click(screen.getByTestId('message-menu-edit'))
    expect(onEdit).toHaveBeenCalledTimes(1)
    fireEvent.pointerDown(screen.getByTestId('message-actions-more'))
    fireEvent.click(screen.getByTestId('message-menu-rollback'))
    // 回退仍走内联二次确认（确认前零副作用）
    expect(onRollbackTo).not.toHaveBeenCalled()
    expect(screen.getByTestId('rollback-confirm')).toBeInTheDocument()
    fireEvent.click(screen.getByText('确认'))
    expect(onRollbackTo).toHaveBeenCalledWith('m1')
  })

  it('assistant 末条消息：重新生成条目在菜单内', () => {
    window.innerWidth = 375
    const onRegenerate = vi.fn()
    mount(makeMessage({ role: 'assistant', content: '回复' }), { onRegenerate })
    fireEvent.pointerDown(screen.getByTestId('message-actions-more'))
    fireEvent.click(screen.getByTestId('message-menu-regenerate'))
    expect(onRegenerate).toHaveBeenCalledTimes(1)
  })

  it('桌面视口：hover 平铺组契约不变（图标组在场、无「⋯」）', () => {
    mount(makeMessage(), { onEdit: vi.fn(), onRollbackTo: vi.fn() })
    expect(screen.getByTitle('复制')).toBeInTheDocument()
    expect(screen.getByTitle('分享为图片')).toBeInTheDocument()
    expect(screen.queryByTestId('message-actions-more')).toBeNull()
  })

  it('默认复制链路（无 onCopy 回调）：写剪贴板 + toast（既有 toast 面）', async () => {
    window.innerWidth = 375
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    mount(makeMessage())
    fireEvent.pointerDown(screen.getByTestId('message-actions-more'))
    fireEvent.click(screen.getByTestId('message-menu-copy'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('你好'))
  })
})
