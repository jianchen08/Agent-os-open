/**
 * 工具列表页面
 *
 * 展示所有工具，支持搜索过滤、分页和展开详情。
 * 统一外壳走 shared/PageShell，状态走 shared/StatusBadge，三态走 shared 加载/错误/空组件。
 */

import { useState, useEffect, useCallback } from 'react'
import { Wrench } from '@/assets/icons'
import { EmptyState } from '@/components/shared/EmptyState'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { StatusBadge } from '@/components/shared/StatusBadge'
import { getTools } from '@/services/api/tools'
import type { ToolResponse, GetToolsParams } from '@/services/api/tools'

/**
 * 工具列表页面组件
 */
export function ToolsPage() {
  const [tools, setTools] = useState<ToolResponse[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize] = useState(12)
  const [filterCategory, setFilterCategory] = useState('')
  const [filterSource, setFilterSource] = useState('')

  /**
   * 加载工具列表
   */
  const fetchTools = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const params: GetToolsParams = {
        page,
        pageSize,
        search: search || undefined,
        category: filterCategory || undefined,
        source: filterSource || undefined,
      }
      const res = await getTools(params)
      setTools(res.items)
      setTotal(res.total)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取工具列表失败'
      setError(message)
    } finally {
      setIsLoading(false)
    }
  }, [page, pageSize, search, filterCategory, filterSource])

  useEffect(() => {
    fetchTools()
  }, [fetchTools])

  const totalPages = Math.ceil(total / pageSize)
  const hasFilter = Boolean(search || filterCategory || filterSource)

  return (
    <PageShell
      title="工具管理"
      backHref="/"
      actions={<span className="text-muted-foreground text-xs">共 {total} 个工具</span>}
    >
      {/* 搜索和过滤（常驻，不随三态隐藏） */}
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap">
        <input
          type="text"
          placeholder="搜索工具..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value)
            setPage(1)
          }}
          aria-label="搜索工具"
          className="bg-background focus:ring-primary w-full rounded-lg border px-3 py-1.5 text-sm focus:ring-1 focus:outline-none sm:w-auto sm:flex-1"
        />
        <select
          value={filterCategory}
          onChange={(e) => {
            setFilterCategory(e.target.value)
            setPage(1)
          }}
          aria-label="按分类筛选"
          className="bg-background w-full rounded-lg border px-3 py-1.5 text-sm sm:w-auto"
        >
          <option value="">全部分类</option>
          <option value="file">文件</option>
          <option value="search">搜索</option>
          <option value="web">网页</option>
          <option value="memory">记忆</option>
          <option value="task">任务</option>
          <option value="system">系统</option>
          <option value="execution">执行</option>
          <option value="analysis">分析</option>
        </select>
        <select
          value={filterSource}
          onChange={(e) => {
            setFilterSource(e.target.value)
            setPage(1)
          }}
          aria-label="按来源筛选"
          className="bg-background w-full rounded-lg border px-3 py-1.5 text-sm sm:w-auto"
        >
          <option value="">全部来源</option>
          <option value="builtin">内置</option>
          <option value="mcp">MCP</option>
          <option value="custom">自定义</option>
          <option value="code">代码</option>
        </select>
      </div>

      {/* 加载态 - 骨架屏（shared） */}
      {isLoading && <LoadingState variant="skeleton" skeletonCount={6} />}

      {/* 错误态（shared，带重试） */}
      {error && <ErrorState message={error} onRetry={fetchTools} />}

      {/* 空态（shared） */}
      {!isLoading && !error && tools.length === 0 && (
        <EmptyState
          icon={Wrench}
          title={hasFilter ? '没有找到匹配的工具' : '暂无工具'}
          description={hasFilter ? undefined : '请在 config/tools/ 目录下添加工具配置文件'}
        />
      )}

      {/* 工具列表 */}
      {!isLoading && !error && tools.length > 0 && (
        <div
          className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3"
          aria-live="polite"
          aria-label="工具列表"
        >
          {tools.map((tool) => (
            <div
              key={tool.name}
              className="hover:bg-accent/30 cursor-pointer rounded-lg border p-4 transition-colors"
              onClick={() => setExpandedId(expandedId === tool.name ? null : tool.name)}
            >
              <div className="mb-2 flex items-start justify-between">
                <h3 className="mr-2 flex-1 truncate text-sm font-semibold">{tool.name}</h3>
                <StatusBadge status={tool.status} />
              </div>
              <p className="text-muted-foreground mb-2 line-clamp-2 text-xs">
                {tool.description}
              </p>
              <div className="text-muted-foreground flex gap-2 text-xs">
                {tool.category && (
                  <span className="bg-accent/30 rounded px-1.5 py-0.5">{tool.category}</span>
                )}
                <span className="bg-accent/30 rounded px-1.5 py-0.5">{tool.source}</span>
              </div>

              {/* 展开详情 */}
              {expandedId === tool.name && (
                <div className="mt-3 space-y-2 border-t pt-3 text-xs">
                  {tool.when_to_use && tool.when_to_use.length > 0 && (
                    <div>
                      <span className="text-muted-foreground">适用场景：</span>
                      <ul className="mt-1 list-inside list-disc">
                        {tool.when_to_use.map((w, i) => (
                          <li key={i}>{w}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {tool.tags && tool.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {tool.tags.map((tag) => (
                        <span
                          key={tag}
                          className="bg-primary/10 text-primary rounded px-1.5 py-0.5 text-xs"
                        >
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                  {tool.version && (
                    <div>
                      <span className="text-muted-foreground">版本：</span>
                      {tool.version}
                    </div>
                  )}
                  {tool.requires_approval !== undefined && (
                    <div>
                      <span className="text-muted-foreground">需要审批：</span>
                      {tool.requires_approval ? '是' : '否'}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 pt-4">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="hover:bg-accent/50 h-8 rounded-lg border px-3 text-sm disabled:opacity-50 md:min-h-[44px]"
            aria-label="上一页"
          >
            上一页
          </button>
          <span className="text-muted-foreground text-sm">
            {page} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="hover:bg-accent/50 h-8 rounded-lg border px-3 text-sm disabled:opacity-50 md:min-h-[44px]"
            aria-label="下一页"
          >
            下一页
          </button>
        </div>
      )}
    </PageShell>
  )
}
