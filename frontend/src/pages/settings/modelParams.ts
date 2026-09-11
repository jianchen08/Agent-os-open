/**
 * 模型参数草稿与序列化（纯函数，无组件）：被 ModelParamsEditor 与 LlmSettingsPage 消费。
 *
 * 覆盖 llm.yaml 模型条目的可编辑面：
 * - 基础：context_window / default_params 采样参数（temperature/max_tokens/top_p）
 * - 推理：reasoning_model 标记 + 默认 thinking.type / reasoning_effort
 * - 思考强度映射：thinking_strength_params（high/medium/low → thinking /
 *   reasoning_effort）。白名单只认这两个键（llm_core
 *   resolve_thinking_strength_params 过滤）；留空档位不写该键，运行时该档位
 *   回退内置默认表（reasoning_effort low/medium/high）
 * - 多模态：multimodal 节（supports_image/audio/video + 类型清单 + MB 上限，
 *   落盘换算为字节；未管理的键如 supports_document 更新时原样保留）
 * - 自定义参数：合并进 default_params 随请求发送
 *
 * buildModelFields 负责草稿 → 配置字段序列化；update 场景传入 original
 * 以合并保留 default_params 其他键（extra_body / reasoning_retention 等）。
 */
import type { ModelConfig } from '@/services/api/config'

export type CustomParam = { key: string; value: string }

export type StrengthLevelDraft = { thinkingType: string; effort: string }

/**
 * 思考强度档位允许写入的参数键（前端写入面单一出口）。与 llm_core
 * resolve_thinking_strength_params 的 _THINKING_STRENGTH_ALLOWED 白名单
 * 对账（源码扫描契约闸：__tests__/modelParams.contract.test.ts）——后端
 * 扩/删键时契约测试变红，镜像漂移在此显式暴露而非静默丢键。
 */
export const STRENGTH_PARAM_KEYS = ['thinking', 'reasoning_effort'] as const

export type ModalityDraft = { enabled: boolean; types: string; maxSizeMb: string }

export interface ModelParamsDraft {
  contextWindow: string
  temperature: number
  maxTokens: number
  topP: number
  reasoningModel: boolean
  /** 模型默认思考模式（'' = 保持原样不写） */
  thinkingType: string
  /** 模型默认推理力度（'' = 保持原样不写） */
  effort: string
  customParams: CustomParam[]
  customKey: string
  customValue: string
  /** 键 = 思考强度档位（档位词汇由 llm_service 预置声明下发） */
  strength: Record<string, StrengthLevelDraft>
  multimodal: Record<'image' | 'audio' | 'video', ModalityDraft>
}

const MB = 1024 * 1024

/**
 * 最大输出默认值（新添加模型、且无从得知真实上限时）。
 *
 * 4096 对推理模型过低：一轮 max 档思考就能耗尽输出预算，正文一字未出
 * （历史事故见 commit 633fc8f1f）。上限事实可得时（litellm 注册表经
 * /remote-models 下发）以事实为准，此值只作兜底。
 */
export const DEFAULT_MAX_TOKENS = 32768

/**
 * 思考强度档位缺省词汇。真值由 llm_service /ext 预置端点声明下发
 * （thinking_strength.levels，与 llm_core 过滤白名单对账）——组件/页面
 * 接收声明值传入；本缺省仅供类型缺省与测试兜底。
 * 须覆盖聊天页全部档位（含 off）：清单缺档位会在保存时静默删除该档映射。
 */
export const DEFAULT_STRENGTH_LEVELS: readonly string[] = ['high', 'medium', 'low', 'off']
const MODALITIES = ['image', 'audio', 'video'] as const

/** 勾选多模态时的类型清单默认值（与 multimodal 插件/asr 支持的 MIME 对齐） */
const STANDARD_TYPES: Record<(typeof MODALITIES)[number], string> = {
  image: 'image/jpeg, image/png, image/gif, image/webp',
  audio: 'audio/mpeg, audio/wav, audio/x-m4a, audio/webm',
  video: 'video/mp4, video/webm',
}
const DEFAULT_MAX_MB: Record<(typeof MODALITIES)[number], string> = {
  image: '20',
  audio: '20',
  video: '100',
}

/** 自定义参数值类型解析：true/false → 布尔，数字 → 数值，其余原样字符串 */
export const parseCustomValue = (v: string): unknown => {
  if (v === 'true') return true
  if (v === 'false') return false
  if (v !== '' && !Number.isNaN(Number(v))) return Number(v)
  return v
}

/** 空草稿（新添加模型的起点；levels = 声明下发的思考强度档位） */
export function emptyModelParamsDraft(
  levels: readonly string[] = DEFAULT_STRENGTH_LEVELS,
): ModelParamsDraft {
  const strength: Record<string, StrengthLevelDraft> = {}
  for (const level of levels) strength[level] = { thinkingType: '', effort: '' }
  return {
    contextWindow: '',
    temperature: 0.7,
    maxTokens: DEFAULT_MAX_TOKENS,
    topP: 1,
    reasoningModel: false,
    thinkingType: '',
    effort: '',
    customParams: [],
    customKey: '',
    customValue: '',
    strength,
    multimodal: {
      image: { enabled: false, types: STANDARD_TYPES.image, maxSizeMb: DEFAULT_MAX_MB.image },
      audio: { enabled: false, types: STANDARD_TYPES.audio, maxSizeMb: DEFAULT_MAX_MB.audio },
      video: { enabled: false, types: STANDARD_TYPES.video, maxSizeMb: DEFAULT_MAX_MB.video },
    },
  }
}

