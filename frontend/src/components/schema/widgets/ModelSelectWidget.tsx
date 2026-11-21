/**
 * ModelSelectWidget —— 输入框模型选择器（chat-input 空间 model_id 槽位）
 *
 * 声明面 = llm_core ui_schema widget（id=model_id / type=model_select /
 * frame_field=model_id）：widget 即字段声明——用途（选择模型）、来源
 * （useLlmConfigQuery 模型注册表）、传输去向（frame_field → user_input 帧
 * model_id 字段 → 内核单键 state overlay → llm_core 解析链首位，显式选择
 * 最高优先级覆盖管道配置）。
 *
 * 值契约（useControlledSlotBridge 注入）：value.model_id（'' = 无显式选择）+
 * onChange。停留态与权限选择器同态（2026-10-23 用户裁定）：**当前是什么模型
 * 就停在什么选项**——无显式选择时按管道 state 行 llm_model（model_name）反查
 * 注册表键常驻显示；显式选择优先且随标签记忆（modelSelectStore），用户想换才
 * 动手。上下文窗口/用量信息归 context_usage 小圈圈，本选择器不重复展示。
 *
 * 默认显示面（fallbackModelName，宿主经 overrideProps 注入）：state 行未出
 * （新会话首轮发送前 llm_model 恒空）时按宿主给的默认模型名（agent 头 model
 * → 系统默认对话模型，ChatContainer 解析）三级反查注册表键常驻显示——求值序
 * = 显式命中 > state 停留值 > fallback；反查失败回空态占位（fail-soft，不
 * 新造 option）。
 */

import { Select } from 'antd'
import { useMemo } from 'react'
import { useLlmConfigQuery } from '@/hooks/queries/useLlmQueries'
import { usePipelineStatesQuery } from '@/hooks/queries/usePipelineRunsQuery'
import { useActiveTabPipelineId } from '@/hooks/useActiveExecutionContext'
import { mapStateInfoToViewModel } from '@/services/api/pipelines'

interface ModelOption {
  value: string
  label: string
}

interface ModelEntry {
  model_name?: string
  display_name?: string
}

/** 名 → 注册表键三级反查：model_name 精确 → 键精确 → 键忽略大小写
 *  （useModelContextInfo 同款三级，网关键 gw-* 与 model_name 形态不一致是常态）；
 *  未命中返回 ''，由调用方回落空态占位。 */
function lookupRegistryKey(models: Record<string, ModelEntry>, name: string): string {
  for (const [key, m] of Object.entries(models)) {
    if (m.model_name === name || key === name) return key
  }
  const lower = name.toLowerCase()
  for (const key of Object.keys(models)) {
    if (key.toLowerCase() === lower) return key
  }
  return ''
}

export function ModelSelectWidget(props: Record<string, unknown>) {
  const { title, value, onChange, disabled, fallbackModelName } = props as {
    title?: string
    value?: { model_id?: string }
    onChange?: (values: Record<string, unknown>) => void
    disabled?: boolean
    fallbackModelName?: string
  }
  const explicit = value?.model_id ?? ''

  const { data: llmConfig } = useLlmConfigQuery()
  const models = llmConfig?.models ?? {}
  const options = useMemo<ModelOption[]>(
    () =>
      Object.entries(models).map(([key, m]) => ({
        value: key,
        label: m.display_name || key,
      })),
    [models],
  )

  /** 当前实际模型（管道 state 行 llm_model）→ 反查注册表键：无显式选择时
   *  停留在当前模型选项；state 行未出（新会话首轮前）→ fallback 或空态占位。 */
  const pipelineId = useActiveTabPipelineId()
  const { data: states } = usePipelineStatesQuery()
  const restingValue = useMemo(() => {
    const row = pipelineId && states ? states[pipelineId] : undefined
    const name = row ? mapStateInfoToViewModel(row).llmModel : undefined
    if (!name) return ''
    return lookupRegistryKey(models, name)
  }, [pipelineId, states, models])

  /** 默认模型名（agent 头/系统默认）→ 反查注册表键；反查失败 '' = 占位 */
  const fallbackValue = useMemo(
    () => (fallbackModelName ? lookupRegistryKey(models, fallbackModelName) : ''),
    [fallbackModelName, models],
  )

  const shownValue = useMemo(() => {
    if (explicit && options.some((o) => o.value === explicit)) return explicit
    return restingValue || fallbackValue
  }, [explicit, options, restingValue, fallbackValue])

  return (
    <Select<ModelOption['value']>
      size="small"
      variant="borderless"
      className="min-w-0"
      style={{ minWidth: 96, maxWidth: 220 }}
      showSearch
      placeholder={title ?? '模型'}
      value={shownValue || undefined}
      options={options}
      optionFilterProp="label"
      disabled={disabled}
      onChange={(v) => onChange?.({ model_id: v ?? '' })}
      popupMatchSelectWidth={false}
      data-testid="model-select"
    />
  )
}

export default ModelSelectWidget
