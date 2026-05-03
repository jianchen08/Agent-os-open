/**
 * useSchemaRenderer Hook
 *
 * 封装 Schema 解析和渲染引擎的 React Hook，
 * 提供响应式的渲染指令，Schema 变化时自动重新生成。
 *
 * @module useSchemaRenderer
 */

import { useMemo, useState, useCallback } from 'react'
import type { ModuleUISchema, ClientCapabilities } from '@/types/schema'
import { schemaParser } from '@/services/schema/SchemaParser'
import { renderingEngine, type RenderInstructionSet } from '@/services/schema/RenderingEngine'

/** Hook 返回值 */
export interface UseSchemaRendererResult {
  /** 渲染指令集 */
  instructionSet: RenderInstructionSet | null
  /** 是否正在加载 */
  loading: boolean
  /** 错误信息 */
  error: Error | null
  /** 强制刷新 */
  refresh: () => void
}

/**
 * Schema 渲染 Hook
 *
 * 接收 ModuleUISchema，自动完成解析和渲染指令生成。
 * Schema 或客户端能力变化时自动重新生成指令。
 *
 * @param schema - 模块 UI Schema
 * @param capabilities - 可选的客户端能力
 * @returns 渲染结果
 *
 * @example
 * ```tsx
 * const { instructionSet, loading, error } = useSchemaRenderer(schema)
 *
 * if (loading) return <Spinner />
 * if (error) return <ErrorView error={error} />
 *
 * return (
 *   <ChatSpaceRenderer instructions={instructionSet.bySpace.chat} />
 * )
 * ```
 */
export function useSchemaRenderer(
  schema: ModuleUISchema | null | undefined,
  capabilities?: ClientCapabilities,
): UseSchemaRendererResult {
  const [refreshKey, setRefreshKey] = useState(0)

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1)
  }, [])

  const result = useMemo(() => {
    if (!schema) {
      return { instructionSet: null, loading: false, error: null }
    }

    try {
      const { parsed } = schemaParser.parse(schema)
      const instructionSet = renderingEngine.render(parsed, capabilities)
      return { instructionSet, loading: false, error: null }
    } catch (err) {
      return { instructionSet: null, loading: false, error: err as Error }
    }
    // refreshKey 变化时重新计算
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schema, capabilities, refreshKey])

  return {
    ...result,
    refresh,
  }
}
