/**
 * 会话编辑 / 新建模态框组件
 *
 * 新建和编辑复用同一个组件：
 * - mode="edit"：需要传入 session，打开时填入当前标题和 Agent
 * - mode="create"：session 为 null，打开时填入默认值（标题空，Agent 默认灵汐）
 *
 * 工作空间/隔离等字段由插件贡献（contributes.thread_fields 聚合 schema）驱动：
 * 打开时拉取 /threads/schema，按字段类型渲染——新增插件字段无需改前端代码。
 */

import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { Loader2, Plus } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { RjsfForm } from '@/services/schema/RjsfForm'
import { useAgentStore } from '@/stores/agentStore'
import type { Session } from '@/types'
import type { UIInputFormField } from '@/types/schema'
import { getThreadSchema, type ThreadField } from '@/services/api/session'

/** 新建会话的工作空间与隔离模式选项 */
export interface SessionCreateOptions {
  /** 会话工作空间绝对路径（项目目录；空 = 默认目录自动生成） */
  workspace?: string
  /** 会话工作空间拓扑：worktree（默认，隔离副本）/ plain（直接操作目录） */
  workspaceMode?: 'worktree' | 'plain'
  /** 会话隔离模式：isolated（容器）/ non_isolated（宿主） */
  isolationMode?: 'isolated' | 'non_isolated'
  /** 插件贡献字段的通用值（未知字段走 metadata 透传） */
  extra?: Record<string, string>
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

/** 内置字段名（title/intent 由组件原生渲染，不参与插件字段渲染） */
const BUILTIN_FIELDS = new Set(['title', 'intent'])

/** 专属渲染字段（绑定 SessionCreateOptions 语义，保留定制 UI 与帮助文案） */
const SPECIAL_FIELDS = new Set(['workspace', 'workspaceMode', 'isolationMode'])

/**
 * 会话编辑 / 新建模态框
 */
export const SessionEditModal = memo<SessionEditModalProps>(
  ({ mode, isOpen, session, onClose, onSave, isSaving = false }) => {
    const [title, setTitle] = useState('')
    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
    const [workspace, setWorkspace] = useState('')
    const [workspaceMode, setWorkspaceMode] = useState<'worktree' | 'plain'>('worktree')
    const [isolationMode, setIsolationMode] = useState<'isolated' | 'non_isolated'>('non_isolated')
    // 插件贡献字段 schema（打开时拉取）与通用值
    const [pluginFields, setPluginFields] = useState<ThreadField[]>([])
    const [extraValues, setExtraValues] = useState<Record<string, string>>({})
    const agents = useAgentStore((state) => state.agents)

    const availableAgents = useMemo(() => {
      return agents.filter((a) => a.status === 'active')
    }, [agents])

    const defaultAgentId = useMemo(() => {
      const agentos = agents.find(
        (a) => a.configId === 'agentos' || a.name === '灵汐',
      )
      return agentos?.configId || agentos?.id || null
    }, [agents])

    useEffect(() => {
      if (isOpen) {
        // 拉取插件贡献的线程创建字段 schema（失败降级为空，不阻断表单）
        getThreadSchema()
          .then((fields) => {
            setPluginFields(fields.filter((f) => !BUILTIN_FIELDS.has(f.name)))
            // 初始化通用字段默认值（select 取首个选项）
            const next: Record<string, string> = {}
            for (const f of fields) {
              if (BUILTIN_FIELDS.has(f.name)) continue
              if (f.name === 'workspace' || f.name === 'workspaceMode' || f.name === 'isolationMode') continue
              next[f.name] = f.options?.[0]?.value ?? ''
            }
            setExtraValues(next)
          })
          .catch(() => setPluginFields([]))
        if (mode === 'edit' && session) {
          setTitle(session.title || '')
          setSelectedAgentId(session.agentId || defaultAgentId)
          setWorkspace(session.workspace || '')
          setWorkspaceMode('worktree')
          setIsolationMode(session.isolationMode || 'non_isolated')
        } else {
          setTitle('新会话')
          setSelectedAgentId(defaultAgentId)
          setWorkspace('')
          setWorkspaceMode('worktree')
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
          // 拓扑/隔离与工作空间填写解耦：未填空间时按默认目录自动生成后同样生效
          workspaceMode,
          isolationMode,
          extra: Object.keys(extraValues).length > 0 ? extraValues : undefined,
        },
      )
    }, [mode, session, title, selectedAgentId, workspace, workspaceMode, isolationMode, extraValues, onSave])

    const isCreate = mode === 'create'

    /** 专属字段（workspace/workspaceMode/isolationMode）：绑定本地 state 与 SessionCreateOptions */
    const specialFields = pluginFields.filter((f) => SPECIAL_FIELDS.has(f.name))

    /** 通用插件字段：统一表单核心渲染，值经 onChange 流入 extraValues（metadata 透传） */
    const genericFields: UIInputFormField[] = pluginFields
      .filter((f) => !SPECIAL_FIELDS.has(f.name))
      .map((f) => ({
        name: f.name,
        type: (f.type || 'string') as UIInputFormField['type'],
        label: f.label || f.name,
        description: f.description,
        options: f.options,
      }))

    /** 渲染单个专属字段 */
    const renderSpecialField = (field: ThreadField) => {
      const label = field.label || field.name

      if (field.name === 'workspace') {
        return (
          <div key={field.name}>
            <label className="text-foreground mb-1 block text-sm font-medium">
              {label} <span className="text-muted-foreground">（可选，项目目录）</span>
            </label>
            <input
              type="text"
              value={workspace}
              onChange={(e) => setWorkspace(e.target.value)}
              className="bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
              placeholder={field.description || '如 D:/myproject/demo-app'}
            />
            <p className="text-muted-foreground mt-1 text-xs">
              留空则按默认目录自动生成；填写的目录需已存在
            </p>
          </div>
        )
      }

      if (field.name === 'workspaceMode') {
        return (
          <div key={field.name}>
            <label className="text-foreground mb-1 block text-sm font-medium">{label}</label>
            <select
              value={workspaceMode}
              onChange={(e) => setWorkspaceMode(e.target.value as 'worktree' | 'plain')}
              className="bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
            >
              {(field.options || []).map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <p className="text-muted-foreground mt-1 text-xs">
              worktree 在目标项目上建隔离副本（默认）；plain 直接操作目标目录。与隔离模式相互独立
            </p>
          </div>
        )
      }

      return (
        <div key={field.name}>
          <label className="text-foreground mb-1 block text-sm font-medium">{label}</label>
          <select
            value={isolationMode}
            onChange={(e) => setIsolationMode(e.target.value as 'isolated' | 'non_isolated')}
            className="bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors"
          >
            {(field.options || []).map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <p className="text-muted-foreground mt-1 text-xs">
            执行环境（容器/宿主）。不依赖工作空间填写——未填时自动生成的空间同样按此隔离
          </p>
        </div>
      )
    }

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
                {availableAgents.map((agent, index) => (
                  <option
                    key={`${agent.configId || agent.id}-${index}`}
                    value={agent.configId || agent.id}
                  >
                    {agent.name}
                  </option>
                ))}
              </select>
            </div>

            {/* 插件贡献字段：专属字段定制渲染，通用字段统一表单核心渲染 */}
            {specialFields.map((field) => renderSpecialField(field))}
            {genericFields.length > 0 && (
              <RjsfForm
                fields={genericFields}
                initialValues={extraValues}
                onChange={(values) => setExtraValues(values as Record<string, string>)}
              />
            )}
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
