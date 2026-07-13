/**
 * VotingPanel - 多方案投票面板组件
 *
 * 功能：
 * - 多方案对比展示（卡片式布局）
 * - 支持多选投票
 * - 可附理由说明
 * - 投票结果可视化
 * - 方案详情展开/折叠
 */

import { BarChart3, Check, ChevronDown, ChevronUp, Clock, MessageSquare, Send, ThumbsUp, X } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useVotingStore } from '@/stores/votingStore'
import { MarkdownRenderer } from './markdown/MarkdownRenderer'
import type { VotingOption, VotingSession } from '@/types/voting'

/** 单个方案卡片 */
function OptionCard({
  option,
  isSelected,
  isVoted,
  canVote,
  showResult,
  maxVotes,
  onSelect,
}: {
  option: VotingOption
  isSelected: boolean
  isVoted: boolean
  canVote: boolean
  showResult: boolean
  maxVotes: number
  onSelect: (id: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const hasDetails = !!option.details
  const percentage = maxVotes > 0 ? Math.round((option.voteCount / maxVotes) * 100) : 0

  return (
    <div
      className={cn(
        'rounded-xl border-2 transition-all duration-200',
        isSelected
          ? 'border-primary bg-primary/5 shadow-sm'
          : 'border-border/50 bg-background hover:border-border',
        isVoted && !isSelected && 'opacity-60',
      )}
    >
      {/* 选择区 */}
      <div
        className="flex items-start gap-3 p-3 cursor-pointer"
        onClick={() => canVote && onSelect(option.id)}
      >
        {/* 选中指示器 */}
        <div
          className={cn(
            'mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full border-2 transition-all',
            isSelected
              ? 'border-primary bg-primary text-primary-foreground'
              : 'border-muted-foreground/30',
          )}
        >
          {isSelected && <Check className="h-3 w-3" />}
        </div>

        {/* 方案内容 */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold">{option.title}</span>
            {option.tag && (
              <span
                className={cn(
                  'text-[10px] px-1.5 py-0.5 rounded-full font-medium',
                  option.tagColor ?? 'bg-primary/10 text-primary',
                )}
              >
                {option.tag}
              </span>
            )}
          </div>
          {option.description && (
            <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
              {option.description}
            </p>
          )}
        </div>

        {/* 票数 */}
        {showResult && (
          <div className="flex items-center gap-1 text-xs text-muted-foreground flex-shrink-0">
            <ThumbsUp className="h-3 w-3" />
            <span>{option.voteCount}</span>
          </div>
        )}
      </div>

      {/* 结果进度条 */}
      {showResult && (
        <div className="px-3 pb-2">
          <div className="h-1.5 rounded-full bg-muted overflow-hidden">
            <div
              className={cn(
                'h-full rounded-full transition-all duration-500',
                isSelected ? 'bg-primary' : 'bg-muted-foreground/30',
              )}
              style={{ width: `${percentage}%` }}
            />
          </div>
          <div className="flex justify-between mt-1">
            <span className="text-[10px] text-muted-foreground">{percentage}%</span>
            <span className="text-[10px] text-muted-foreground">{option.voteCount} 票</span>
          </div>
        </div>
      )}

      {/* 详情展开区 */}
      {hasDetails && (
        <>
          <button
            className="w-full flex items-center justify-center gap-1 py-1.5 text-xs text-muted-foreground hover:text-primary transition-colors border-t border-border/30"
            onClick={(e) => {
              e.stopPropagation()
              setExpanded(!expanded)
            }}
          >
            {expanded ? (
              <>
                <ChevronUp className="h-3 w-3" /> 收起详情
              </>
            ) : (
              <>
                <ChevronDown className="h-3 w-3" /> 查看详情
              </>
            )}
          </button>
          {expanded && (
            <div className="px-4 pb-3 text-sm text-muted-foreground border-t border-border/30 pt-2">
              <MarkdownRenderer content={option.details!} />
            </div>
          )}
        </>
      )}

      {/* 投票者列表 */}
      {isVoted && option.voters && option.voters.length > 0 && (
        <div className="px-3 pb-3 border-t border-border/30 pt-2">
          <div className="space-y-1">
            {option.voters.map((voter, idx) => (
              <div key={idx} className="flex items-start gap-2 text-xs">
                <span className="font-medium">{voter.userName}</span>
                {voter.reason && (
                  <span className="text-muted-foreground flex-1">: {voter.reason}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/** 投票面板主组件 */
export interface VotingPanelProps {
  /** 投票会话 */
  voting: VotingSession
  /** 自定义类名 */
  className?: string
}

export function VotingPanel({ voting, className }: VotingPanelProps) {
  const submitVote = useVotingStore((s) => s.submitVote)
  const closeVoting = useVotingStore((s) => s.closeVoting)
  const cancelVoting = useVotingStore((s) => s.cancelVoting)

  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [reason, setReason] = useState('')
  const [showReasonInput, setShowReasonInput] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const isOpen = voting.status === 'open'
  const isClosed = voting.status === 'closed'
  const hasVoted = voting.options.some((o) => o.hasVoted)
  const maxVotes = Math.max(...voting.options.map((o) => o.voteCount), 1)
  const showResult = isClosed || hasVoted

  /** 已选择的方案数量 */
  const selectedCount = selectedIds.size

  /** 切换选择 */
  const handleSelect = useCallback(
    (optionId: string) => {
      if (!isOpen || hasVoted) return

      setSelectedIds((prev) => {
        const next = new Set(prev)
        if (next.has(optionId)) {
          next.delete(optionId)
        } else {
          // 单选模式下替换
          if (!voting.allowMultiple) {
            return new Set([optionId])
          }
          // 多选模式下检查上限
          if (voting.maxSelections && next.size >= voting.maxSelections) {
            return prev
          }
          next.add(optionId)
        }
        return next
      })
    },
    [isOpen, hasVoted, voting.allowMultiple, voting.maxSelections],
  )

  /** 提交投票 */
  const handleSubmit = useCallback(() => {
    if (selectedIds.size === 0) return

    const result = submitVote(
      voting.id,
      Array.from(selectedIds),
      reason.trim() || undefined,
    )

    if (!result.success) {
      setSubmitError(result.error ?? '投票失败')
      return
    }

    setSubmitError(null)
    setReason('')
    setSelectedIds(new Set())
  }, [selectedIds, reason, submitVote, voting.id])

  // 格式化截止时间
  const deadlineStr = voting.deadline
    ? new Date(voting.deadline).toLocaleString('zh-CN', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    : null

  // 获取获胜方案
  const winnerOption = useMemo(() => {
    if (!voting.result?.winnerId) return null
    return voting.options.find((o) => o.id === voting.result!.winnerId) ?? null
  }, [voting.result, voting.options])

  return (
    <div
      className={cn(
        'mx-4 my-3 rounded-xl border transition-colors overflow-hidden',
        isOpen
          ? 'border-primary/40 bg-primary/5 shadow-md shadow-primary/10'
          : 'border-border/50 bg-muted/30',
        className,
      )}
      data-testid={`voting-panel-${voting.id}`}
    >
      {/* 标题区 */}
      <div className="border-b border-border/30 px-4 py-3">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-4 w-4 text-primary" />
          <span className="text-sm font-semibold">{voting.title}</span>
          <span
            className={cn(
              'text-[10px] px-1.5 py-0.5 rounded-full font-medium',
              isOpen
                ? 'bg-green-500/10 text-green-600'
                : isClosed
                  ? 'bg-gray-500/10 text-gray-500'
                  : 'bg-red-500/10 text-red-500',
            )}
          >
            {isOpen ? '投票中' : isClosed ? '已结束' : '已取消'}
          </span>
          {voting.allowMultiple && isOpen && (
            <span className="text-[10px] text-muted-foreground bg-muted px-1.5 py-0.5 rounded-full">
              多选{voting.maxSelections ? ` (最多${voting.maxSelections}项)` : ''}
            </span>
          )}
        </div>
        {voting.description && (
          <p className="text-muted-foreground text-xs mt-1">{voting.description}</p>
        )}
        {deadlineStr && (
          <div className="flex items-center gap-1 text-xs text-muted-foreground mt-1">
            <Clock className="h-3 w-3" />
            <span>截止: {deadlineStr}</span>
          </div>
        )}
      </div>

      {/* 方案列表 */}
      <div className="p-3 space-y-2">
        {voting.options.map((option) => (
          <OptionCard
            key={option.id}
            option={option}
            isSelected={selectedIds.has(option.id) || option.hasVoted}
            isVoted={option.hasVoted}
            canVote={isOpen && !hasVoted}
            showResult={showResult}
            maxVotes={maxVotes}
            onSelect={handleSelect}
          />
        ))}
      </div>

      {/* 投票结果摘要 */}
      {isClosed && voting.result && (
        <div className="px-4 pb-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <span>🏆 获胜方案:</span>
            <span className="text-primary">{winnerOption?.title ?? '无'}</span>
          </div>
          <div className="text-xs text-muted-foreground mt-1">
            共 {voting.result.totalVoters} 人参与投票
          </div>
        </div>
      )}

      {/* 投票操作区 */}
      {isOpen && !hasVoted && (
        <div className="border-t border-border/30 px-4 py-3 space-y-2">
          {/* 选择提示 */}
          {voting.allowMultiple && (
            <div className="text-xs text-muted-foreground">
              已选择 {selectedCount} 项
              {voting.maxSelections ? ` / 最多 ${voting.maxSelections} 项` : ''}
            </div>
          )}

          {/* 理由输入区 */}
          {voting.requireReason || showReasonInput ? (
            <div className="space-y-1">
              <div className="flex items-center gap-1 text-xs text-muted-foreground">
                <MessageSquare className="h-3 w-3" />
                <span>{voting.requireReason ? '请填写理由（必填）' : '投票理由（可选）'}</span>
              </div>
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="请说明你选择该方案的理由..."
                rows={2}
                className="w-full resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none transition-shadow focus:ring-1 focus:ring-primary"
              />
            </div>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="text-xs h-6"
              onClick={() => setShowReasonInput(true)}
            >
              <MessageSquare className="h-3 w-3 mr-1" />
              附上理由
            </Button>
          )}

          {/* 错误提示 */}
          {submitError && (
            <div className="text-xs text-destructive">{submitError}</div>
          )}

          {/* 提交按钮 */}
          <Button
            size="sm"
            className="w-full"
            disabled={
              selectedIds.size === 0 ||
              (voting.requireReason && !reason.trim())
            }
            onClick={handleSubmit}
          >
            <Send className="h-3.5 w-3.5 mr-1" />
            提交投票
          </Button>
        </div>
      )}

      {/* 已投票提示 */}
      {isOpen && hasVoted && (
        <div className="border-t border-border/30 px-4 py-3 flex items-center gap-2 text-xs text-green-600">
          <Check className="h-3.5 w-3.5" />
          <span>已投票</span>
        </div>
      )}
    </div>
  )
}
