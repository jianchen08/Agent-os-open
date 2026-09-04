/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * LlmSettingsPage 组件测试（2 Tab 重构版）
 *
 * 覆盖「提供商与密钥」优先的核心交互：
 * - 默认打开提供商 Tab，未配置 Key 的提供者排前、显示 env var 提示
 * - 死掉的「模型参数」Tab 已删除（仅 2 个 Tab）
 * - 填 Key / 更新 Key → updateProviderConfig（明文 keys[0].api_key）
 * - 拉取模型对话框：远端列表勾选 + 自定义输入 → addModel 批量写入
 * - 模型 Tab：每模型「参数」编辑 → updateModel(default_params 合并保留)
 *
 * 测试策略：Mock 仅外部依赖（API 层 + UI 基础组件），组件真实渲染。
 */

import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { toast } from '@/components/ui/sonner'
import { renderWithProviders } from '@/test/renderWithProviders'
import { LlmSettingsPage } from '../LlmSettingsPage'

// ── Mock API 层 ──
const mockGetLLMConfig = vi.fn()
const mockGetDefaults = vi.fn()
const mockGetProviderTypes = vi.fn()
const mockGetRemoteModels = vi.fn()
const mockUpdateProviderConfig = vi.fn()
const mockAddModel = vi.fn()
const mockUpdateModel = vi.fn()

vi.mock('@/services/api/config', () => ({
  getLLMConfig: (...args: unknown[]) => mockGetLLMConfig(...args),
  getDefaults: (...args: unknown[]) => mockGetDefaults(...args),
  getProviderTypes: (...args: unknown[]) => mockGetProviderTypes(...args),
  getRemoteModels: (...args: unknown[]) => mockGetRemoteModels(...args),
  addModel: (...args: unknown[]) => mockAddModel(...args),
  updateModel: (...args: unknown[]) => mockUpdateModel(...args),
  deleteModel: vi.fn(),
  addProvider: vi.fn(),
  deleteProvider: vi.fn(),
  updateProviderConfig: (...args: unknown[]) => mockUpdateProviderConfig(...args),
}))

// ── Mock UI 基础组件（简化依赖，聚焦业务行为）──
vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, ...props }: any) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}))

vi.mock('@/components/ui/input', () => ({
  Input: ({ value, onChange, type, ...props }: any) => (
    <input value={value ?? ''} onChange={onChange} type={type} {...props} />
  ),
}))

vi.mock('@/components/ui/sonner', () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  },
}))

vi.mock('@/components/ui/select', async () => {
  const { createContext, useContext } = await import('react')
  // 最小上下文：SelectItem 点击回传 onValueChange，驱动真实选择逻辑
  const SelectCtx = createContext<{ onValueChange?: (v: string) => void }>({})
  return {
    Select: ({ children, onValueChange }: any) => (
      <SelectCtx.Provider value={{ onValueChange }}>{children}</SelectCtx.Provider>
    ),
    SelectTrigger: ({ children }: any) => <div>{children}</div>,
    SelectValue: ({ placeholder }: any) => <span>{placeholder}</span>,
    SelectContent: ({ children }: any) => <div>{children}</div>,
    SelectGroup: ({ children }: any) => <div>{children}</div>,
    SelectLabel: ({ children }: any) => <div>{children}</div>,
    SelectItem: ({ value, children }: any) => {
      const { onValueChange: onItemChange } = useContext(SelectCtx)
      return (
        <button type="button" onClick={() => onItemChange?.(value)}>
          {children}
        </button>
      )
    },
  }
})

vi.mock('@/components/ui/Modal', () => ({
  Modal: ({ open, children }: any) => (open ? <div role="dialog">{children}</div> : null),
}))

/** 样例 LLM 配置：预置提供者（已配/未配）+ 自定义 + 一个模型 */
const sampleLLMConfig = {
  models: {
    'deepseek-v4-flash': {
      provider: 'deepseek',
      model_name: 'deepseek-v4-flash',
      display_name: 'DeepSeek V4 Flash',
      default_params: { temperature: 0.5, max_tokens: 100000 },
    },
  },
  providers: {
    openai: {
      type: 'openai',
      api_base: 'https://api.openai.com/v1',
      keys: [{ id: 'openai_main', api_key: 'sk-1********abcd', max_concurrent: 6, rpm: 10 }],
      has_key: true,
      env_var: 'OPENAI_API_KEY',
    },
    qwen: {
      type: 'openai',
      api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      keys: [{ id: 'qwen_main', api_key: '${DASHSCOPE_API_KEY}', max_concurrent: 6, rpm: 10 }],
      has_key: false,
      env_var: 'DASHSCOPE_API_KEY',
    },
    myproxy: {
      type: 'openai',
      api_base: 'https://my.proxy/v1',
      keys: [],
      has_key: false,
      env_var: null,
    },
  },
  defaults: { chat: 'deepseek-v4-flash', embedding: '', tiers: {} },
}

