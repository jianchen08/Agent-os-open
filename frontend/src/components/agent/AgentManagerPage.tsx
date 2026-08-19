/**
 * AgentManagerPage 组件——agent_manager 插件的智能体管理页面（浏览+编辑一体）
 *
 * 2026-08-20 插件化：承接原 AgentsPage 内容（卡片列表 + 搜索 + 展开详情 +
 * AgentConfigModal 编辑），数据源切 /ext/agent_manager/agents（agent_manager
 * 插件 http_endpoints，原内核 /api/v1/agents 已删——ADR 2026-08-20）。
 * 经 widgetRegistry 注册为 `agents_panel`，插件 contributes.pages（path=/agents）
 * 声明入口；亦可经 PanelHostWidget 内嵌工作区页签。
 */

import { useState, useEffect, useCallback } from 'react'
import { Bot, EditIcon, RefreshCw } from '@/assets/icons'
import { AgentConfigModal } from '@/components/agent/AgentConfigModal'
import { EmptyState } from '@/components/shared/EmptyState'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { getAgents } from '@/services/api/agents'
import type { AgentResponse } from '@/services/api/agents'

/** agent_type → 中文标签 */
const AGENT_TYPE_LABELS: Record<string, string> = {
  main: '主控',
  sub: '子代理',
  atomic: '原子',
}

/**
 * Agent 管理页面组件（agent_manager 插件页面承载）
 */
export function AgentManagerPage() {
  const [agents, setAgents] = useState<AgentResponse[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [editingAgent, setEditingAgent] = useState<AgentResponse | null>(null)

  /**
   * 加载 Agent 列表（/ext/agent_manager/agents）
   */
  const fetchAgents = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await getAgents({ search: search || undefined, pageSize: 100 })
      setAgents(res.items)
      setTotal(res.total)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取 Agent 列表失败'
      setError(message)
    } finally {
      setIsLoading(false)
    }
  }, [search])

  useEffect(() => {
    fetchAgents()
  }, [fetchAgents])

  return (
    <PageShell title="智能体管理" embedded>
      <div className="mb-3 flex items-center gap-3">
        <span className="text-muted-foreground text-xs">共 {total} 个智能体</span>
        <button
          onClick={fetchAgents}
          disabled={isLoading}
          className="hover:bg-accent/50 h-8 w-8 rounded-lg border p-1.5 text-xs disabled:opacity-50 md:min-h-[44px] md:min-w-[44px]"
          aria-label="刷新智能体列表"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* 搜索（常驻） */}
      <input
        type="text"
        placeholder="搜索 Agent..."
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        aria-label="搜索智能体"
        className="bg-background focus:ring-primary mb-4 w-full max-w-md rounded-lg border px-3 py-1.5 text-sm focus:ring-1 focus:outline-none"
      />

      {/* 加载态 - 骨架屏（shared，结构与真实卡片对齐） */}
      {isLoading && <LoadingState variant="skeleton" skeletonCount={6} />}

      {/* 错误态（shared，带重试） */}
      {error && <ErrorState message={error} onRetry={fetchAgents} />}

      {/* 空状态（shared） */}
      {!isLoading && !error && agents.length === 0 && (
        <EmptyState
          icon={Bot}
          title={search ? '没有找到匹配的智能体' : '暂无智能体'}
          description={search ? undefined : '请在 config/agents/ 目录下添加 Agent 配置文件'}
        />
      )}

      {/* Agent 卡片列表 */}
      {!isLoading && !error && agents.length > 0 && (
        <div
          className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3"
          role="list"
          aria-live="polite"
          aria-label="智能体列表"
        >
          {agents.map((agent) => (
            <div
              key={agent.id}
              className="hover:bg-accent/30 cursor-pointer rounded-lg border p-4 transition-colors"
              onClick={() => setExpandedId(expandedId === agent.id ? null : agent.id)}
              role="listitem"
            >
              <div className="mb-2 flex items-start justify-between">
                <h3 className="mr-2 flex-1 truncate text-sm font-semibold">{agent.name}</h3>
                <div className="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      setEditingAgent(agent)
                    }}
                    className="text-muted-foreground hover:bg-accent/50 hover:text-foreground rounded-md p-1.5 transition-colors"
                    aria-label={`编辑 ${agent.name}`}
                    title="编辑配置"
                  >
                    <EditIcon className="h-3.5 w-3.5" />
                  </button>
                  <StatusBadge status={agent.status} />
                </div>
              </div>
              <p className="text-muted-foreground mb-3 line-clamp-2 text-xs">
                {agent.description || '暂无描述'}
              </p>
              <div className="flex flex-wrap gap-1.5 text-xs">
                <span className="bg-accent/30 rounded px-1.5 py-0.5">
                  {AGENT_TYPE_LABELS[agent.agent_type] ?? agent.agent_type}
                </span>
                {agent.level && (
                  <span className="bg-accent/30 rounded px-1.5 py-0.5">{agent.level}</span>
                )}
                <span className="bg-accent/30 max-w-[120px] truncate rounded px-1.5 py-0.5">
                  {agent.model}
                </span>
              </div>

              {/* 展开详情 */}
              {expandedId === agent.id && (
                <div className="mt-3 space-y-1.5 border-t pt-3 text-xs">
                  {agent.system_prompt && (
                    <div>
                      <span className="text-muted-foreground">系统提示词：</span>
                      <p className="bg-accent/20 text-muted-foreground mt-0.5 line-clamp-4 rounded p-2">
                        {agent.system_prompt}
                      </p>
                    </div>
                  )}
                  {agent.tool_names && agent.tool_names.length > 0 && (
                    <div>
                      <span className="text-muted-foreground">绑定工具：</span>
                      <div className="mt-0.5 flex flex-wrap gap-1">
                        {agent.tool_names.map((t) => (
                          <span
                            key={t}
                            className="bg-primary/10 text-primary rounded px-1.5 py-0.5"
                          >
                            {t}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                  {agent.max_iterations && (
                    <div>
                      <span className="text-muted-foreground">最大迭代：</span>
                      {agent.max_iterations}
                    </div>
                  )}
                  {agent.timeout && (
                    <div>
                      <span className="text-muted-foreground">超时：</span>
                      {agent.timeout}s
                    </div>
                  )}
                  {agent.tags && agent.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {agent.tags.map((tag) => (
                        <span key={tag} className="bg-accent/30 rounded px-1.5 py-0.5">
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 编辑配置模态框（fieldsUri/dataUri 已切 /ext/agent_manager/*） */}
      <AgentConfigModal
        agent={editingAgent}
        isOpen={!!editingAgent}
        onClose={() => setEditingAgent(null)}
        onSaved={fetchAgents}
      />
    </PageShell>
  )
}

export default AgentManagerPage
