/**
 * 多方案投票系统类型定义
 *
 * 支持多方案对比展示、多选投票、附理由说明
 */

/** 投票状态 */
export type VotingStatus = 'open' | 'closed' | 'cancelled'

/** 单个投票方案 */
export interface VotingOption {
  /** 方案 ID */
  id: string
  /** 方案标题 */
  title: string
  /** 方案描述（支持 Markdown） */
  description?: string
  /** 方案详情（展开后显示的完整内容） */
  details?: string
  /** 方案标签（如"推荐"、"快速"、"保守"） */
  tag?: string
  /** 方案标签颜色 */
  tagColor?: string
  /** 当前得票数 */
  voteCount: number
  /** 当前用户是否已投票 */
  hasVoted: boolean
  /** 投票用户列表（可选展示） */
  voters?: Array<{
    userId: string
    userName: string
    reason?: string
    votedAt: string
  }>
}

/** 投票会话 */
export interface VotingSession {
  /** 投票 ID */
  id: string
  /** 标题 */
  title: string
  /** 描述 */
  description?: string
  /** 发起者 Agent ID */
  agentId: string
  /** 关联的任务 ID */
  taskId?: string
  /** 关联的会话 ID */
  sessionId?: string
  /** 关联的 Tab ID */
  tabId?: string
  /** 投票方案列表 */
  options: VotingOption[]
  /** 投票状态 */
  status: VotingStatus
  /** 是否支持多选 */
  allowMultiple: boolean
  /** 最大可选数（多选时有效） */
  maxSelections?: number
  /** 是否要求填写理由 */
  requireReason: boolean
  /** 创建时间 */
  createdAt: string
  /** 截止时间（可选） */
  deadline?: string
  /** 关闭时间 */
  closedAt?: string
  /** 投票结果摘要（关闭后） */
  result?: {
    /** 获胜方案 ID */
    winnerId?: string
    /** 总投票人数 */
    totalVoters: number
    /** 各方案得票数 */
    optionResults: Array<{
      optionId: string
      voteCount: number
      percentage: number
    }>
  }
}

/** 投票请求 */
export interface SubmitVoteRequest {
  /** 投票 ID */
  votingId: string
  /** 选中的方案 ID 列表 */
  selectedOptionIds: string[]
  /** 投票理由 */
  reason?: string
}

/** 投票响应 */
export interface SubmitVoteResponse {
  success: boolean
  /** 更新后的投票会话 */
  voting: VotingSession
  /** 错误信息 */
  error?: string
}
