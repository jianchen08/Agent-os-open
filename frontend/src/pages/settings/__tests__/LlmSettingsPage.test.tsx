/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * LlmSettingsPage 组件测试（单界面重构版）
 *
 * 覆盖「提供商选择器 + 同界面模型选择」的核心交互：
 * - 提供商是一个选择控件：每个内置提供商 = 一个选项，另有「自定义」选项；
 *   不再有 Tab 与卡片墙
 * - 选中内置提供商无需填 URL/API Key（已配置时无输入框；未配置时仅给 Key 入口）
 * - 选中「自定义」→ 同一界面下方出现 Base URL 与 API Key 输入框
 * - 模型选择在同一界面完成：拉取列表勾选 + 手填模型名，一次提交写 addModel
 * - 已注册模型逐行条目；tier 区只留选择栏位与当前值（不列模型清单）
 *
 * 测试策略：Mock 仅外部依赖（API 层 + UI 基础组件），组件真实渲染。
 */

import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { toast } from '@/components/ui/sonner'
import { renderWithProviders } from '@/test/renderWithProviders'
import { LlmSettingsPage } from '../LlmSettingsPage'
import { DEFAULT_MAX_TOKENS } from '../modelParams'

// ── Mock API 层 ──
const mock = {
  getLLMConfig: vi.fn(),
  getLLMPresets: vi.fn(),
  getProviderTypes: vi.fn(),
  getRemoteModels: vi.fn(),
  addModel: vi.fn(),
  updateModel: vi.fn(),
  deleteModel: vi.fn(),
  addProvider: vi.fn(),
  deleteProvider: vi.fn(),
  updateProviderConfig: vi.fn(),
  saveDefaults: vi.fn(),
}

vi.mock('@/services/api/config', () => ({
  getLLMConfig: (...a: unknown[]) => mock.getLLMConfig(...a),
  getLLMPresets: (...a: unknown[]) => mock.getLLMPresets(...a),
  getProviderTypes: (...a: unknown[]) => mock.getProviderTypes(...a),
  getRemoteModels: (...a: unknown[]) => mock.getRemoteModels(...a),
  addModel: (...a: unknown[]) => mock.addModel(...a),
  updateModel: (...a: unknown[]) => mock.updateModel(...a),
  deleteModel: (...a: unknown[]) => mock.deleteModel(...a),
  addProvider: (...a: unknown[]) => mock.addProvider(...a),
  deleteProvider: (...a: unknown[]) => mock.deleteProvider(...a),
  updateProviderConfig: (...a: unknown[]) => mock.updateProviderConfig(...a),
  saveDefaults: (...a: unknown[]) => mock.saveDefaults(...a),
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
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
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
      const { onValueChange } = useSelectCtx(SelectCtx)
      return (
        <button type="button" onClick={() => onValueChange?.(value)}>
          {children}
        </button>
      )
    },
  }
  function useSelectCtx(ctx: React.Context<{ onValueChange?: (v: string) => void }>) {
    return useContext(ctx)
  }
})

