/**
 * 调试评估指标页面（query 化：useEvaluationMetricsQuery 缓存 SWR，重挂零请求）
 *
 * 展示系统评估指标列表
 */

import { useState } from 'react'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { useEvaluationMetricsQuery } from '@/hooks/queries/useDebugQueries'

/** 分类过滤选项 */
const CATEGORY_OPTIONS = ['', 'quality', 'safety', 'performance', 'reliability']

/**
 * 调试评估指标页面组件
 */
export function DebugEvaluationMetricsPage({ embedded }: { embedded?: boolean } = {}) {
  const [categoryFilter, setCategoryFilter] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)

  // 评估指标（query 化）：分类过滤变化显式重拉；staleTime 窗口内重挂零请求
  const metricsQuery = useEvaluationMetricsQuery(categoryFilter || undefined)
  const metrics = metricsQuery.data?.metrics ?? []
  const total = metricsQuery.data?.total ?? 0
  // 无缓存数据时显示 loading（有缓存先渲染缓存不闪 loading）
  const isLoading = metricsQuery.isPending && !metricsQuery.data
  const error = metricsQuery.isError
    ? metricsQuery.error instanceof Error
      ? metricsQuery.error.message
      : '获取评估指标失败'
    : null

  /** 分类过滤变更 */
  const handleCategoryChange = (category: string) => {
    setCategoryFilter(category)
  }

  return (
    <PageShell
      title="评估指标"
      backHref="/debug"
      embedded={embedded}
      actions={<span className="text-muted-foreground text-xs">共 {total} 个指标</span>}
    >
      {/* 分类过滤 */}
      <div className="flex gap-2">
        <button
          onClick={() => handleCategoryChange('')}
          className={`rounded-lg border px-3 py-1.5 text-xs transition-colors ${
            categoryFilter === ''
              ? 'bg-primary text-primary-foreground border-primary'
              : 'hover:bg-accent/50'
          }`}
        >
          全部
        </button>
        {CATEGORY_OPTIONS.slice(1).map((cat) => (
          <button
            key={cat}
            onClick={() => handleCategoryChange(cat)}
            className={`rounded-lg border px-3 py-1.5 text-xs capitalize transition-colors ${
              categoryFilter === cat
                ? 'bg-primary text-primary-foreground border-primary'
                : 'hover:bg-accent/50'
            }`}
          >
            {cat}
          </button>
        ))}
      </div>

      {/* 加载状态 */}
      {isLoading && <LoadingState />}

      {/* 错误提示 */}
      {error && <ErrorState message={error} />}

      {/* 空状态 */}
      {!isLoading && !error && metrics.length === 0 && (
        <div className="text-muted-foreground py-12 text-center">暂无数据</div>
      )}

      {/* 指标列表 */}
      {!isLoading && !error && metrics.length > 0 && (
        <div className="space-y-3">
            {metrics.map((metric) => (
              <div
                key={metric.id}
                className="hover:bg-accent/30 cursor-pointer rounded-lg border p-4 transition-colors"
                onClick={() => setExpandedId(expandedId === metric.id ? null : metric.id)}
              >
                <div className="mb-2 flex items-start justify-between">
                  <h3 className="text-sm font-semibold">{metric.name}</h3>
                  <div className="flex gap-1.5">
                    {metric.is_red_line && (
                      <span className="rounded bg-status-error/10 px-1.5 py-0.5 text-xs text-status-error">
                        红线
                      </span>
                    )}
                    <span className="bg-accent/30 rounded px-1.5 py-0.5 text-xs">
                      {metric.status}
                    </span>
                  </div>
                </div>
                <p className="text-muted-foreground mb-2 line-clamp-2 text-xs">
                  {metric.description}
                </p>
                <div className="flex flex-wrap gap-1.5 text-xs">
                  <span className="bg-primary/10 text-primary rounded px-1.5 py-0.5">
                    {metric.category}
                  </span>
                  <span className="bg-accent/30 rounded px-1.5 py-0.5">L{metric.level}</span>
                  <span className="bg-accent/30 rounded px-1.5 py-0.5">
                    权重: {metric.default_weight}
                  </span>
                  {metric.default_pass_threshold !== undefined && (
                    <span className="bg-accent/30 rounded px-1.5 py-0.5">
                      阈值: {metric.default_pass_threshold}
                    </span>
                  )}
                </div>

                {/* 展开详情 */}
                {expandedId === metric.id && (
                  <div className="mt-3 space-y-1.5 border-t pt-3 text-xs">
                    <div>
                      <span className="text-muted-foreground">评估器：</span>
                      {metric.evaluator_type} ({metric.evaluator_id})
                    </div>
                    <div>
                      <span className="text-muted-foreground">来源：</span>
                      {metric.source}
                    </div>
                    <div>
                      <span className="text-muted-foreground">使用次数：</span>
                      {metric.usage_count} (成功 {metric.success_count})
                    </div>
                    {metric.avg_execution_time !== undefined && (
                      <div>
                        <span className="text-muted-foreground">平均耗时：</span>
                        {metric.avg_execution_time.toFixed(0)}ms
                      </div>
                    )}
                    {metric.includes && metric.includes.length > 0 && (
                      <div>
                        <span className="text-muted-foreground">包含指标：</span>
                        <div className="mt-0.5 flex flex-wrap gap-1">
                          {metric.includes.map((inc) => (
                            <span key={inc} className="bg-accent/30 rounded px-1.5 py-0.5">
                              {inc}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                    {metric.tags && metric.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {metric.tags.map((tag) => (
                          <span
                            key={tag}
                            className="bg-primary/10 text-primary rounded px-1.5 py-0.5"
                          >
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
    </PageShell>
  )
}
