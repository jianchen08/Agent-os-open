/**
 * 审批文档审阅组件
 *
 * 根据 ui_schema（type: "review_document"）渲染文档审阅面板：
 * 显示制品标题、内容区（支持 diff 视图）、批注列表。
 * 触发场景：插件在 route_signal:wait 时弹出该组件，供用户审阅并批注。
 *
 * @module ReviewDocumentWidget
 */

import React, { useMemo, useState } from 'react'
import { FileText, MessageSquare, GitCompare } from 'lucide-react'
import { ReviewDiff } from '@/components/review/ReviewDiff'

/** 审阅制品项（与 ui_schema.props.artifacts 元素结构对齐） */
interface ReviewArtifact {
  /** 制品 ID */
  id: string
  /** 制品标题 */
  title?: string
  /** 制品内容 */
  content?: string
  /** 对比基线内容（提供时启用 diff 视图） */
  baselineContent?: string
}

/** 批注项 */
interface ReviewAnnotation {
  /** 批注 ID */
  id: string
  /** 批注作者 */
  author?: string
  /** 批注内容 */
  suggestion: string
  /** 创建时间 */
  createdAt?: string
}

/**
 * 从 props 安全提取制品数组
 *
 * @param raw - 原始 props.artifacts
 * @returns 类型安全的 ReviewArtifact 数组
 */
function extractArtifacts(raw: unknown): ReviewArtifact[] {
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (a): a is ReviewArtifact =>
      typeof a === 'object' && a !== null && typeof (a as ReviewArtifact).id === 'string',
  )
}

/**
 * 从 props 安全提取批注数组
 *
 * @param raw - 原始 props.annotations
 * @returns 类型安全的 ReviewAnnotation 数组
 */
function extractAnnotations(raw: unknown): ReviewAnnotation[] {
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (a): a is ReviewAnnotation =>
      typeof a === 'object' && a !== null && typeof (a as ReviewAnnotation).id === 'string',
  )
}

/**
 * 审批文档审阅组件
 *
 * @param props - 组件属性
 *   - artifacts: 待审阅制品列表
 *   - annotations: 已有批注列表
 *   - diff_view: 是否启用 diff 视图（需制品提供 baselineContent）
 *   - annotation: 是否展示批注区
 * @returns 审阅面板渲染结果
 */
export function ReviewDocumentWidget(props: Record<string, unknown>) {
  const artifacts = extractArtifacts(props.artifacts)
  const annotations = extractAnnotations(props.annotations)
  const diffViewEnabled = (props.diff_view as boolean) ?? false
  const annotationEnabled = (props.annotation as boolean) ?? true
  const [diffMode, setDiffMode] = useState<'side-by-side' | 'unified'>('side-by-side')

  // 当前选中的制品（默认第一个）
  const [activeId, setActiveId] = useState<string>(() => artifacts[0]?.id ?? '')
  const active = useMemo(
    () => artifacts.find((a) => a.id === activeId) ?? artifacts[0],
    [artifacts, activeId],
  )

  if (artifacts.length === 0) {
    return (
      <div className="text-muted-foreground rounded-lg border bg-background p-6 text-center text-sm">
        暂无可审阅的制品
      </div>
    )
  }

  const showDiff = diffViewEnabled && active?.baselineContent !== undefined

  return (
    <div className="flex h-full flex-col rounded-lg border bg-background">
      {/* 标题栏 */}
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <FileText className="text-primary h-5 w-5" />
          <h3 className="text-foreground text-base font-semibold">
            {active?.title ?? active?.id ?? '文档审阅'}
          </h3>
        </div>
        {showDiff && (
          <button
            type="button"
            onClick={() => setDiffMode((m) => (m === 'side-by-side' ? 'unified' : 'side-by-side'))}
            className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs"
          >
            <GitCompare className="h-4 w-4" />
            {diffMode === 'side-by-side' ? '切换为统一视图' : '切换为对比视图'}
          </button>
        )}
      </div>

      {/* 多制品切换 */}
      {artifacts.length > 1 && (
        <div className="flex flex-wrap gap-1 border-b px-4 py-2">
          {artifacts.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => setActiveId(a.id)}
              className={`rounded-md px-2 py-1 text-xs transition-colors ${
                (active?.id ?? '') === a.id
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:bg-muted'
              }`}
            >
              {a.title ?? a.id}
            </button>
          ))}
        </div>
      )}

      <div className={`flex min-h-0 flex-1 ${annotationEnabled ? 'flex-col lg:flex-row' : ''}`}>
        {/* 内容区 */}
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {showDiff ? (
            <ReviewDiff
              oldContent={active?.baselineContent ?? ''}
              newContent={active?.content ?? ''}
              mode={diffMode}
            />
          ) : (
            <pre className="text-foreground whitespace-pre-wrap break-words text-sm">
              {active?.content ?? ''}
            </pre>
          )}
        </div>

        {/* 批注区 */}
        {annotationEnabled && (
          <div className="border-t bg-muted/30 p-4 lg:w-72 lg:border-l lg:border-t-0">
            <div className="mb-2 flex items-center gap-1.5">
              <MessageSquare className="text-muted-foreground h-4 w-4" />
              <span className="text-foreground text-sm font-medium">
                批注（{annotations.length}）
              </span>
            </div>
            {annotations.length === 0 ? (
              <p className="text-muted-foreground text-xs">暂无批注</p>
            ) : (
              <ul className="space-y-2">
                {annotations.map((ann) => (
                  <li key={ann.id} className="rounded-md border bg-background p-2 text-xs">
                    {ann.author && (
                      <div className="text-foreground font-medium">{ann.author}</div>
                    )}
                    <div className="text-muted-foreground mt-0.5">{ann.suggestion}</div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