/** 样例配置：两个内置提供商（openai 已配 / qwen 未配）+ 一个自定义提供商 + 两个模型 */
const sampleLLMConfig = {
  models: {
    'deepseek-v4-flash': {
      provider: 'deepseek',
      model_name: 'deepseek-v4-flash',
      display_name: 'DeepSeek V4 Flash',
      default_params: { temperature: 0.5, max_tokens: 100000 },
    },
    'gpt-5.1': {
      provider: 'openai',
      model_name: 'gpt-5.1',
      display_name: 'GPT-5.1',
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
  defaults: { chat: 'deepseek-v4-flash', embedding: '', tiers: { large: 'gpt-5.1' } },
}

// 预置声明样例（llm_service /ext 端点下发）：声明外 provider（myproxy）落「其他已添加」
const samplePresets = {
  provider_groups: [
    { label: '国内', providers: [['qwen', '通义千问']] },
    { label: '国际', providers: [['openai', 'OpenAI']] },
  ],
  common_provider_types: ['openai'],
  thinking_strength: {
    levels: ['high', 'medium', 'low'],
    allowed_keys: ['thinking', 'reasoning_effort'],
  },
}

async function renderLoaded() {
  mock.getLLMConfig.mockResolvedValue(sampleLLMConfig)
  mock.getLLMPresets.mockResolvedValue(samplePresets)
  mock.getProviderTypes.mockResolvedValue({ types: ['openai', 'anthropic', 'zai'] })
  renderWithProviders(<LlmSettingsPage />)
  await waitFor(() => {
    expect(screen.getByText('已注册模型 (2)')).toBeInTheDocument()
  })
}

/** 模型区（第 2 段）容器：用于把查询限定在「同一界面内完成模型选择」的范围 */
function modelSection(): HTMLElement {
  return screen.getByRole('heading', { name: '模型', level: 3 }).closest('section') as HTMLElement
}

/** 已注册模型段容器（逐行条目） */
function registrySection(): HTMLElement {
  return screen.getByRole('heading', { name: /已注册模型/ }).closest('section') as HTMLElement
}

/** 默认模型（tier）区容器 */
function tierSection(): HTMLElement {
  return screen.getByRole('heading', { name: '默认模型' }).closest('section') as HTMLElement
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('LlmSettingsPage 单界面形态', () => {
  it('提供商改成一个选择控件：内置提供商各占一个选项 + 「自定义」选项，且无 Tab', async () => {
    await renderLoaded()
    // 不再有 Tab 结构（原「提供商与密钥 / 模型」两 Tab 已合并为单界面）
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    // 每个内置提供商是一个选项；声明外的已添加提供商也出现在选项里
    expect(screen.getByRole('button', { name: 'OpenAI' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '通义千问' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'myproxy' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '自定义（新增提供商）' })).toBeInTheDocument()
  })

  it('选中内置且已配置的提供商：无需填 URL/Key（无输入框，仅状态与只读 Base URL）', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'OpenAI' }))
    await waitFor(() => expect(screen.getByText('已配置')).toBeInTheDocument())
    // Base URL 只读展示，不是输入框
    expect(screen.getByText('Base URL: https://api.openai.com/v1')).toBeInTheDocument()
    expect(screen.queryByLabelText('填写 openai 的 API Key')).not.toBeInTheDocument()
    // 「更换 Key」是可选的后续动作，不是必填
    expect(screen.getByRole('button', { name: '更换 Key' })).toBeInTheDocument()
  })

  it('选中内置但未配置的提供商：不给 URL 输入，仅给 Key 填写入口', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: '通义千问' }))
    await waitFor(() => expect(screen.getByText('未配置')).toBeInTheDocument())
    expect(screen.getByLabelText('填写 qwen 的 API Key')).toBeInTheDocument()
    expect(screen.queryByPlaceholderText(/https:\/\/my.proxy/)).not.toBeInTheDocument()
  })

  it('选中「自定义」：同一界面下方出现 Base URL 与 API Key 输入框，提交走 addProvider', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: '自定义（新增提供商）' }))

    const urlInput = await screen.findByLabelText('Base URL')
    const keyInput = screen.getByLabelText('API Key')
    fireEvent.change(screen.getByLabelText('提供商 ID'), { target: { value: 'myproxy2' } })
    fireEvent.change(urlInput, { target: { value: 'https://p2/v1' } })
    fireEvent.change(keyInput, { target: { value: 'sk-p2' } })
    mock.addProvider.mockResolvedValueOnce(sampleLLMConfig.providers)
    fireEvent.click(screen.getByRole('button', { name: '添加提供商' }))

    await waitFor(() =>
      expect(mock.addProvider).toHaveBeenCalledWith('myproxy2', {
        type: 'openai',
        api_base: 'https://p2/v1',
        api_key: 'sk-p2',
      }),
    )
  })

  it('模型选择在同一界面完成：拉取列表勾选 + 手填模型名 → 批量 addModel', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'OpenAI' }))
    mock.getRemoteModels.mockResolvedValue({
      provider: 'openai',
      models: [
        { id: 'gpt-6', owned_by: 'openai' },
        { id: 'gpt-6-mini', owned_by: 'openai' },
      ],
    })
    mock.addModel.mockResolvedValue({ models: sampleLLMConfig.models, added_ids: ['x'] })

    // 拉取与手填都在同一段的同一界面内（不弹窗）
    const panel = modelSection()
    fireEvent.click(within(panel).getByRole('button', { name: /拉取模型列表/ }))
    await waitFor(() => expect(within(panel).getByText('gpt-6')).toBeInTheDocument())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    fireEvent.click(within(panel).getByText('gpt-6'))
    const customInput = within(panel).getByLabelText('自定义模型名')
    fireEvent.change(customInput, { target: { value: 'gpt-6-preview' } })
    fireEvent.keyDown(customInput, { key: 'Enter' })
    fireEvent.click(within(panel).getByRole('button', { name: /添加模型 \(2\)/ }))

    await waitFor(() => {
      expect(mock.addModel).toHaveBeenCalledTimes(2)
      expect(mock.addModel).toHaveBeenCalledWith('gpt-6', {
        provider: 'openai',
        model_name: 'gpt-6',
        display_name: 'gpt-6',
        default_params: { temperature: 0.7, max_tokens: DEFAULT_MAX_TOKENS, top_p: 1 },
      })
      // 手填模型不在远端列表 → 无事实，同样走兜底上限
      expect(mock.addModel).toHaveBeenCalledWith('gpt-6-preview', {
        provider: 'openai',
        model_name: 'gpt-6-preview',
        display_name: 'gpt-6-preview',
        default_params: { temperature: 0.7, max_tokens: DEFAULT_MAX_TOKENS, top_p: 1 },
      })
    })
  })

  it('远端下发上限事实：context_window 与 max_tokens 按事实写入（不覆盖为默认值）', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'OpenAI' }))
    mock.getRemoteModels.mockResolvedValue({
      provider: 'openai',
      models: [
        { id: 'glm-5.2', owned_by: 'z-ai', context_window: 1000000, max_output_tokens: 128000 },
      ],
    })
    mock.addModel.mockResolvedValue({ models: sampleLLMConfig.models, added_ids: ['glm-5.2'] })

    const panel = modelSection()
    fireEvent.click(within(panel).getByRole('button', { name: /拉取模型列表/ }))
    await waitFor(() => expect(within(panel).getByText('glm-5.2')).toBeInTheDocument())
    fireEvent.click(within(panel).getByText('glm-5.2'))
    fireEvent.click(within(panel).getByRole('button', { name: /添加模型/ }))

    await waitFor(() =>
      expect(mock.addModel).toHaveBeenCalledWith('glm-5.2', {
        provider: 'openai',
        model_name: 'glm-5.2',
        display_name: 'glm-5.2',
        context_window: 1000000,
        default_params: { temperature: 0.7, max_tokens: 128000, top_p: 1 },
      }),
    )
  })

  it('已注册模型逐行条目：两个模型两行（显示名 + provider/模型名）', async () => {
    await renderLoaded()
    const registry = registrySection()
    expect(within(registry).getByText('DeepSeek V4 Flash')).toBeInTheDocument()
    expect(within(registry).getByText('GPT-5.1')).toBeInTheDocument()
    expect(within(registry).getByText('deepseek / deepseek-v4-flash')).toBeInTheDocument()
    expect(within(registry).getByText('openai / gpt-5.1')).toBeInTheDocument()
    // 逐行条目各带「参数」「删除」操作（提供商段的删除按钮不在该段内）
    expect(within(registry).getAllByRole('button', { name: /参数/ })).toHaveLength(2)
    expect(within(registry).getAllByRole('button', { name: '删除' })).toHaveLength(2)
  })

  it('tier 区只留选择栏位与当前值：不展示已注册模型清单条目', async () => {
    await renderLoaded()
    const tier = tierSection()
    // 大/中/小三个档位栏位 + 对话/向量用途
    expect(within(tier).getByText('大模型')).toBeInTheDocument()
    expect(within(tier).getByText('中模型')).toBeInTheDocument()
    expect(within(tier).getByText('小模型')).toBeInTheDocument()
    expect(within(tier).getByText('默认对话模型')).toBeInTheDocument()
    expect(within(tier).getByText('默认向量模型')).toBeInTheDocument()
    // 模型清单条目（显示名 / provider 行 / 参数按钮）不在 tier 区内
    expect(within(tier).queryByText('DeepSeek V4 Flash')).not.toBeInTheDocument()
    expect(within(tier).queryByText('openai / gpt-5.1')).not.toBeInTheDocument()
    expect(within(tier).queryByRole('button', { name: /参数/ })).not.toBeInTheDocument()
    expect(within(tier).queryByText(/已注册模型/)).not.toBeInTheDocument()
  })

  it('tier 区改档位后保存 → saveDefaults 带上新的 tiers', async () => {
    await renderLoaded()
    const tier = tierSection()
    // 「中模型」栏位行内选择已注册模型（样例初始只设了 large）
    const mediumRow = within(tier)
      .getByText('中模型')
      .closest('div[class*="flex-col"]') as HTMLElement
    fireEvent.click(within(mediumRow).getByRole('button', { name: 'deepseek-v4-flash' }))
    mock.saveDefaults.mockResolvedValue({
      chat: 'deepseek-v4-flash',
      embedding: '',
      tiers: { large: 'gpt-5.1', medium: 'deepseek-v4-flash' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存默认模型' }))

    await waitFor(() =>
      expect(mock.saveDefaults).toHaveBeenCalledWith({
        chat: 'deepseek-v4-flash',
        embedding: '',
        tiers: { large: 'gpt-5.1', medium: 'deepseek-v4-flash' },
      }),
    )
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('默认模型已保存'))
  })
})
