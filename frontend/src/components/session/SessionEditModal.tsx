/**
 * 会话编辑 / 新建模态框组件
 *
 * 新建和编辑复用同一个组件：
 * - mode="edit"：需要传入 session，打开时填入当前标题和 Agent
 * - mode="create"：session 为 null，打开时填入默认值（标题空，Agent 默认灵汐）
 *
 * 除内置字段（title/intent）外，全部表单字段由插件贡献（contributes.
 * thread_fields 聚合 schema /threads/schema 声明驱动）：按 type/options
 * 通用渲染，前端不感知具体字段名——字段可来自不同插件，值的存储键
 * （x_metadata_key）、生效路径（x_execution_path）与值守卫（x_guard）都由
 * 各插件在声明里表达。新增/调整插件字段无需改前端代码。
 *
 * 保存产物：metadata 形状整包（fieldMetadata）+ 消息级 execution_context。
 * 创建走 metadata 出生落库；编辑保存为本地快照、下一次发消息以消息级
 * execution_context 生效（sessionExecutionOptions 持久层承载）。
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
import { useAgentsQuery } from '@/hooks/queries/useAgentsQuery'
import type { Session } from '@/types'
import type { UIInputFormField } from '@/types/schema'
import { getThreadSchema, type ThreadField } from '@/services/api/session'
import { loadSessionExecutionOptions as loadSessionSnapshot } from '@/services/sessionExecutionOptions'

/** 保存回调携带的插件表单产物 */
export interface SessionFormOptions {
  /**
   * 插件表单值整包：键 = 各插件声明的 x_metadata_key（缺省 name），
   * 直接并入 thread metadata / 本地快照 values 区。
   */
  fieldMetadata: Record<string, string>
  /**
   * 消息级 execution_context（按各插件 x_execution_path 组装的结构体，
   * 如 {workspace:{source_path,mode}, isolation:{level}}）；无任何声明值时缺省。
   */
  executionContext?: Record<string, unknown>
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
    options?: SessionFormOptions,
  ) => void
  /** 是否正在保存中 */
  isSaving?: boolean
}

/** 内置字段名（title/intent 由组件原生渲染，不参与插件表单渲染） */
const BUILTIN_FIELDS = new Set(['title', 'intent'])

/** 声明字段在 metadata 中的存储键（x_metadata_key 缺省 = name） */
function metadataKeyOf(field: ThreadField): string {
  return field.x_metadata_key ?? field.name
}

/** 应用插件的值守卫声明：依赖源为空时回退到声明值（如拓扑空源回退 plain） */
export function applyGuards(
  fields: ThreadField[],
  values: Record<string, string>,
): Record<string, string> {
  const out = { ...values }
  for (const f of fields) {
    if (!f.x_guard) continue
    const src = out[f.x_guard.requires]
    if (typeof src !== 'string' || src.trim() === '') {
      out[f.name] = f.x_guard.on_empty
    }
  }
  return out
}

/**
 * 把声明名值包翻译为保存产物：
 * - fieldMetadata：逐字段按 x_metadata_key 重排；
 * - executionContext：有 x_execution_path 的字段按 '.' 路径写入嵌套结构
 *   （不同插件的路径互不感知，各自落到自己的语义分支下）。
 */
export function toFormOptions(
  fields: ThreadField[],
  values: Record<string, string>,
): SessionFormOptions {
  const fieldMetadata: Record<string, string> = {}
  for (const f of fields) {
    const v = values[f.name]
    if (typeof v === 'string' && v !== '') {
      fieldMetadata[metadataKeyOf(f)] = v
    }
  }

  const ec: Record<string, unknown> = {}
  for (const f of fields) {
    if (!f.x_execution_path) continue
    const v = values[f.name]
    if (typeof v !== 'string' || v === '') continue
    const parts = f.x_execution_path.split('.')
    let node = ec
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i]
      if (typeof node[seg] !== 'object' || node[seg] === null) node[seg] = {}
      node = node[seg] as Record<string, unknown>
    }
    node[parts[parts.length - 1]] = v
  }

  return {
    fieldMetadata,
    ...(Object.keys(ec).length > 0 ? { executionContext: ec } : {}),
  }
}

