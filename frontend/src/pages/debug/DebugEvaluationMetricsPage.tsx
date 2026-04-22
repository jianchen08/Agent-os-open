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
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/debug" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">评估指标</h1>
        <span className="ml-auto text-xs text-muted-foreground">共 {total} 个指标</span>
      </header>
      <main className="flex-1 overflow-y-auto p-6 space-y-4">
        {/* 分类过滤 */}
        <div className="flex gap-2">
          <button
            onClick={() => handleCategoryChange('')}
            className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${
              categoryFilter === '' ? 'bg-primary text-primary-foreground border-primary' : 'hover:bg-accent/50'
            }`}
          >
            全部
          </button>
          {CATEGORY_OPTIONS.slice(1).map(cat => (
            <button
              key={cat}
              onClick={() => handleCategoryChange(cat)}
              className={`px-3 py-1.5 text-xs rounded-lg border transition-colors capitalize ${
                categoryFilter === cat ? 'bg-primary text-primary-foreground border-primary' : 'hover:bg-accent/50'
              }`}
            >
              {cat}
            </button>
          ))}
        </div>

        {/* 加载状态 */}
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">加载中...</span>
          </div>
        )}

        {/* 错误提示 */}
        {error && (
          <div className="p-4 rounded-lg bg-destructive/10 text-destructive text-sm">{error}</div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && metrics.length === 0 && (
          <div className="text-center py-12 text-muted-foreground">暂无数据</div>
        )}

        {/* 指标列表 */}
        {!isLoading && !error && metrics.length > 0 && (
          <div className="space-y-3">
            {metrics.map(metric => (
              <div
                key={metric.id}
                className="border rounded-lg p-4 cursor-pointer hover:bg-accent/30 transition-colors"
                onClick={() => setExpandedId(expandedId === metric.id ? null : metric.id)}
              >
                <div className="flex items-start justify-between mb-2">
                  <h3 className="text-sm font-semibold">{metric.name}</h3>
                  <div className="flex gap-1.5">
                    {metric.is_red_line && (
                      <span className="text-xs px-1.5 py-0.5 bg-red-500/10 text-red-500 rounded">红线</span>
                    )}
                    <span className="text-xs px-1.5 py-0.5 bg-accent/30 rounded">{metric.status}</span>
                  </div>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2 mb-2">{metric.description}</p>
                <div className="flex flex-wrap gap-1.5 text-xs">
                  <span className="px-1.5 py-0.5 bg-primary/10 text-primary rounded">{metric.category}</span>
                  <span className="px-1.5 py-0.5 bg-accent/30 rounded">L{metric.level}</span>
                  <span className="px-1.5 py-0.5 bg-accent/30 rounded">权重: {metric.default_weight}</span>
                  {metric.default_pass_threshold !== undefined && (
                    <span className="px-1.5 py-0.5 bg-accent/30 rounded">阈值: {metric.default_pass_threshold}</span>
                  )}
                </div>

                {/* 展开详情 */}
                {expandedId === metric.id && (
                  <div className="mt-3 pt-3 border-t text-xs space-y-1.5">
                    <div><span className="text-muted-foreground">评估器：</span>{metric.evaluator_type} ({metric.evaluator_id})</div>
                    <div><span className="text-muted-foreground">来源：</span>{metric.source}</div>
                    <div><span className="text-muted-foreground">使用次数：</span>{metric.usage_count} (成功 {metric.success_count})</div>
                    {metric.avg_execution_time !== undefined && (
                      <div><span className="text-muted-foreground">平均耗时：</span>{metric.avg_execution_time.toFixed(0)}ms</div>
                    )}
                    {metric.includes && metric.includes.length > 0 && (
                      <div>
                        <span className="text-muted-foreground">包含指标：</span>
                        <div className="flex flex-wrap gap-1 mt-0.5">
                          {metric.includes.map(inc => (
                            <span key={inc} className="px-1.5 py-0.5 bg-accent/30 rounded">{inc}</span>
                          ))}
                        </div>
                      </div>
                    )}
                    {metric.tags && metric.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {metric.tags.map(tag => (
                          <span key={tag} className="px-1.5 py-0.5 bg-primary/10 text-primary rounded">{tag}</span>
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
