/**
 * ContextUsageWidget —— 输入框上下文用量指示器（chat-input 空间 context_usage 槽位）
 *
 * 职责边界：**数据从哪来由插件配置声明，组件只做上下文适配**。
 * props.datasourceUri 声明数据源（默认内核管道 state 聚合），组件负责
 * 按当前激活 Tab 的 pipelineRunId 从全量 state 里挑出本管道行，映射
 * track.llm_usage（用量）与 llm_model（模型名）；渲染复用
 * ContextUsageIndicator，与改造前的槽位 fallback 视觉完全一致。
 *
 * 对应三个历史缺陷（2026-08-28）：
 * - **刷新后归零**：数据源是后端持久化的管道 state（挂载即重拉），不再依赖
 *   contextUsageStore 的纯内存桶（刷新即丢）
 * - **不实时/串管道**：pipelineId 从 agentTabStore 响应式取，切 Tab 即跟随；
 *   声明的 refresh.poll 周期性重拉保证新鲜度
 * - **模型名时有时无**：模型名取 state.llm_model（管道实际值），不依赖
 *   agents 异步列表，无加载竞态导致的「模型无效」误报
 *
 * 插件声明示例（llm_core/plugin.json ui_schema.widgets）：
 * ```json
 * {
 *   "id": "context_usage",
 *   "type": "context_usage",
 *   "space": "chat-input",
 *   "props": {
 *     "datasourceUri": "/api/v1/pipelines/state",
 *     "refresh": { "type": "poll", "intervalSeconds": 15 }
 *   }
 * }
 * ```
 * id 与槽位 id 相同 → 覆盖 ChatInput 的默认件（DeclaredWidgetLayer 槽位语义）。
 */

import { useMemo } from 'react'
import { ContextUsageIndicator } from '@/components/chat/ContextUsageIndicator'
import { useAgentsQuery } from '@/hooks/queries/useAgentsQuery'
import { useModelContextInfo } from '@/hooks/useModelContextInfo'
import { useDataWidget } from '@/services/schema/dataWidget'
import { useAgentTabStore } from '@/stores/agentTabStore'
import type { PipelineLlmUsage, PipelineStateInfo } from '@/services/api/pipelines'

/** 默认数据源：内核管道 state 聚合（已出口 track.llm_usage / llm_model） */
const DEFAULT_DATASOURCE = '/api/v1/pipelines/state'

export function ContextUsageWidget(props: Record<string, unknown>) {
  const uri = (props.datasourceUri as string | undefined) ?? DEFAULT_DATASOURCE
  /** 声明的静态兜底模型名：新会话尚无管道 state 时先显示它 */
  const staticModel = props.modelName as string | undefined
  const { data } = useDataWidget({ ...props, datasourceUri: uri }, 'scalar')

  /** 当前激活 Tab（响应式：切标签即重算，不串到别的管道） */
  const pipelineId = useAgentTabStore((s) => {
    const tab = s.tabs.find((t) => t.id === s.activeTabId)
    return tab?.pipelineRunId ?? ''
  })
  const agentId = useAgentTabStore((s) => {
    const tab = s.tabs.find((t) => t.id === s.activeTabId)
    return tab?.agentId ?? ''
  })

  /**
   * 兜底模型名（新会话尚无管道 state 时用）：从 agents 列表按 agentId 查。
   * 仅作兜底——有 state 时以管道实际 llm_model 为准，故 agents 异步加载
   * 的空窗期不会造成「模型无效」误报（无值时不渲染）。
   */
  const { data: agents = [] } = useAgentsQuery()
  const agentsModel = useMemo(() => {
    if (!agentId) return ''
    const matched = agents.find((a) => a.id === agentId || a.configId === agentId)
    return matched?.model || matched?.config?.model || ''
  }, [agentId, agents])

  /** 从全量 state 中挑出当前管道行的用量与模型名 */
  const { modelName, usage } = useMemo(() => {
    const items = (data as { items?: PipelineStateInfo[] } | null | undefined)?.items
    const row = pipelineId && items ? items.find((i) => i.pipeline_id === pipelineId) : undefined
    const st = row?.state
    return {
      modelName: st?.llm_model || staticModel || agentsModel,
      usage: st?.['track.llm_usage'] as PipelineLlmUsage | undefined,
    }
  }, [data, pipelineId, staticModel, agentsModel])

  /** 动态 context_window：模型无效时为 0（下游 maxTokens<=0 不渲染圆环） */
  const { contextWindow } = useModelContextInfo(modelName || 'unknown')

  // 无模型名（新会话尚无管道 state 且未声明默认值）：不渲染，
  // 避免显示误报的「模型无效」
  if (!modelName) return null

  const currentTokenUsage = usage?.last_input_tokens ?? 0
  const completionTokens = usage?.last_output_tokens ?? 0
  const totalTokens = currentTokenUsage + completionTokens

  return (
    <ContextUsageIndicator
      modelName={modelName}
      currentTokenUsage={currentTokenUsage}
      maxTokens={contextWindow}
      totalTokens={totalTokens || undefined}
      completionTokens={completionTokens || undefined}
      cumulative={
        usage?.total_tokens
          ? {
              total_input: usage.total_input_tokens ?? 0,
              total_output: usage.total_output_tokens ?? 0,
              total_cached: usage.total_cached_tokens ?? 0,
              missed: usage.total_missed_tokens ?? 0,
              total_tokens: usage.total_tokens ?? 0,
              cache_hit_ratio: usage.total_cache_hit_ratio ?? 0,
            }
          : undefined
      }
      cachedTokens={usage?.last_cached_tokens || undefined}
      hitRatio={usage?.last_cache_hit_ratio || undefined}
      className={props.className as string | undefined}
    />
  )
}

export default ContextUsageWidget