/**
 * 会话编辑 / 新建模态框
 */
export const SessionEditModal = memo<SessionEditModalProps>(
  ({ mode, isOpen, session, onClose, onSave, isSaving = false }) => {
    const [title, setTitle] = useState('')
    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
    // 插件贡献字段 schema（打开时拉取）与用户改动值（初始值兜底由 initialValues 提供）
    const [pluginFields, setPluginFields] = useState<ThreadField[]>([])
    const [changedValues, setChangedValues] = useState<Record<string, string>>({})
    const { data: agents = [] } = useAgentsQuery()

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
        setChangedValues({})
        if (mode === 'edit' && session) {
          setTitle(session.title || '')
          setSelectedAgentId(session.agentId || defaultAgentId)
        } else {
          setTitle('新会话')
          setSelectedAgentId(defaultAgentId)
        }
        // 拉取插件贡献的线程创建字段 schema（失败降级为空，不阻断表单）
        getThreadSchema()
          .then((fields) => setPluginFields(fields.filter((f) => !BUILTIN_FIELDS.has(f.name))))
          .catch(() => setPluginFields([]))
      }
    }, [isOpen, mode, session, defaultAgentId])

    /**
     * 插件表单初始值（声明名值域）：编辑模式 = 本地编辑快照 values 区
     * （最新意图，键为 metadata 存储形状 → 反查回声明名）覆盖 thread
     * metadata 出生值，两者皆缺才落插件选项默认。字段集就绪前为空对象
     * ——RjsfForm 延迟到 fields 非空才挂载，保证拿到初值（其 formData 为
     * 挂载期一次性 useState 工厂）。
     */
    const initialValues = useMemo(() => {
      if (!isOpen || pluginFields.length === 0) return {}
      const snapshot =
        mode === 'edit' && session ? loadSessionSnapshot(session.id) : null
      const next: Record<string, string> = {}
      for (const f of pluginFields) {
        const storedKey = metadataKeyOf(f)
        const saved = snapshot?.values?.[storedKey] ?? session?.metadata?.[storedKey]
        next[f.name] =
          typeof saved === 'string' && saved !== ''
            ? saved
            : (f.options?.[0]?.value ?? '')
      }
      return applyGuards(pluginFields, next)
    }, [isOpen, mode, session, pluginFields])

    const handleSave = useCallback(() => {
      if (mode === 'edit' && (!session || !title.trim())) return
      const merged = applyGuards(pluginFields, {
        ...initialValues,
        ...changedValues,
      })
      onSave(session?.id || null, title.trim() || '新会话', selectedAgentId, toFormOptions(pluginFields, merged))
    }, [mode, session, title, selectedAgentId, pluginFields, initialValues, changedValues, onSave])

    const isCreate = mode === 'create'

    /** 全部插件字段统一走表单核心渲染（类型/选项/描述全部来自插件声明） */
    const formFields: UIInputFormField[] = pluginFields.map((f) => ({
      name: f.name,
      type: (f.type || 'string') as UIInputFormField['type'],
      label: f.label || f.name,
      description: f.description,
      options: f.options,
    }))

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

            {/* 插件贡献字段：声明驱动的通用渲染。key 绑定会话身份——切换编辑
                目标时强制重挂载，让 RjsfForm 重新吃最新 initialValues（其
                formData 为挂载期一次性初值）。 */}
            {formFields.length > 0 && (
              <RjsfForm
                key={`${mode}-${session?.id ?? 'new'}`}
                fields={formFields}
                initialValues={initialValues}
                onChange={(values) =>
                  setChangedValues(applyGuards(pluginFields, (values as Record<string, string>) ?? {}))
                }
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
