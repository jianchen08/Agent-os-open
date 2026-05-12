/**
 * Schema 主渲染组件
 *
 * 顶层渲染组件，接收 ModuleUISchema，通过 SchemaParser 解析、
 * RenderingEngine 生成渲染指令，分发到对应的渲染空间组件。
 * 处理加载态、错误态、空数据态。
 *
 * @module SchemaRenderer
 */

import React, { useMemo, useState, useCallback } from 'react'
import { renderingEngine, type RenderInstructionSet } from '@/services/schema/RenderingEngine'
import { schemaParser, type SchemaParseError } from '@/services/schema/SchemaParser'
import {
  ChatSpaceRenderer,
  WorkspaceSpaceRenderer,
  FloatingSpaceRenderer,
  DockSpaceRenderer,
} from './spaces'
import type { ModuleUISchema, ClientCapabilities } from '@/types/schema'

/** SchemaRenderer 属性 */
export interface SchemaRendererProps {
  /** 模块 UI Schema */
  schema: ModuleUISchema
  /** 可选的客户端能力（用于降级过滤） */
  capabilities?: ClientCapabilities
  /** 指定只渲染某个空间 */
  space?: 'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen'
  /** 自定义加载态组件 */
  loadingFallback?: React.ReactNode
  /** 自定义错误态组件 */
  errorFallback?: (error: Error, retry: () => void) => React.ReactNode
  /** 自定义空数据态组件 */
  emptyFallback?: React.ReactNode
  /** Dock 入口点击回调 */
  onDockItemClick?: (moduleId: string) => void
  /** 额外的 CSS 类名 */
  className?: string
}

/** 渲染状态 */
type RenderState = 'idle' | 'loading' | 'success' | 'error'

/**
 * Schema 主渲染组件
 *
 * 接收 ModuleUISchema，自动完成解析和渲染指令生成，
 * 根据渲染指令选择对应的渲染空间组件。
 *
 * 支持三种异常状态：
 * - 加载中：显示加载指示器
 * - 错误：显示错误信息（可自定义）
 * - 空数据：显示空状态占位
 *
 * @param props - 渲染器属性
 * @returns 渲染结果
 */
export function SchemaRenderer({
  schema,
  capabilities,
  space,
  loadingFallback,
  errorFallback,
  emptyFallback,
  onDockItemClick,
  className,
}: SchemaRendererProps) {
  const [retryCount, setRetryCount] = useState(0)

  // 解析 Schema 并生成渲染指令
  const { state, instructionSet, error } = useMemo(() => {
    try {
      const { parsed } = schemaParser.parse(schema)
      const instructions = renderingEngine.render(parsed, capabilities)

      if (instructions.all.length === 0) {
        return { state: 'success' as RenderState, instructionSet: instructions, error: null }
      }

      return { state: 'success' as RenderState, instructionSet: instructions, error: null }
    } catch (err) {
      const parseError = err as SchemaParseError
      return { state: 'error' as RenderState, instructionSet: null, error: parseError }
    }
    // retryCount 变化时重新解析（用于重试）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schema, capabilities, retryCount])

  // 重试回调
  const handleRetry = useCallback(() => {
    setRetryCount((c) => c + 1)
  }, [])

  // 渲染加载态
  if (state === 'loading') {
    return (
      <div className={className}>
        {loadingFallback ?? <DefaultLoadingState />}
      </div>
    )
  }

  // 渲染错误态
  if (state === 'error' || !instructionSet) {
    if (errorFallback && error) {
      return <div className={className}>{errorFallback(error, handleRetry)}</div>
    }
    return (
      <div className={className}>
        <DefaultErrorState error={error} onRetry={handleRetry} />
      </div>
    )
  }

  // 渲染空数据态
  if (instructionSet.all.length === 0) {
    return (
      <div className={className}>
        {emptyFallback ?? <DefaultEmptyState />}
      </div>
    )
  }

  // 渲染指定空间或全部空间
  if (space) {
    return (
      <div className={className}>
        <SpaceRenderer
          space={space}
          instructions={instructionSet.bySpace[space]}
          onDockItemClick={onDockItemClick}
        />
      </div>
    )
  }

  // 渲染所有空间
  return (
    <div className={className}>
      <ChatSpaceRenderer instructions={instructionSet.bySpace.chat} />
      <WorkspaceSpaceRenderer instructions={instructionSet.bySpace.workspace} />
      <FloatingSpaceRenderer instructions={instructionSet.bySpace.floating} />
      <DockSpaceRenderer
        instructions={instructionSet.bySpace.dock}
        onItemClick={onDockItemClick}
      />
    </div>
  )
}

/**
 * 空间路由：根据空间类型选择对应的渲染器
 */
function SpaceRenderer({
  space,
  instructions,
  onDockItemClick,
}: {
  space: 'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen'
  instructions: RenderInstructionSet['bySpace'][RenderingSpaceType]
  onDockItemClick?: (moduleId: string) => void
}) {
  switch (space) {
    case 'chat':
      return <ChatSpaceRenderer instructions={instructions} />
    case 'workspace':
      return <WorkspaceSpaceRenderer instructions={instructions} />
    case 'floating':
      return <FloatingSpaceRenderer instructions={instructions} />
    case 'dock':
      return <DockSpaceRenderer instructions={instructions} onItemClick={onDockItemClick} />
    case 'fullscreen':
      // 全屏渲染复用工作区渲染器
      return <WorkspaceSpaceRenderer instructions={instructions} />
    default:
      return null
  }
}

/**
 * 默认加载态组件
 */
function DefaultLoadingState() {
  return (
    <div className="flex items-center justify-center p-8">
      <div className="flex flex-col items-center gap-2">
        <div className="border-primary h-6 w-6 animate-spin rounded-full border-2 border-t-transparent" />
        <span className="text-muted-foreground text-sm">加载中...</span>
      </div>
    </div>
  )
}

/**
 * 默认错误态组件
 */
function DefaultErrorState({
  error,
  onRetry,
}: {
  error: Error | null
  onRetry: () => void
}) {
  return (
    <div className="flex items-center justify-center p-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <div className="bg-destructive/10 text-destructive rounded-full p-3">
          <svg
            className="h-5 w-5"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"
            />
          </svg>
        </div>
        <div>
          <p className="text-sm font-medium">渲染失败</p>
          {error && (
            <p className="text-muted-foreground mt-1 text-xs">{error.message}</p>
          )}
        </div>
        <button
          type="button"
          className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-3 py-1.5 text-xs transition-colors"
          onClick={onRetry}
        >
          重试
        </button>
      </div>
    </div>
  )
}

/**
 * 默认空数据态组件
 */
function DefaultEmptyState() {
  return (
    <div className="flex items-center justify-center p-8">
      <div className="flex flex-col items-center gap-2 text-center">
        <div className="bg-muted text-muted-foreground rounded-full p-3">
          <svg
            className="h-5 w-5"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"
            />
          </svg>
        </div>
        <p className="text-muted-foreground text-sm">暂无可渲染内容</p>
      </div>
    </div>
  )
}
