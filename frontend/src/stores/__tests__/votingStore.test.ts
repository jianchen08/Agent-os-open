/**
 * votingStore 单测：投票会话创建/投票/关闭/取消/展开/查询
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { useVotingStore } from '../votingStore'
import type { VotingSession } from '@/types/voting'

function createSession(overrides: Partial<VotingSession> = {}): VotingSession {
  return {
    id: 'vote-1',
    title: '方案选择',
    agentId: 'agent-a',
    options: [
      { id: 'opt-a', title: '方案A', voteCount: 0, hasVoted: false, voters: [] },
      { id: 'opt-b', title: '方案B', voteCount: 0, hasVoted: false, voters: [] },
    ],
    status: 'open',
    allowMultiple: false,
    requireReason: false,
    createdAt: '2026-08-27T00:00:00.000Z',
    ...overrides,
  }
}

describe('votingStore', () => {
  beforeEach(() => {
    useVotingStore.getState().clearAll()
  })

  describe('createVoting', () => {
    it('创建会话：自动补 id/status/createdAt，选项初始化 voteCount=0', () => {
      const id = useVotingStore.getState().createVoting({
        title: '方案选择',
        agentId: 'agent-a',
        tabId: 'tab-1',
        allowMultiple: false,
        requireReason: false,
        options: [{ id: 'opt-a', title: '方案A' }, { title: '方案B' }],
      })

      const session = useVotingStore.getState().getVotingById(id)
      expect(session).toBeDefined()
      expect(session!.status).toBe('open')
      expect(session!.tabId).toBe('tab-1')
      expect(session!.options).toHaveLength(2)
      expect(session!.options[0]).toMatchObject({ id: 'opt-a', voteCount: 0, hasVoted: false })
      // 未提供 id 的选项自动生成
      expect(session!.options[1].id).toMatch(/^opt-/)
      expect(session!.createdAt).toBeTruthy()
    })

    it('多次创建生成不同 id', () => {
      const s = useVotingStore.getState()
      const id1 = s.createVoting({ title: 't1', agentId: 'a', allowMultiple: false, requireReason: false, options: [] })
      const id2 = s.createVoting({ title: 't2', agentId: 'a', allowMultiple: false, requireReason: false, options: [] })
      expect(id1).not.toBe(id2)
      expect(useVotingStore.getState().votingSessions).toHaveLength(2)
    })
  })

  describe('submitVote', () => {
    it('投票成功：票数 +1、hasVoted、voters 记录理由与时间', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({
        title: '方案选择',
        agentId: 'a',
        allowMultiple: false,
        requireReason: true,
        options: [{ id: 'opt-a', title: 'A' }, { id: 'opt-b', title: 'B' }],
      })

      const result = useVotingStore.getState().submitVote(id, ['opt-a'], ' 理由 ')
      expect(result).toEqual({ success: true })

      const session = useVotingStore.getState().getVotingById(id)!
      expect(session.options[0].voteCount).toBe(1)
      expect(session.options[0].hasVoted).toBe(true)
      expect(session.options[0].voters![0]).toMatchObject({
        userId: 'current-user',
        userName: '我',
        reason: '理由',
      })
      expect(session.options[1].voteCount).toBe(0)
    })

    it('投票不存在 → 失败', () => {
      const result = useVotingStore.getState().submitVote('nope', ['opt-a'])
      expect(result).toEqual({ success: false, error: '投票不存在' })
    })

    it('已关闭的投票 → 失败', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: false, options: [{ id: 'o1', title: 'O' }] })
      s.closeVoting(id)
      const result = useVotingStore.getState().submitVote(id, ['o1'])
      expect(result).toEqual({ success: false, error: '投票已关闭' })
    })

    it('单选投票提交多选 → 失败', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: false, options: [{ id: 'o1', title: 'O' }, { id: 'o2', title: 'O2' }] })
      const result = useVotingStore.getState().submitVote(id, ['o1', 'o2'])
      expect(result).toEqual({ success: false, error: '此投票不支持多选' })
    })

    it('超过 maxSelections → 失败', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: true, maxSelections: 1, requireReason: false, options: [{ id: 'o1', title: 'O' }, { id: 'o2', title: 'O2' }] })
      const result = useVotingStore.getState().submitVote(id, ['o1', 'o2'])
      expect(result).toEqual({ success: false, error: '最多选择 1 个方案' })
    })

    it('requireReason 且理由为空 → 失败', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: true, options: [{ id: 'o1', title: 'O' }] })
      const result = useVotingStore.getState().submitVote(id, ['o1'], '   ')
      expect(result).toEqual({ success: false, error: '请填写投票理由' })
    })

    it('包含无效选项 ID → 失败', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: true, requireReason: false, options: [{ id: 'o1', title: 'O' }] })
      const result = useVotingStore.getState().submitVote(id, ['o1', 'ghost'])
      expect(result).toEqual({ success: false, error: '包含无效的方案 ID' })
    })

    it('多选投票可投多个有效选项', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: true, requireReason: false, options: [{ id: 'o1', title: 'O' }, { id: 'o2', title: 'O2' }] })
      const result = useVotingStore.getState().submitVote(id, ['o1', 'o2'])
      expect(result.success).toBe(true)
      const session = useVotingStore.getState().getVotingById(id)!
      expect(session.options.every((o) => o.voteCount === 1)).toBe(true)
    })
  })

  describe('closeVoting', () => {
    it('关闭后 status=closed、closedAt 落库、result 含百分比', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: false, options: [{ id: 'o1', title: 'O' }, { id: 'o2', title: 'O2' }] })
      s.submitVote(id, ['o1'])
      s.submitVote(id, ['o1'])
      s.submitVote(id, ['o2'])

      s.closeVoting(id, 'o1')

      const session = useVotingStore.getState().getVotingById(id)!
      expect(session.status).toBe('closed')
      expect(session.closedAt).toBeTruthy()
      // 现状契约：totalVoters = 各选项 voteCount 的最大值（非去重投票人数）
      expect(session.result).toEqual({
        winnerId: 'o1',
        totalVoters: 2,
        optionResults: [
          { optionId: 'o1', voteCount: 2, percentage: 67 },
          { optionId: 'o2', voteCount: 1, percentage: 33 },
        ],
      })
    })

    it('零票关闭：totalVoters 兜底 1、百分比为 0', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: false, options: [{ id: 'o1', title: 'O' }] })
      s.closeVoting(id)
      const session = useVotingStore.getState().getVotingById(id)!
      expect(session.result!.totalVoters).toBe(1)
      expect(session.result!.optionResults[0].percentage).toBe(0)
    })
  })

  describe('cancelVoting / toggleExpand / removeVoting / 查询', () => {
    it('cancelVoting 置 status=cancelled', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: false, options: [] })
      s.cancelVoting(id)
      expect(useVotingStore.getState().getVotingById(id)!.status).toBe('cancelled')
    })

    it('toggleExpand 展开/收起切换', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: false, options: [] })
      s.toggleExpand(id)
      expect(useVotingStore.getState().expandedVotingId).toBe(id)
      s.toggleExpand(id)
      expect(useVotingStore.getState().expandedVotingId).toBeNull()
    })

    it('getActiveVotingsForTab 只返回该 tab 的 open 会话', () => {
      const s = useVotingStore.getState()
      const openId = s.createVoting({ title: 't1', agentId: 'a', tabId: 'tab-1', allowMultiple: false, requireReason: false, options: [] })
      s.createVoting({ title: 't2', agentId: 'a', tabId: 'tab-2', allowMultiple: false, requireReason: false, options: [] })
      s.createVoting({ title: 't3', agentId: 'a', tabId: 'tab-1', allowMultiple: false, requireReason: false, options: [] })
      s.closeVoting(openId)

      const active = useVotingStore.getState().getActiveVotingsForTab('tab-1')
      expect(active).toHaveLength(1)
      expect(active[0].title).toBe('t3')
    })

    it('removeVoting 移除会话并收起展开项', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: false, options: [] })
      s.toggleExpand(id)
      s.removeVoting(id)
      const state = useVotingStore.getState()
      expect(state.getVotingById(id)).toBeUndefined()
      expect(state.expandedVotingId).toBeNull()
    })

    it('clearAll 清空会话与展开项', () => {
      const s = useVotingStore.getState()
      const id = s.createVoting({ title: 't', agentId: 'a', allowMultiple: false, requireReason: false, options: [] })
      s.toggleExpand(id)
      s.clearAll()
      const state = useVotingStore.getState()
      expect(state.votingSessions).toEqual([])
      expect(state.expandedVotingId).toBeNull()
    })
  })
})
