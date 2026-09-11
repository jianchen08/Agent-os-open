/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ModelParamsEditor 纯函数测试：draftFromModel / buildModelFields / buildRemoteModelFields
 * 序列化契约。
 *
 * 覆盖：
 * - 空草稿只产出基础字段（不写 thinking_strength_params / multimodal 键）
 * - yaml 形状模型 → 草稿 → 字段：值往返（字节↔MB）、default_params 未管理键
 *   保留（extra_body / reasoning_retention）、multimodal 未管理键保留
 *   （supports_document）
 * - 思考强度映射：档位留空不写该键（回退内置表）；原本配置过、全空 → 显式 {}
 * - 多模态：勾选 → supports/types/字节上限；原本启用现取消 → 显式 false
 * - 拉取模型添加载荷：有 litellm 事实用事实，无事实走 DEFAULT_MAX_TOKENS 兜底
 */
import { describe, expect, it } from 'vitest'
import {
  buildModelFields,
  buildRemoteModelFields,
  draftFromModel,
  emptyModelParamsDraft,
  parseCustomValue,
  DEFAULT_MAX_TOKENS,
} from '../modelParams'
import type { ModelConfig } from '@/services/api/config'

describe('parseCustomValue', () => {
  it('true/false → 布尔，数字 → 数值，其余原样字符串', () => {
    expect(parseCustomValue('true')).toBe(true)
    expect(parseCustomValue('false')).toBe(false)
    expect(parseCustomValue('50')).toBe(50)
    expect(parseCustomValue('auto')).toBe('auto')
  })
})

describe('buildModelFields', () => {
  it('空草稿只产出基础字段，不写强度/多模态键', () => {
    const fields = buildModelFields(emptyModelParamsDraft())
    expect(fields).toEqual({
      reasoning_model: false,
      default_params: { temperature: 0.7, max_tokens: DEFAULT_MAX_TOKENS, top_p: 1 },
    })
    expect(fields.thinking_strength_params).toBeUndefined()
    expect(fields.multimodal).toBeUndefined()
  })

  it('「保持原样」不写 thinking/reasoning_effort；update 保留 default_params 未管理键', () => {
    const original: ModelConfig = {
      provider: 'zhipu_coding',
      model_name: 'glm-5.2',
      display_name: 'GLM-5.2',
      default_params: {
        temperature: 0.5,
        extra_body: { tool_stream: true },
        reasoning_retention: { sample_interval: 3 },
      },
    }
    const draft = draftFromModel(original)
    const fields = buildModelFields(draft, original)
    expect(fields.default_params).toEqual({
      temperature: 0.5,
      max_tokens: DEFAULT_MAX_TOKENS,
      top_p: 1,
      extra_body: { tool_stream: true },
      reasoning_retention: { sample_interval: 3 },
    })
    expect((fields.default_params as Record<string, unknown>).thinking).toBeUndefined()
  })

  it('思考强度映射按档位序列化；全空且原本未配置 → 不写键', () => {
    const draft = emptyModelParamsDraft()
    draft.strength.high = { thinkingType: '', effort: 'max' }
    draft.strength.low = { thinkingType: 'disabled', effort: '' }
    const fields = buildModelFields(draft)
    expect(fields.thinking_strength_params).toEqual({
      high: { reasoning_effort: 'max' },
      low: { thinking: { type: 'disabled' } },
    })
  })

  it('原本配置过强度映射、用户清空档位 → 显式 {} 复位为内置默认表', () => {
    const original: ModelConfig = {
      provider: 'deepseek',
      model_name: 'deepseek-v4-flash',
      display_name: '',
      thinking_strength_params: { high: { reasoning_effort: 'max' } },
    }
    // 不清空直接保存 = 往返保留
    expect(buildModelFields(draftFromModel(original), original).thinking_strength_params).toEqual({
      high: { reasoning_effort: 'max' },
    })
    // 用户清空全部档位 → 显式 {}
    const draft = draftFromModel(original)
    draft.strength.high = { thinkingType: '', effort: '' }
    expect(buildModelFields(draft, original).thinking_strength_params).toEqual({})
  })

  it('思考强度档位由声明下发：levels 参数驱动草稿形状与序列化（插件声明携带白名单）', () => {
    const levels = ['turbo', 'eco']
    const draft = emptyModelParamsDraft(levels)
    expect(Object.keys(draft.strength).sort()).toEqual(['eco', 'turbo'])
    draft.strength.turbo = { thinkingType: '', effort: 'max' }
    expect(buildModelFields(draft).thinking_strength_params).toEqual({
      turbo: { reasoning_effort: 'max' },
    })
    // 回显：按传入档位读取模型配置，未配置档位保持空草稿
    const original = {
      provider: 'x',
      model_name: 'x',
      display_name: '',
      thinking_strength_params: { eco: { reasoning_effort: 'low' } },
    } as ModelConfig
    const back = draftFromModel(original, levels)
    expect(back.strength.eco.effort).toBe('low')
    expect(back.strength.turbo.thinkingType).toBe('')
    expect(back.strength.turbo.effort).toBe('')
  })

  it('关闭档（off）映射可回显且保存不丢：缺档位会在保存时静默删除该档映射', () => {
    const original = {
      provider: 'deepseek',
      model_name: 'deepseek-v4-flash',
      display_name: '',
      thinking_strength_params: {
        high: { reasoning_effort: 'max' },
        off: { thinking: { type: 'disabled' } },
      },
    } as ModelConfig
    // 缺省档位词汇须含 off，否则 off 映射在草稿阶段就不存在
    const draft = draftFromModel(original)
    expect(draft.strength.off.thinkingType).toBe('disabled')
    // 原样保存 → off 映射仍在（往返不丢档）
    expect(buildModelFields(draft, original).thinking_strength_params).toEqual({
      high: { reasoning_effort: 'max' },
      off: { thinking: { type: 'disabled' } },
    })
  })

  it('多模态：勾选 → supports/types/字节上限；未勾选不写', () => {
    const draft = emptyModelParamsDraft()
    draft.multimodal.image = {
      enabled: true,
      types: 'image/png, image/jpeg',
      maxSizeMb: '20',
    }
    const fields = buildModelFields(draft)
    expect(fields.multimodal).toEqual({
      supports_image: true,
      supported_image_types: ['image/png', 'image/jpeg'],
      max_image_size: 20 * 1024 * 1024,
    })
  })

  it('多模态回显往返：字节→MB→字节；取消已启用模态写显式 false；supports_document 保留', () => {
    const original = {
      provider: 'minimax',
      model_name: 'MiniMax-M3',
      display_name: '',
      multimodal: {
        supports_image: true,
        supported_image_types: ['image/jpeg', 'image/webp'],
        max_image_size: 20971520,
        supports_video: true,
        max_video_size: 104857600,
        supports_document: true,
      },
    } as ModelConfig
    const draft = draftFromModel(original)
    expect(draft.multimodal.image.maxSizeMb).toBe('20')
    expect(draft.multimodal.video.maxSizeMb).toBe('100')
    // 用户取消图片、保留视频
    draft.multimodal.image.enabled = false

    const fields = buildModelFields(draft, original)
    expect(fields.multimodal).toEqual({
      supports_document: true,
      supports_image: false,
      supports_video: true,
      // 原条目未写视频类型清单 → 草稿回填标准清单并随保存落盘
      supported_video_types: ['video/mp4', 'video/webm'],
      max_video_size: 104857600,
    })
  })
})

