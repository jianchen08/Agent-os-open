/**
 * 投票状态管理 Store
 *
 * 管理多方案投票会话的创建、投票、关闭和结果统计。
 */

import { create } from 'zustand'
import type { VotingOption, VotingSession, VotingStatus } from '@/types/voting'

/** 自动递增 ID */
let _nextVotingId = 1

function generateVotingId(): string {
  return `vote-${Date.now()}-${_nextVotingId++}`
}

interface VotingState {
  /** 活跃的投票会话列表 */
  votingSessions: VotingSession[]
  /** 当前展开的投票 ID */
  expandedVotingId: string | null

  // ---- Actions ----

  /** 创建投票会话 */
  createVoting: (data: Omit<VotingSession, 'id' | 'status' | 'createdAt' | 'options'> & {
    options: Array<Omit<VotingOption, 'voteCount' | 'hasVoted' | 'voters'>>
  }) => string
  /** 提交投票 */
  submitVote: (votingId: string, selectedOptionIds: string[], reason?: string) => { success: boolean; error?: string }
  /** 关闭投票 */
  closeVoting: (votingId: string, winnerId?: string) => void
  /** 取消投票 */
  cancelVoting: (votingId: string) => void
  /** 切换投票面板展开/折叠 */
  toggleExpand: (votingId: string) => void
  /** 获取指定 ID 的投票会话 */
  getVotingById: (votingId: string) => VotingSession | undefined
  /** 获取指定 Tab 的活跃投票 */
  getActiveVotingsForTab: (tabId: string) => VotingSession[]
  /** 移除投票会话 */
  removeVoting: (votingId: string) => void
  /** 清空所有投票 */
  clearAll: () => void
}

export const useVotingStore = create<VotingState>()((set, get) => ({
  votingSessions: [],
  expandedVotingId: null,

  createVoting: (data) => {
    const id = generateVotingId()
    const options: VotingOption[] = data.options.map((opt, idx) => ({
      ...opt,
      id: opt.id ?? `opt-${id}-${idx}`,
      voteCount: 0,
      hasVoted: false,
      voters: [],
    }))

    const session: VotingSession = {
      ...data,
      id,
      options,
      status: 'open',
      createdAt: new Date().toISOString(),
    }

    set((state) => ({
      votingSessions: [...state.votingSessions, session],
    }))

    return id
  },

  submitVote: (votingId, selectedOptionIds, reason) => {
    const state = get()
    const session = state.votingSessions.find((v) => v.id === votingId)

    if (!session) {
      return { success: false, error: '投票不存在' }
    }

    if (session.status !== 'open') {
      return { success: false, error: '投票已关闭' }
    }

    // 多选验证
    if (!session.allowMultiple && selectedOptionIds.length > 1) {
      return { success: false, error: '此投票不支持多选' }
    }

    if (session.maxSelections && selectedOptionIds.length > session.maxSelections) {
      return { success: false, error: `最多选择 ${session.maxSelections} 个方案` }
    }

    // 理由验证
    if (session.requireReason && !reason?.trim()) {
      return { success: false, error: '请填写投票理由' }
    }

    // 验证选项 ID 有效
    const validIds = selectedOptionIds.filter((id) =>
      session.options.some((o) => o.id === id),
    )
    if (validIds.length !== selectedOptionIds.length) {
      return { success: false, error: '包含无效的方案 ID' }
    }

    // 更新投票数据
    set((state) => ({
      votingSessions: state.votingSessions.map((v) => {
        if (v.id !== votingId) return v

        const updatedOptions = v.options.map((opt) => {
          const isSelected = selectedOptionIds.includes(opt.id)
          if (!isSelected) return opt

          return {
            ...opt,
            voteCount: opt.voteCount + 1,
            hasVoted: true,
            voters: [
              ...(opt.voters || []),
              {
                userId: 'current-user',
                userName: '我',
                reason: reason?.trim() || undefined,
                votedAt: new Date().toISOString(),
              },
            ],
          }
        })

        return { ...v, options: updatedOptions }
      }),
    }))

    return { success: true }
  },

  closeVoting: (votingId, winnerId) => {
    set((state) => ({
      votingSessions: state.votingSessions.map((v) => {
        if (v.id !== votingId) return v

        const totalVoters = Math.max(
          ...v.options.map((o) => o.voteCount),
          1,
        )
        const totalVotes = v.options.reduce((sum, o) => sum + o.voteCount, 0)

        return {
          ...v,
          status: 'closed' as VotingStatus,
          closedAt: new Date().toISOString(),
          result: {
            winnerId,
            totalVoters,
            optionResults: v.options.map((o) => ({
              optionId: o.id,
              voteCount: o.voteCount,
              percentage: totalVotes > 0 ? Math.round((o.voteCount / totalVotes) * 100) : 0,
            })),
          },
        }
      }),
    }))
  },

  cancelVoting: (votingId) => {
    set((state) => ({
      votingSessions: state.votingSessions.map((v) =>
        v.id === votingId ? { ...v, status: 'cancelled' as VotingStatus } : v,
      ),
    }))
  },

  toggleExpand: (votingId) => {
    set((state) => ({
      expandedVotingId:
        state.expandedVotingId === votingId ? null : votingId,
    }))
  },

  getVotingById: (votingId) => {
    return get().votingSessions.find((v) => v.id === votingId)
  },

  getActiveVotingsForTab: (tabId) => {
    return get().votingSessions.filter(
      (v) => v.tabId === tabId && v.status === 'open',
    )
  },

  removeVoting: (votingId) => {
    set((state) => ({
      votingSessions: state.votingSessions.filter((v) => v.id !== votingId),
      expandedVotingId:
        state.expandedVotingId === votingId ? null : state.expandedVotingId,
    }))
  },

  clearAll: () => {
    set({ votingSessions: [], expandedVotingId: null })
  },
}))
