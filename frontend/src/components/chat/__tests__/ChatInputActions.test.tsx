/**
 * 功能测试：chat 空间声明驱动 —— 聊天输入动作（架构愿景功能点）
 *
 * 推演链：项目愿景（插件扩展能力）→ 前端愿景（chat 空间也声明驱动）→ 架构
 * （chat 空间的 input-action 声明必须有消费者）→ 功能点（"插件声明一个聊天输入动作，
 * 它在输入工具栏显示为按钮，点击回调触发"）。
 *
 * 之前 chat 空间（getPagesBySpace('chat')）零消费者——六个空间里唯一未声明驱动的。
 *
 * 本测试用真实 contributionRegistry 声明 + 渲染真实 ChatInputActions，断言按钮出现在 DOM、
 * 点击触发回调——端到端行为验证。
 */

import { render, screen, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ChatInputActions } from '@/components/chat/ChatInputActions'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

describe('功能点：ChatInputActions 渲染 chat 空间声明的输入动作', () => {
  beforeEach(() => contributionRegistry.clear())
  afterEach(() => contributionRegistry.clear())

  it('插件声明 chat/input-action → 工具栏出现对应按钮（title + icon）', () => {
    contributionRegistry.register({
      type: 'pages',
      id: 'translate-action',
      title: '翻译',
      icon: '🌐',
      space: 'chat',
      slot: 'input-action',
      pluginId: 'ext',
    })

    render(<ChatInputActions />)

    expect(screen.getByRole('button', { name: '翻译' })).toBeInTheDocument()
    expect(screen.getByText('🌐')).toBeInTheDocument()
  })

  it('点击声明动作按钮 → 触发 onAction 回调（携带声明）', () => {
    const onAction = vi.fn()
    contributionRegistry.register({
      type: 'pages',
      id: 'attach-action',
      title: '附件',
      icon: '📎',
      space: 'chat',
      slot: 'input-action',
      pluginId: 'ext',
    })

    render(<ChatInputActions onAction={onAction} />)
    fireEvent.click(screen.getByRole('button', { name: '附件' }))

    expect(onAction).toHaveBeenCalledTimes(1)
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ id: 'attach-action', title: '附件' }))
  })

  it('无 chat/input-action 声明时不渲染容器（不污染工具栏）', () => {
    const { container } = render(<ChatInputActions />)
    expect(container.querySelector('[data-testid="chat-input-actions"]')).toBeNull()
  })

  it('非 input-action 的 chat 声明（如 inline）不进入工具栏（slot 隔离）', () => {
    contributionRegistry.register({
      type: 'pages',
      id: 'inline-card',
      title: '内联卡片',
      space: 'chat',
      slot: 'inline',
      pluginId: 'ext',
    })

    render(<ChatInputActions />)
    expect(screen.queryByRole('button', { name: '内联卡片' })).not.toBeInTheDocument()
  })
})
