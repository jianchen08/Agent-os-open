/**
 * 会话编辑模态框组件
 *
 * 允许用户修改会话标题和绑定的 Agent。
 * 打开时自动填入当前会话的信息。
 */

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
import { useAgentStore } from '@/stores/agentStore'
import type { Session } from '@/types'

interface SessionEditModalProps {
  /** 是否打开模态框 */
  isOpen: boolean
  /** 当前编辑的会话（null 表示关闭） */
  session: Session | null
  /** 关闭模态框回调 */
  onClose: () => void
  /** 保存编辑回调，参数为 sessionId、新标题和新 agentId */
  onSave: (sessionId: string, title: string, agentId: string | null) => void
}

/**
 * 会话编辑模态框
 * 支持修改会话标题和绑定的 Agent
 */
export const SessionEditModal = memo<SessionEditModalProps>(
  ({ isOpen, session, onClose, onSave }) => {
    const [title, setTitle] = useState('')
    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
    const agents = useAgentStore((state) => state.agents)

    /**
     * 过滤出可用的活跃 Agent
     */
    const availableAgents = useMemo(() => {
      return agents.filter((a) => a.status === 'active')
    }, [agents])

    /**
     * 打开模态框时，初始化表单数据
     */
    useEffect(() => {
      if (isOpen && session) {
        setTitle(session.title || '')
        setSelectedAgentId(session.agentId || null)
      }
    }, [isOpen, session])

    /**
     * 处理保存操作
     */
    const handleSave = useCallback(() => {
      if (!session || !title.trim()) return
      onSave(session.id, title.trim(), selectedAgentId)
    }, [session, title, selectedAgentId, onSave])

    return (
      <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
        <DialogContent className="max-w-[400px]">
          <DialogHeader>
            <DialogTitle>编辑会话</DialogTitle>
            <DialogDescription>修改会话标题和绑定的 Agent</DialogDescription>
          </DialogHeader>

          <div className="space-y-4 px-1">
            {/* 标题输入 */}
            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">标题</label>
              <input
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
                placeholder="输入会话标题..."
                autoFocus
              />
            </div>

            {/* Agent 选择 */}
            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">绑定 Agent</label>
              <select
                value={selectedAgentId || ''}
                onChange={(e) => setSelectedAgentId(e.target.value || null)}
                className="bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
              >
                <option value="">默认 Agent</option>
                {availableAgents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onClose}>
              取消
            </Button>
            <Button size="sm" onClick={handleSave} disabled={!title.trim()}>
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  },
)

SessionEditModal.displayName = 'SessionEditModal'
