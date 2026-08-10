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

/** 新建会话的工作空间与隔离模式选项 */
export interface SessionCreateOptions {
  /** 会话工作空间绝对路径（项目目录） */
  workspace?: string
  /** 会话隔离模式：isolated（容器）/ non_isolated（宿主+审批） */
  isolationMode?: 'isolated' | 'non_isolated'
}

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
  onSave: (
    sessionId: string | null,
    title: string,
    agentId: string | null,
    options?: SessionCreateOptions,
  ) => void
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
    const [workspace, setWorkspace] = useState('')
    const [isolationMode, setIsolationMode] = useState<'isolated' | 'non_isolated'>('non_isolated')
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
          setWorkspace(session.workspace || '')
          setIsolationMode(session.isolationMode || 'non_isolated')
        } else {
          setTitle('新会话')
          setSelectedAgentId(defaultAgentId)
          setWorkspace('')
          setIsolationMode('non_isolated')
        }
      }
    }, [isOpen, mode, session, defaultAgentId])

    const handleSave = useCallback(() => {
      if (mode === 'edit' && (!session || !title.trim())) return
      onSave(
        session?.id || null,
        title.trim() || '新会话',
        selectedAgentId,
        {
          workspace: workspace.trim() || undefined,
          isolationMode: workspace.trim() ? isolationMode : undefined,
        },
      )
    }, [mode, session, title, selectedAgentId, workspace, isolationMode, onSave])

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

            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">
                工作空间 <span className="text-muted-foreground">（可选，项目目录）</span>
              </label>
              <input
                type="text"
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                className="bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
                placeholder="如 D:/myproject/demo-app"
              />
              <p className="text-muted-foreground mt-1 text-xs">
                留空则使用系统默认目录；填写的目录需已存在，AI 的所有操作将在此目录内进行
              </p>
            </div>

            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">隔离模式</label>
              <select
                value={isolationMode}
                onChange={(e) =>
                  setIsolationMode(e.target.value as 'isolated' | 'non_isolated')
                }
                disabled={!workspace.trim()}
                className="bg-muted/50 border-border/50 focus:border-primary disabled:opacity-50 w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
              >
                <option value="non_isolated">普通模式（读取放行，危险操作需审批）</option>
                <option value="isolated">容器隔离（命令在 Docker 容器中执行）</option>
              </select>
              <p className="text-muted-foreground mt-1 text-xs">
                需填写工作空间后可选容器隔离
              </p>
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
