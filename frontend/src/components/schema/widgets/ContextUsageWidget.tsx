/**
 * ContextUsageWidget —— 输入框上下文用量指示器（chat-input 空间 context_usage 槽位）
 *
 * 职责边界：**数据从哪来由插件配置声明，组件只做上下文适配**。
 * props.datasourceUri 声明数据源（默认内核管道 state 聚合），组件负责
 * 按当前激活 Tab 的 pipelineRunId 从全量 state 里挑出本管道行，模型名、
 * 上下文窗口、用量三类真值全部出自该行（llm_model / context_window /
 * track.llm_usage）——不依赖 agents 异步列表，无加载竞态；渲染复用
 * ContextUsageIndicator。取数经 dataWidget 层的载荷缓存，槽位轮询
 * 重挂载/切标签先渲上次数据再静默刷新，不闪空窗。
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
import { useModelContextInfo } from '@/hooks/useModelContextInfo'
import { useDataWidget } from '@/services/schema/dataWidget'
import { useAgentTabStore } from '@/stores/agentTabStore'
import type { PipelineLlmUsage, PipelineStateInfo } from '@/services/api/pipelines'

/** 默认数据源：内核管道 state 聚合（已出口 track.llm_usage / llm_model / context_window） */
const DEFAULT_DATASOURCE = '/api/v1/pipelines/state'

export function ContextUsageWidget(props: Record<string, unknown>) {
  const uri = (props.datasourceUri as string | undefined) ?? DEFAULT_DATASOURCE
  /** 声明的静态兜底模型名：尚无管道 state 行时先显示它 */
  const staticModel = props.modelName as string | undefined
  const { data } = useDataWidget({ ...props, datasourceUri: uri }, 'scalar')

  /** 当前激活 Tab（响应式：切标签即重算，不串到别的管道） */
  const pipelineId = useAgentTabStore((s) => {
    const tab = s.tabs.find((t) => t.id === s.activeTabId)
    return tab?.pipelineRunId ?? ''
  })

  /** 从全量 state 中挑出当前管道行的模型/窗口/用量（上游唯一数据源） */
  const { modelName, usage, stateContextWindow } = useMemo(() => {
    const items = (data as { items?: PipelineStateInfo[] } | null | undefined)?.items
    const row = pipelineId && items ? items.find((i) => i.pipeline_id === pipelineId) : undefined
    const st = row?.state
    return {
      modelName: st?.llm_model || staticModel,
      usage: st?.['track.llm_usage'] as PipelineLlmUsage | undefined,
      stateContextWindow: st?.context_window ?? 0,
    }
  }, [data, pipelineId, staticModel])

  // 窗口真值=state 行 context_window；模型注册表按键精确查询，而实际模型名
  // （如 MiniMax-M3）与配置键（minimax-m3）大小写形态不一致，仅作行缺字段时兜底
  const registry = useModelContextInfo(modelName || 'unknown')
  const maxTokens = stateContextWindow || registry.contextWindow

  // 无模型名（尚无管道 state 行且未声明默认值）：不渲染，避免显示误报的「模型无效」
  if (!modelName) return null

  const currentTokenUsage = usage?.last_input_tokens ?? 0
  const completionTokens = usage?.last_output_tokens ?? 0
  const totalTokens = currentTokenUsage + completionTokens

  return (
    <ContextUsageIndicator
      modelName={modelName}
      currentTokenUsage={currentTokenUsage}
      maxTokens={maxTokens}
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
