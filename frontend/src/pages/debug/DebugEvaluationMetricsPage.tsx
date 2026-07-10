/**
 * 调试评估指标页面
 *
 * 展示系统评估指标列表
 */

import { useState, useEffect, useCallback } from 'react'
import * as evaluationApi from '@/services/api/evaluationMetrics'
import type { EvaluationMetric } from '@/services/api/evaluationMetrics'

/** 分类过滤选项 */
const CATEGORY_OPTIONS = ['', 'quality', 'safety', 'performance', 'reliability']

/**
 * 调试评估指标页面组件
 */
export function DebugEvaluationMetricsPage() {
  const [metrics, setMetrics] = useState<EvaluationMetric[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [categoryFilter, setCategoryFilter] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)

  /**
   * 加载评估指标
   */
  const fetchMetrics = useCallback(async (category?: string) => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await evaluationApi.getEvaluationMetrics({
        category: category || undefined,
        limit: 100,
      })
      setMetrics(res.metrics)
      setTotal(res.total)
    } catch (err: any) {
      setError(err.message || '获取评估指标失败')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchMetrics()
  }, [fetchMetrics])

  /** 分类过滤变更 */
  const handleCategoryChange = (category: string) => {
    setCategoryFilter(category)
    fetchMetrics(category || undefined)
  }

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/debug" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">评估指标</h1>
        <span className="text-muted-foreground ml-auto text-xs">共 {total} 个指标</span>
      </header>
      <main className="flex-1 space-y-4 overflow-y-auto p-3 sm:p-6">
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
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="border-primary h-6 w-6 animate-spin rounded-full border-2 border-t-transparent" />
            <span className="text-muted-foreground ml-2 text-sm">加载中...</span>
          </div>
        )}

        {/* 错误提示 */}
        {error && (
          <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>
        )}

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
      </main>
    </div>
  )
}
