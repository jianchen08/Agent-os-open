/** @feature WebView 110 兼容系统审计 R12 | @ci: frontend-test */
/**
 * VotingPanel — WebView 110（Chromium <111）防回归测试
 *
 * 根因（R11 插件页同案，R12 全仓审计产物反查命中）：状态色 var 任意值类加
 * alpha 修饰符（/10 形态）生成两段式产物——@supports color-mix 门内才是
 * 10% 淡底，门外 fallback 是裸 var 实心色；110 不过门 → 实心状态色底 + 同色
 * 文字 = 状态徽章（「投票中」/「已取消」）文字隐形。
 * 契约：状态徽章底色一律 rgb(var(--status-*-rgb,fallback)/alpha) 原生 alpha
 * 语法（Chromium 65+，无门、双端一致；-rgb 三元组 design-tokens 静态在场 +
 * 主题引擎运行时同步发射），渲染产物禁入 color-mix。
 */

import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useVotingStore } from '@/stores/votingStore'
import { VotingPanel } from '../VotingPanel'
import type { VotingOption, VotingSession } from '@/types/voting'

function makeOption(overrides: Partial<VotingOption> = {}): VotingOption {
  return {
    id: 'opt-1',
    title: '方案A',
    voteCount: 0,
    hasVoted: false,
    ...overrides,
  }
}

function makeSession(overrides: Partial<VotingSession> = {}): VotingSession {
  return {
    id: 'v-1',
    title: '选择实现方案',
    agentId: 'agent-1',
    options: [makeOption(), makeOption({ id: 'opt-2', title: '方案B' })],
    status: 'open',
    allowMultiple: false,
    requireReason: false,
    ...overrides,
  }
}

beforeEach(() => {
  useVotingStore.setState({ votingSessions: [], expandedVotingId: null })
})

describe('VotingPanel · WebView 110 兼容（禁入 color-mix 破坏形态）', () => {
  it('投票中徽章底色为 rgb(var(--status-success-rgb)/alpha) 原生 alpha 类', () => {
    const { container } = render(<VotingPanel voting={makeSession()} />)
    expect(screen.getByText('投票中')).toBeDefined()
    expect(container.innerHTML).toContain(
      'bg-[rgb(var(--status-success-rgb,5_150_105)/0.1)]',
    )
    expect(container.innerHTML).toContain('text-[var(--status-success)]')
  })

  it('已取消徽章底色为 rgb(var(--status-error-rgb)/alpha) 原生 alpha 类', () => {
    const { container } = render(
      <VotingPanel voting={makeSession({ status: 'cancelled' })} />,
    )
    expect(screen.getByText('已取消')).toBeDefined()
    expect(container.innerHTML).toContain(
      'bg-[rgb(var(--status-error-rgb,220_38_38)/0.1)]',
    )
  })

  it('两种状态渲染产物均不含任何 color-mix（110 两段式实心 fallback 根因禁入）', () => {
    const open = render(<VotingPanel voting={makeSession()} />)
    expect(open.container.innerHTML).not.toContain('color-mix(')
    open.unmount()
    const cancelled = render(
      <VotingPanel voting={makeSession({ status: 'cancelled' })} />,
    )
    expect(cancelled.container.innerHTML).not.toContain('color-mix(')
  })
})
