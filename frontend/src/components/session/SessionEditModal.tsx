/**
 * 会话编辑 / 新建模态框组件
 *
 * 新建和编辑复用同一个组件：
 * - mode="edit"：需要传入 session，打开时填入当前标题和 Agent
 * - mode="create"：session 为 null，打开时填入默认值（标题空，Agent 默认灵汐）
 */

import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { Loader2, Plus } from 'lucide-react'
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
  /** 模式：edit=编辑已有会话，create=新建会话 */
  mode: 'edit' | 'create'
  /** 是否打开模态框 */
  isOpen: boolean
  /** 当前编辑的会话（mode="create" 时传 null） */
  session: Session | null
  /** 关闭模态框回调 */
  onClose: () => void
  /** 保存回调 */
  onSave: (sessionId: string | null, title: string, agentId: string | null) => void
  /** 是否正在保存中 */
  isSaving?: boolean
}

/**
 * 会话编辑 / 新建模态框
 */
export const SessionEditModal = memo<SessionEditModalProps>(
  ({ mode, isOpen, session, onClose, onSave, isSaving = false }) => {
    const [title, setTitle] = useState('')
    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
    const agents = useAgentStore((state) => state.agents)

    const availableAgents = useMemo(() => {
      return agents.filter((a) => a.status === 'active')
    }, [agents])

    const defaultAgentId = useMemo(() => {
      const lingxi = agents.find(
        (a) => a.configId === 'lingxi' || a.name === '灵汐',
      )
      return lingxi?.configId || lingxi?.id || null
    }, [agents])

    useEffect(() => {
      if (isOpen) {
        if (mode === 'edit' && session) {
          setTitle(session.title || '')
          setSelectedAgentId(session.agentId || defaultAgentId)
        } else {
          setTitle('新会话')
          setSelectedAgentId(defaultAgentId)
        }
      }
    }, [isOpen, mode, session, defaultAgentId])

    const handleSave = useCallback(() => {
      if (mode === 'edit' && (!session || !title.trim())) return
      onSave(session?.id || null, title.trim() || '新会话', selectedAgentId)
    }, [mode, session, title, selectedAgentId, onSave])

    const isCreate = mode === 'create'

    return (
      <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
        <DialogContent className="max-w-[400px]">
          <DialogHeader>
            <DialogTitle>{isCreate ? '新建会话' : '编辑会话'}</DialogTitle>
            <DialogDescription>
              {isCreate
                ? '输入标题并选择一个 Agent 开始新的对话'
                : '修改会话标题和绑定的 Agent'}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 px-1">
            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">标题</label>
              <input
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
                placeholder={isCreate ? '输入会话标题（可选）...' : '输入会话标题...'}
                autoFocus
              />
            </div>

            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">Agent</label>
              <select
                value={selectedAgentId || ''}
                onChange={(e) => setSelectedAgentId(e.target.value || null)}
                className="bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
              >
                <option value="">默认 Agent</option>
                {availableAgents.map((agent) => (
                  <option key={agent.configId || agent.id} value={agent.configId || agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onClose} disabled={isSaving}>
              取消
            </Button>
            <Button size="sm" onClick={handleSave} disabled={isCreate ? isSaving : (!title.trim() || isSaving)}>
              {isSaving ? (
                <>
                  <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                  {isCreate ? '创建中...' : '保存中...'}
                </>
              ) : isCreate ? (
                <>
                  <Plus className="mr-1 h-3.5 w-3.5" />
                  创建
                </>
              ) : (
                '保存'
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  },
)

SessionEditModal.displayName = 'SessionEditModal'
