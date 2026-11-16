/** @feature WebView 110 兼容系统审计 R12 | @ci: frontend-test */
/**
 * MessageActions — WebView 110（Chromium <111）防回归测试
 *
 * 根因（R11 插件页同案）：Tailwind v4 对 color-mix 任意值底类（混 transparent 求
 * 半透明面）生成两段式产物——@supports color-mix 门内才是 N% 淡底，门外 fallback
 * 是裸 var
 * 实心色；110 不过门 → 实心状态色底 + 同色文字（text-[var(--status-warning)]）
 * = 回退二次确认条文字隐形。
 * 契约：渲染产物禁入 color-mix（110 破坏形态），半透明底一律
 * rgb(var(--status-warning-rgb,fallback)/alpha) 原生 alpha 语法（Chromium 65+，
 * 无门、双端一致；-rgb 三元组 design-tokens 静态在场 + 主题引擎运行时同步发射）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MessageActions } from '../MessageActions'
import type { Message } from '@/types/models'

function makeUserMessage(): Message {
  return {
    id: 'msg-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'user',
    content: '原始问题',
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'completed',
  } as Message
}

function renderWithRollbackConfirm() {
  const onRollbackTo = vi.fn()
  const { container } = render(
    <MessageActions message={makeUserMessage()} sessionId="session-1" onRollbackTo={onRollbackTo} />,
  )
  const rollbackBtn = screen
    .getAllByRole('button')
    .find((b) => b.getAttribute('title') === '回退到这条消息')
  expect(rollbackBtn).toBeDefined()
  fireEvent.click(rollbackBtn!)
  expect(screen.getByTestId('rollback-confirm')).toBeDefined()
  return { container }
}

describe('MessageActions · WebView 110 兼容（禁入 color-mix 破坏形态）', () => {
  it('回退二次确认条底色为 rgb(var(--status-warning-rgb)/alpha) 原生 alpha 类', () => {
    const { container } = renderWithRollbackConfirm()
    expect(container.innerHTML).toContain(
      'bg-[rgb(var(--status-warning-rgb,217_119_6)/0.15)]',
    )
  })

  it('回退确认条展开态渲染产物不含任何 color-mix（110 两段式实心 fallback 根因禁入）', () => {
    const { container } = renderWithRollbackConfirm()
    expect(container.innerHTML).not.toContain('color-mix(')
    // 文字色保持裸 var（无 color-mix，110 与桌面同渲染）
    expect(container.innerHTML).toContain('text-[var(--status-warning)]')
  })

  it('默认态（确认条未展开）渲染产物同样不含 color-mix', () => {
    const { container } = render(
      <MessageActions message={makeUserMessage()} sessionId="session-1" />,
    )
    expect(container.innerHTML).not.toContain('color-mix(')
  })
})