/**
 * 远端模型条目 → 添加载荷（拉取模型对话框的"添加"路径）。
 *
 * 上限事实由 /remote-models 经 litellm 注册表下发（厂商 /models 端点不返回
 * 上下文与输出上限）：context_window 与 max_tokens 有事实用事实；无事实时
 * max_tokens 落 DEFAULT_MAX_TOKENS 兜底（不用 4096——推理模型一轮思考即耗尽）。
 */
export function buildRemoteModelFields(model: {
  context_window?: number
  max_output_tokens?: number
}): Partial<ModelConfig> {
  const fields: Partial<ModelConfig> = {}
  if (typeof model.context_window === 'number' && model.context_window > 0) {
    fields.context_window = model.context_window
  }
  fields.default_params = {
    temperature: 0.7,
    max_tokens:
      typeof model.max_output_tokens === 'number' && model.max_output_tokens > 0
        ? model.max_output_tokens
        : DEFAULT_MAX_TOKENS,
    top_p: 1,
  }
  return fields
}

/** 已有模型条目 → 草稿（模型行「参数」面板回显；levels = 声明下发的思考强度档位） */
export function draftFromModel(
  model: ModelConfig | undefined,
  levels: readonly string[] = DEFAULT_STRENGTH_LEVELS,
): ModelParamsDraft {
  const draft = emptyModelParamsDraft(levels)
  if (!model) return draft
  const params = (model.default_params ?? {}) as Record<string, unknown>
  if (model.context_window != null) draft.contextWindow = String(model.context_window)
  if (typeof params.temperature === 'number') draft.temperature = params.temperature
  if (typeof params.max_tokens === 'number') draft.maxTokens = params.max_tokens
  if (typeof params.top_p === 'number') draft.topP = params.top_p
  draft.reasoningModel = model.reasoning_model ?? false
  draft.thinkingType = (params.thinking as { type?: string } | undefined)?.type ?? ''
  draft.effort = typeof params.reasoning_effort === 'string' ? params.reasoning_effort : ''

  const strength = model.thinking_strength_params
  for (const level of Object.keys(draft.strength)) {
    const lv = strength?.[level]
    draft.strength[level].thinkingType =
      (lv?.thinking as { type?: string } | undefined)?.type ?? ''
    draft.strength[level].effort =
      typeof lv?.reasoning_effort === 'string' ? lv.reasoning_effort : ''
  }

  const mm = (model.multimodal ?? null) as Record<string, unknown> | null
  if (mm) {
    for (const m of MODALITIES) {
      const d = draft.multimodal[m]
      d.enabled = mm[`supports_${m}`] === true
      const types = mm[`supported_${m}_types`]
      if (Array.isArray(types) && types.length > 0) d.types = types.join(', ')
      const size = mm[`max_${m}_size`]
      if (typeof size === 'number' && size > 0) d.maxSizeMb = String(Math.round(size / MB))
    }
  }
  return draft
}

/** 草稿 → 模型配置字段（update 场景传 original 合并保留原值） */
export function buildModelFields(
  draft: ModelParamsDraft,
  original?: ModelConfig,
): Partial<ModelConfig> {
  const fields: Partial<ModelConfig> = {}
  if (draft.contextWindow.trim() !== '') fields.context_window = Number(draft.contextWindow)
  fields.reasoning_model = draft.reasoningModel

  const originalParams = (original?.default_params ?? {}) as Record<string, unknown>
  const nextParams: Record<string, unknown> = {
    ...originalParams,
    temperature: draft.temperature,
    max_tokens: draft.maxTokens,
    top_p: draft.topP,
  }
  if (draft.thinkingType) {
    nextParams.thinking = {
      ...((originalParams.thinking as Record<string, unknown>) ?? {}),
      type: draft.thinkingType,
    }
  }
  if (draft.effort) nextParams.reasoning_effort = draft.effort
  for (const p of draft.customParams) nextParams[p.key] = parseCustomValue(p.value)
  fields.default_params = nextParams

  const strengthOut: Record<string, Record<string, unknown>> = {}
  for (const [level, lv] of Object.entries(draft.strength)) {
    // 写入键集合 = STRENGTH_PARAM_KEYS（llm_core 白名单镜像，契约测试对账）
    const entry: Record<string, unknown> = {}
    if (lv.thinkingType) entry.thinking = { type: lv.thinkingType }
    if (lv.effort) entry.reasoning_effort = lv.effort
    if (Object.keys(entry).length > 0) strengthOut[level] = entry
  }
  if (Object.keys(strengthOut).length > 0 || original?.thinking_strength_params != null) {
    // 原本配置过、现全空 → 显式 {}（llm_core 视为回退内置默认表）
    fields.thinking_strength_params = strengthOut
  }

  const originalMm = (original?.multimodal ?? null) as Record<string, unknown> | null
  const managedKeys = new Set(
    MODALITIES.flatMap((m) => [`supports_${m}`, `supported_${m}_types`, `max_${m}_size`]),
  )
  const mmOut: Record<string, unknown> = {}
  if (originalMm) {
    for (const [k, v] of Object.entries(originalMm)) {
      if (!managedKeys.has(k)) mmOut[k] = v
    }
  }
  for (const m of MODALITIES) {
    const d = draft.multimodal[m]
    if (d.enabled) {
      mmOut[`supports_${m}`] = true
      mmOut[`supported_${m}_types`] = d.types
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean)
      const sizeMb = Number(d.maxSizeMb)
      if (Number.isFinite(sizeMb) && sizeMb > 0) mmOut[`max_${m}_size`] = Math.round(sizeMb * MB)
    } else if (originalMm?.[`supports_${m}`] === true) {
      mmOut[`supports_${m}`] = false
    }
  }
  if (Object.keys(mmOut).length > 0) fields.multimodal = mmOut

  return fields
}

