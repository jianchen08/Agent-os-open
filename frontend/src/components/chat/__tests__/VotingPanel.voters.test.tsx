/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 投票面板「投票者名单」补测：已投票选项展开投票者列表（姓名 + 理由），
 * 未投票或无人投票时不渲染该段（信息只对已参与者可见）。
 */

import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useVotingStore } from '@/stores/votingStore'
import { VotingPanel } from '../VotingPanel'
import type { VotingSession } from '@/types/voting'

function makeSession(overrides: Partial<VotingSession> = {}): VotingSession {
  return {
    id: 'v-voters',
    title: '选择实现方案',
    agentId: 'agent-1',
    options: [],
    status: 'open',
    allowMultiple: false,
    requireReason: false,
    ...overrides,
  }
}

beforeEach(() => {
  useVotingStore.setState({ votingSessions: [], expandedVotingId: null })
})

describe('VotingPanel — 投票者名单', () => {
  it('已投票且有投票者 → 呈现姓名与理由', () => {
    render(
      <VotingPanel
        voting={makeSession({
          options: [
            {
              id: 'opt-1',
              title: '方案A',
              voteCount: 1,
              hasVoted: true,
              voters: [{ userName: '小林', reason: '成本更低' }],
            } as never,
          ],
        })}
      />,
    )

    expect(screen.getByText('小林')).toBeInTheDocument()
    expect(screen.getByText(': 成本更低')).toBeInTheDocument()
  })

  it('未投票 → 不出投票者段（名单只对已参与者可见）', () => {
    render(
      <VotingPanel
        voting={makeSession({
          options: [
            {
              id: 'opt-2',
              title: '方案B',
              voteCount: 0,
              hasVoted: false,
              voters: [{ userName: '小周' }],
            } as never,
          ],
        })}
      />,
    )

    expect(screen.queryByText('小周')).not.toBeInTheDocument()
  })
})