describe('draftFromModel', () => {
  it('回显上下文窗口/思考/强度映射', () => {
    const model = {
      provider: 'deepseek',
      model_name: 'deepseek-v4-flash',
      display_name: '',
      context_window: 1000000,
      reasoning_model: true,
      default_params: {
        temperature: 0.7,
        max_tokens: 100000,
        top_p: 1,
        thinking: { type: 'enabled' },
        reasoning_effort: 'max',
      },
      thinking_strength_params: {
        high: { reasoning_effort: 'max' },
        low: { thinking: { type: 'disabled' } },
      },
    } as ModelConfig
    const draft = draftFromModel(model)
    expect(draft.contextWindow).toBe('1000000')
    expect(draft.reasoningModel).toBe(true)
    expect(draft.thinkingType).toBe('enabled')
    expect(draft.effort).toBe('max')
    expect(draft.strength.high).toEqual({ thinkingType: '', effort: 'max' })
    expect(draft.strength.low).toEqual({ thinkingType: 'disabled', effort: '' })
    expect(draft.strength.medium).toEqual({ thinkingType: '', effort: '' })
  })
})

describe('buildRemoteModelFields - 拉取模型添加载荷', () => {
  it('有 litellm 事实 → context_window 与 max_tokens 都用事实值', () => {
    const fields = buildRemoteModelFields({
      context_window: 1000000,
      max_output_tokens: 128000,
    })

    expect(fields.context_window).toBe(1000000)
    expect((fields.default_params as Record<string, unknown>).max_tokens).toBe(128000)
  })

  it('无事实 → 不写 context_window，max_tokens 走 DEFAULT_MAX_TOKENS（非 4096）', () => {
    const fields = buildRemoteModelFields({})

    expect(fields.context_window).toBeUndefined()
    expect((fields.default_params as Record<string, unknown>).max_tokens).toBe(
      DEFAULT_MAX_TOKENS,
    )
    // 4096 对推理模型过低（一轮思考即耗尽输出预算）——兜底必须显著高于它
    expect(DEFAULT_MAX_TOKENS).toBeGreaterThan(4096)
  })

  it('上限为 0/负数等非法事实 → 视为无事实，回落兜底（不写 0）', () => {
    const fields = buildRemoteModelFields({ context_window: 0, max_output_tokens: 0 })

    expect(fields.context_window).toBeUndefined()
    expect((fields.default_params as Record<string, unknown>).max_tokens).toBe(
      DEFAULT_MAX_TOKENS,
    )
  })

  it('只给 context_window 不给输出上限 → 上下文用事实、输出走兜底', () => {
    const fields = buildRemoteModelFields({ context_window: 200000 })

    expect(fields.context_window).toBe(200000)
    expect((fields.default_params as Record<string, unknown>).max_tokens).toBe(
      DEFAULT_MAX_TOKENS,
    )
  })
})
