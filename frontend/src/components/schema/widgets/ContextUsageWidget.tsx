/**
 * ContextUsageWidget —— 输入框上下文用量指示器（chat-input 空间 context_usage 槽位）
 *
 * 数据源 = 管道 state（唯一真值）：消费共享的 pipelineStates query 缓存
 * （GET /api/v1/pipelines/state，30s 兜底轮询 + WS 重连/轮末 cost_update
 * 事件失效化），组件按当前激活 Tab 的 pipelineRunId 从中挑出本管道行，
 * 模型名、上下文窗口、用量三类真值全部出自该行（llm_model /
 * context_window / track.llm_usage）——不依赖 agents 异步列表，无加载竞态；
 * query 缓存跨消费方共享（任务管理页同源），重挂载/切标签先渲缓存再静默
 * 刷新，不闪空窗。渲染复用 ContextUsageIndicator。
 *
 * props.modelName：尚无管道 state 行时（新会话首轮前）的静态兜底模型名
 * （槽位默认件由宿主传入配置模型名；行内 llm_model 存在时以行内真值优先）。
 *
 * 插件声明示例（llm_core/plugin.json ui_schema.widgets）：
 * ```json
 * { "id": "context_usage", "type": "context_usage", "space": "chat-input" }
 * ```
 * id 与槽位 id 相同 → 覆盖 ChatInput 的默认件（DeclaredWidgetLayer 槽位语义）。
 */

import { useMemo } from 'react'
import { ContextUsageIndicator } from '@/components/chat/ContextUsageIndicator'
import { usePipelineStatesQuery } from '@/hooks/queries/usePipelineRunsQuery'
import { useModelContextInfo } from '@/hooks/useModelContextInfo'
import { mapStateInfoToViewModel } from '@/services/api/pipelines'
import { useAgentTabStore } from '@/stores/agentTabStore'

export function ContextUsageWidget(props: Record<string, unknown>) {
  /** 声明的静态兜底模型名：尚无管道 state 行时先显示它 */
  const staticModel = props.modelName as string | undefined

  /** 当前激活 Tab（响应式：切标签即重算，不串到别的管道） */
  const pipelineId = useAgentTabStore((s) => {
    const tab = s.tabs.find((t) => t.id === s.activeTabId)
    return tab?.pipelineRunId ?? ''
  })

  /** 管道 state 摘要（共享 query 缓存；失效化/轮询由 query 层统一承担） */
  const { data: states } = usePipelineStatesQuery()

  /** 从 state 注册表中挑出当前管道行的模型/窗口/用量（视图模型适配，上游唯一数据源） */
  const { modelName, usage, stateContextWindow } = useMemo(() => {
    const row = pipelineId && states ? states[pipelineId] : undefined
    const view = row ? mapStateInfoToViewModel(row) : undefined
    return {
      modelName: view?.llmModel || staticModel,
      usage: view?.llmUsage,
      stateContextWindow: view?.contextWindow ?? 0,
    }
  }, [states, pipelineId, staticModel])

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
