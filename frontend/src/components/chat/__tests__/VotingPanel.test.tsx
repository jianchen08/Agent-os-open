/** @ci frontend-test */
/**
 * VotingPanel 契约测试（0% → 主链覆盖批次）。
 *
 * 覆盖：状态徽章（投票中/已结束/已取消）、选项点选与多选上限、
 * 理由输入（必填禁用提交/可选展开）、提交成功与失败两路（真实
 * votingStore 副作用断言）、已结束的获胜方案摘要、details 展开。
 *
 * 组件提交走真实 votingStore（session 预注入 store），不 mock 内部依赖。
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'

import { VotingPanel } from '../VotingPanel'
import { useVotingStore } from '@/stores/votingStore'
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

function renderPanel(voting: VotingSession) {
  return render(<VotingPanel voting={voting} />)
}

beforeEach(() => {
  useVotingStore.setState({ votingSessions: [], expandedVotingId: null })
})

describe('VotingPanel: 状态呈现', () => {
  it('投票中：标题 + 状态徽章 + 选项列表', () => {
    renderPanel(makeSession())
    expect(screen.getByText('选择实现方案')).toBeDefined()
    expect(screen.getByText('投票中')).toBeDefined()
    expect(screen.getByText('方案A')).toBeDefined()
    expect(screen.getByText('方案B')).toBeDefined()
  })

  it('已结束且有结果：显示获胜方案与参与人数', () => {
    renderPanel(
      makeSession({
        status: 'closed',
        options: [
          makeOption({ id: 'opt-1', title: '方案A', voteCount: 3, hasVoted: true }),
          makeOption({ id: 'opt-2', title: '方案B', voteCount: 1 }),
        ],
        result: {
          winnerId: 'opt-1',
          totalVoters: 4,
          optionResults: [
            { optionId: 'opt-1', voteCount: 3, percentage: 75 },
            { optionId: 'opt-2', voteCount: 1, percentage: 25 },
          ],
        },
      }),
    )
    expect(screen.getByText('已结束')).toBeDefined()
    expect(screen.getAllByText('方案A').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/4 人参与投票/)).toBeDefined()
  })

  it('已取消：徽章显示已取消，无操作区', () => {
    renderPanel(makeSession({ status: 'cancelled' }))
    expect(screen.getByText('已取消')).toBeDefined()
    expect(screen.queryByText('提交投票')).toBeNull()
  })

  it('已投票：显示已投票提示，不再提供操作区', () => {
    renderPanel(
      makeSession({
        options: [makeOption({ hasVoted: true })],
      }),
    )
    expect(screen.getByText('已投票')).toBeDefined()
    expect(screen.queryByText('提交投票')).toBeNull()
  })
})

describe('VotingPanel: 点选与多选上限', () => {
  it('单选：点选高亮，点另一个替换', () => {
    renderPanel(makeSession())
    const cards = screen.getAllByText(/方案[AB]/)
    fireEvent.click(cards[0])
    fireEvent.click(cards[1])
    // 单选替换后仅一项选中：提交按钮可点（选中数 > 0）
    expect(screen.getByRole('button', { name: /提交投票/ })).not.toHaveProperty('disabled', true)
  })

  it('多选：显示已选计数，超上限忽略后续点选', () => {
    renderPanel(
      makeSession({ allowMultiple: true, maxSelections: 1 }),
    )
    const cards = screen.getAllByText(/方案[AB]/)
    fireEvent.click(cards[0])
    expect(screen.getByText(/已选择 1 项/)).toBeDefined()
    fireEvent.click(cards[1])
    // 上限 1：第二次点选被忽略
    expect(screen.getByText(/已选择 1 项/)).toBeDefined()
  })

  it('再次点选已选项 → 取消选择', () => {
    renderPanel(makeSession({ allowMultiple: true }))
    const cards = screen.getAllByText(/方案A/)
    fireEvent.click(cards[0])
    expect(screen.getByText(/已选择 1 项/)).toBeDefined()
    fireEvent.click(cards[0])
    expect(screen.queryByText(/已选择 1 项/)).toBeNull()
  })
})

describe('VotingPanel: 理由与提交', () => {
  it('requireReason：理由未填提交禁用，填写后可提交并落 store', () => {
    const voting = makeSession({ requireReason: true })
    useVotingStore.setState({ votingSessions: [voting] })
    renderPanel(voting)
    const submit = screen.getByRole('button', { name: /提交投票/ })
    fireEvent.click(screen.getAllByText(/方案A/)[0])
    // 已选中但理由未填 → 仍禁用
    expect(submit).toHaveProperty('disabled', true)
    const textarea = screen.getByPlaceholderText('请说明你选择该方案的理由...')
    fireEvent.change(textarea, { target: { value: '性能更好' } })
    expect(submit).toHaveProperty('disabled', false)
    fireEvent.click(submit)
    const stored = useVotingStore.getState().votingSessions.find((v) => v.id === 'v-1')
    expect(stored?.options.find((o) => o.id === 'opt-1')?.voteCount).toBe(1)
    expect(stored?.options.find((o) => o.id === 'opt-1')?.voters?.[0]?.reason).toBe('性能更好')
  })

  it('非必填：默认折叠理由入口，点击展开', () => {
    renderPanel(makeSession())
    expect(screen.queryByPlaceholderText('请说明你选择该方案的理由...')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /附上理由/ }))
    expect(screen.getByPlaceholderText('请说明你选择该方案的理由...')).toBeDefined()
  })

  it('store 无此 session → 提交失败显示错误文案', () => {
    useVotingStore.setState({ votingSessions: [] })
    renderPanel(makeSession())
    fireEvent.click(screen.getAllByText(/方案A/)[0])
    fireEvent.click(screen.getByRole('button', { name: /提交投票/ }))
    expect(screen.getByText('投票不存在')).toBeDefined()
  })
})

describe('VotingPanel: 详情展开', () => {
  it('有 details 的选项可展开显示富内容', () => {
    renderPanel(
      makeSession({
        options: [
          makeOption({ id: 'opt-1', title: '方案A', details: '### 详细设计\n支持 X/Y/Z' }),
        ],
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: /查看详情/ }))
    expect(screen.getByText(/详细设计/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: /收起详情/ }))
    expect(screen.queryByText(/详细设计/)).toBeNull()
  })
})
