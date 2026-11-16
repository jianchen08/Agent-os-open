/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 聊天输入动作声明的排序补测：多条 input-action 声明按 order 升序呈现
 * （order 缺省 50），点击各自携带自己的声明。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { ChatInputActions } from '../ChatInputActions'

beforeEach(() => contributionRegistry.clear())
afterEach(() => contributionRegistry.clear())

describe('ChatInputActions — 多声明按 order 排序', () => {
  it('order 较小的动作排在前面（缺省 order=50 居中），全部可点击', () => {
    contributionRegistry.register({
      type: 'pages',
      id: 'late-action',
      title: '后置动作',
      space: 'chat',
      slot: 'input-action',
      order: 90,
      pluginId: 'ext',
    })
    contributionRegistry.register({
      type: 'pages',
      id: 'early-action',
      title: '前置动作',
      space: 'chat',
      slot: 'input-action',
      order: 10,
      pluginId: 'ext',
    })

    const onAction = vi.fn()
    render(<ChatInputActions onAction={onAction} />)

    const labels = screen
      .getAllByRole('button')
      .map((btn) => btn.getAttribute('aria-label'))
    expect(labels).toEqual(['前置动作', '后置动作'])

    fireEvent.click(screen.getByRole('button', { name: '后置动作' }))
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ id: 'late-action' }))
  })
})