const sampleDefaults = { chat: 'deepseek-v4-flash', embedding: '', tiers: {} }

async function renderLoaded() {
  mockGetLLMConfig.mockResolvedValue(sampleLLMConfig)
  mockGetDefaults.mockResolvedValue(sampleDefaults)
  mockGetProviderTypes.mockResolvedValue({ types: ['openai', 'anthropic', 'zai'] })
  renderWithProviders(<LlmSettingsPage />)
  await waitFor(() => {
    expect(screen.getByText('通义千问')).toBeInTheDocument()
  })
}

describe('LlmSettingsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('默认打开「提供商与密钥」Tab，仅 2 个 Tab（死掉的「模型参数」已删除）', async () => {
    await renderLoaded()
    // Tab 数量：提供商与密钥 + 模型
    const tablist = screen.getByRole('tablist')
    expect(within(tablist).getAllByRole('tab')).toHaveLength(2)
    expect(screen.getByRole('tab', { name: '提供商与密钥' })).toBeInTheDocument()
    expect(screen.queryByText('模型参数')).not.toBeInTheDocument()
  })

  it('分组展示提供者；未配置 Key 的排前并显示 env var 提示', async () => {
    await renderLoaded()
    expect(screen.getByText('国内（1）')).toBeInTheDocument() // qwen
    expect(screen.getByText('国际（1）')).toBeInTheDocument() // openai
    expect(screen.getByText('自定义（1）')).toBeInTheDocument() // myproxy
    // 未配置提示：env var 占位符可见
    expect(screen.getByText('DASHSCOPE_API_KEY')).toBeInTheDocument()
    // 状态徽标（未配置 ×2：qwen + myproxy；已配置 ×1：openai）
    expect(screen.getAllByText('未配置')).toHaveLength(2)
    expect(screen.getAllByText('已配置')).toHaveLength(1)
  })

  it('未配置提供者内嵌填 Key：输入明文保存 → updateProviderConfig', async () => {
    await renderLoaded()
    const input = screen.getByLabelText('填写 qwen 的 API Key')
    fireEvent.change(input, { target: { value: 'sk-new-qwen-key' } })
    // 作用域限定在 qwen 卡片内（myproxy 也有「保存」按钮）
    const card = input.closest('div[class*="space-y-2"]') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: '保存' }))
    await waitFor(() => {
      expect(mockUpdateProviderConfig).toHaveBeenCalledWith(
        'qwen',
        { keys: [{ id: 'qwen_main', api_key: 'sk-new-qwen-key' }] },
      )
    })
  })

  it('已配置提供者：更新 Key → updateProviderConfig（明文）', async () => {
    await renderLoaded()
    expect(screen.getByText('Key: sk-1********abcd')).toBeInTheDocument()
    const input = screen.getByPlaceholderText('输入新的 API Key')
    fireEvent.change(input, { target: { value: 'sk-rotated' } })
    fireEvent.click(screen.getByRole('button', { name: '更新 Key' }))
    await waitFor(() => {
      expect(mockUpdateProviderConfig).toHaveBeenCalledWith(
        'openai',
        { keys: [{ id: 'openai_main', api_key: 'sk-rotated' }] },
      )
    })
  })

  it('拉取模型对话框：勾选远端模型 + 自定义输入 → 批量 addModel', async () => {
    await renderLoaded()
    mockGetRemoteModels.mockResolvedValue({
      provider: 'openai',
      models: [
        { id: 'gpt-5.1', owned_by: 'openai' },
        { id: 'gpt-5.1-mini', owned_by: 'openai' },
      ],
    })
    mockAddModel.mockResolvedValue({
      models: { ...sampleLLMConfig.models },
      added_ids: ['gpt-5.1-mini', 'gpt-6-preview'],
    })

    // 作用域限定在 openai 卡片内（未配置的提供者也有禁用的「拉取模型」按钮）
    const masked = screen.getByText('Key: sk-1********abcd')
    const card = masked.closest('div[class*="space-y-2"]') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /拉取模型/ }))
    await waitFor(() => {
      expect(mockGetRemoteModels).toHaveBeenCalledWith('openai')
      expect(screen.getByText('gpt-5.1')).toBeInTheDocument()
    })

    // 勾选一个远端模型
    fireEvent.click(screen.getByText('gpt-5.1-mini'))

    // 自定义输入：列表没有的模型
    const customInput = screen.getByPlaceholderText(/自定义模型名/)
    fireEvent.change(customInput, { target: { value: 'gpt-6-preview' } })
    fireEvent.keyDown(customInput, { key: 'Enter' })
    expect(screen.getByText('gpt-6-preview')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /添加所选/ }))

    await waitFor(() => {
      expect(mockAddModel).toHaveBeenCalledTimes(2)
      expect(mockAddModel).toHaveBeenCalledWith('gpt-5.1-mini', {
        provider: 'openai',
        model_name: 'gpt-5.1-mini',
        display_name: 'gpt-5.1-mini',
      })
      expect(mockAddModel).toHaveBeenCalledWith('gpt-6-preview', {
        provider: 'openai',
        model_name: 'gpt-6-preview',
        display_name: 'gpt-6-preview',
      })
    })
  })

  it('模型 Tab：每模型完整设置（上下文/采样/think 参数）→ updateModel 合并保留原字段', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('tab', { name: '模型' }))
    await waitFor(() => {
      expect(screen.getByText('默认模型')).toBeInTheDocument()
    })

    // 展开参数面板（添加表单也挂了同一编辑器，查询须限定在模型行面板内）
    fireEvent.click(screen.getByRole('button', { name: /参数/ }))
    const panel = screen.getByRole('button', { name: /参数/ }).closest('div.bg-card') as HTMLElement

    // 上下文窗口（样例模型未设置 → 空输入，填入值）
    const ctxInput = within(panel).getByPlaceholderText('未设置')
    fireEvent.change(ctxInput, { target: { value: '1000000' } })

    // 采样参数：temperature 0.5 → 0.9
    fireEvent.change(within(panel).getByDisplayValue('0.5'), { target: { value: '0.9' } })

    // think 参数：思考模式/推理力度（原生 select）
    fireEvent.change(within(panel).getByLabelText('思考模式'), { target: { value: 'enabled' } })
    fireEvent.change(within(panel).getByLabelText('推理力度'), { target: { value: 'high' } })

    // 自定义参数：字符串 + 数字（类型自动解析）
    fireEvent.change(within(panel).getByLabelText('自定义参数名'), { target: { value: 'service_tier' } })
    fireEvent.change(within(panel).getByLabelText('自定义参数值'), { target: { value: 'auto' } })
    fireEvent.click(within(panel).getByRole('button', { name: '加入' }))
    fireEvent.change(within(panel).getByLabelText('自定义参数名'), { target: { value: 'top_k' } })
    fireEvent.change(within(panel).getByLabelText('自定义参数值'), { target: { value: '50' } })
    fireEvent.keyDown(within(panel).getByLabelText('自定义参数值'), { key: 'Enter' })
    // chip 可见（字符串与数字各一条）
    expect(screen.getByText(/service_tier=auto/)).toBeInTheDocument()
    expect(screen.getByText(/top_k=50/)).toBeInTheDocument()

    mockUpdateModel.mockResolvedValue({ models: { ...sampleLLMConfig.models } })
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))

    await waitFor(() => {
      expect(mockUpdateModel).toHaveBeenCalledWith('deepseek-v4-flash', {
        context_window: 1000000,
        reasoning_model: false,
        default_params: {
          temperature: 0.9,
          max_tokens: 100000,
          top_p: 1,
          thinking: { type: 'enabled' },
          reasoning_effort: 'high',
          service_tier: 'auto',
          top_k: 50,
        },
      })
    })
  })

  it('手动添加模型：模型名称即身份，参数随添加一并提交，自动改名时提示最终 ID', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('tab', { name: '模型' }))
    const addSection = (await waitFor(() =>
      screen.getByRole('heading', { name: '添加模型' }).closest('section'),
    )) as HTMLElement

    // 填模型名称 + 选提供商（样例配置已有 deepseek 提供商下的同名模型 →
    // 后端按 added_ids 回报自动改名结果）
    fireEvent.change(
      within(addSection).getByPlaceholderText(/作为模型 ID 与调用名/),
      { target: { value: 'deepseek-v4-flash' } },
    )
    fireEvent.click(within(addSection).getByRole('button', { name: 'openai' }))

    // 参数随添加直接填写：默认思考模式 + 多模态图片
    fireEvent.change(within(addSection).getByLabelText('思考模式'), {
      target: { value: 'enabled' },
    })
    fireEvent.click(within(addSection).getByLabelText('图片'))

    mockAddModel.mockResolvedValue({
      models: {
        ...sampleLLMConfig.models,
        'deepseek-v4-flash-openai': {
          provider: 'openai',
          model_name: 'deepseek-v4-flash',
          display_name: '',
        },
      },
      added_ids: ['deepseek-v4-flash-openai'],
    })
    fireEvent.click(within(addSection).getByRole('button', { name: '添加模型' }))

    await waitFor(() => {
      expect(mockAddModel).toHaveBeenCalledWith('deepseek-v4-flash', {
        provider: 'openai',
        model_name: 'deepseek-v4-flash',
        display_name: '',
        reasoning_model: false,
        default_params: {
          temperature: 0.7,
          max_tokens: 4096,
          top_p: 1,
          thinking: { type: 'enabled' },
        },
        multimodal: {
          supports_image: true,
          supported_image_types: ['image/jpeg', 'image/png', 'image/gif', 'image/webp'],
          max_image_size: 20 * 1024 * 1024,
        },
      })
    })
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalled()
    })
  })
})
