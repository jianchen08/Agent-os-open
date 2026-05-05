/**
 * 新建会话模态框组件
 *
 * 允许用户选择 Agent 并创建新会话。
 * 支持自定义会话标题（可选），以及从 Agent 列表中选择绑定的 Agent。
 */

import { Loader2, MessageSquare, Plus } from 'lucide-react'
import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { useAgentStore } from '@/stores/agentStore'

interface NewSessionModalProps {
  /** 是否打开模态框 */
  isOpen: boolean
  /** 关闭模态框回调 */
  onClose: () => void
  /** 确认创建回调，参数为 agentId 和可选标题 */
  onConfirm: (agentId: string | null, title?: string) => void
  /** 是否正在创建中 */
  isCreating: boolean
}

/**
 * 新建会话模态框
 * 提供 Agent 选择列表和可选的标题输入
 */
export const NewSessionModal = memo<NewSessionModalProps>(
  ({ isOpen, onClose, onConfirm, isCreating }) => {
    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
    const agents = useAgentStore((state) => state.agents)

    /**
     * 过滤出可用的主 Agent（level=1 或无 level 标记的 Agent）
     */
    const availableAgents = useMemo(() => {
      return agents.filter((a) => a.status === 'active')
    }, [agents])

    /**
     * 重置选择状态
     */
    useEffect(() => {
      if (!isOpen) {
        setSelectedAgentId(null)
      }
    }, [isOpen])

    /**
     * 处理确认创建
     */
    const handleConfirm = useCallback(() => {
      onConfirm(selectedAgentId)
    }, [onConfirm, selectedAgentId])

    return (
      <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
        <DialogContent className="max-w-[400px]">
          <DialogHeader>
            <DialogTitle>新建会话</DialogTitle>
            <DialogDescription>选择一个 Agent 开始新的对话</DialogDescription>
          </DialogHeader>

          {/* Agent 选择列表 */}
          <div className="max-h-[300px] overflow-y-auto px-1">
            {availableAgents.length === 0 ? (
              <div className="text-muted-foreground py-6 text-center text-sm">
                暂无可用的 Agent
              </div>
            ) : (
              <div className="space-y-1">
                {availableAgents.map((agent) => (
                  <button
                    key={agent.id}
                    onClick={() => setSelectedAgentId(agent.id)}
                    className={cn(
                      'hover:bg-accent flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors',
                      selectedAgentId === agent.id && 'bg-accent text-accent-foreground',
                    )}
                  >
                    <MessageSquare className="text-muted-foreground h-4 w-4 flex-shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-medium">{agent.name}</div>
                      {agent.description && (
                        <div className="text-muted-foreground truncate text-xs">
                          {agent.description}
                        </div>
                      )}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onClose} disabled={isCreating}>
              取消
            </Button>
            <Button size="sm" onClick={handleConfirm} disabled={isCreating}>
              {isCreating ? (
                <>
                  <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                  创建中...
                </>
              ) : (
                <>
                  <Plus className="mr-1 h-3.5 w-3.5" />
                  创建
                </>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  },
)

NewSessionModal.displayName = 'NewSessionModal'
