// @feature FP-T12 前端适配（OBS-R258-1 交互反馈即时性收口） | @ci: frontend-test
/**
 * LlmSettingsPage 保存动作 pending 反馈测试（#1）：
 * - 全部保存按钮 await 窗口 disabled + spinner/文案（保存中.../添加中.../删除中...），
 *   防连点
 * - 失败路径：toast 报错（既有断言面）+ 按钮复位可重试（不残留 disabled）
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { toast } from '@/components/ui/sonner'
import { renderWithProviders } from '@/test/renderWithProviders'
import { LlmSettingsPage } from '../LlmSettingsPage'

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
      const { onValueChange } = useContext(SelectCtx)
      return (
        <button type="button" onClick={() => onValueChange?.(value)}>
          {children}
        </button>
      )
    },
  }
})

const configFixture = {
  models: {
    'm-1': { provider: 'openai', model_name: 'm-1', display_name: '模型一' },
  },
  providers: {
    openai: {
      type: 'openai',
      keys: [{ id: 'openai_main', api_key: 'sk-masked', max_concurrent: 6, rpm: 10 }],
      has_key: true,
      env_var: 'OPENAI_API_KEY',
    },
  },
  defaults: { chat: '', embedding: '', tiers: {} },
}

const presetsFixture = {
  provider_groups: [
    { label: '国际', providers: [['openai', 'OpenAI']] },
  ],
  common_provider_types: ['openai'],
  thinking_strength: { levels: ['high', 'medium', 'low'], allowed_keys: ['thinking'] },
}

async function renderLoaded() {
  mock.getLLMConfig.mockResolvedValue(configFixture)
  mock.getLLMPresets.mockResolvedValue(presetsFixture)
  // 展开自定义提供商表单触发懒加载目录（onToggle 链路），未 mock 会 undefined.then 崩
  mock.getProviderTypes.mockResolvedValue({ types: ['openai'] })
  mock.updateProviderConfig.mockResolvedValue(configFixture.providers)
  mock.saveDefaults.mockResolvedValue(configFixture.defaults)
  mock.deleteModel.mockResolvedValue(configFixture.models)
  mock.updateModel.mockResolvedValue(configFixture.models)
  mock.deleteProvider.mockResolvedValue(configFixture.providers)
  renderWithProviders(<LlmSettingsPage />)
  await waitFor(() => expect(screen.getByText('国际（1）')).toBeInTheDocument())
}

function cardOf(name: string): HTMLElement {
  return screen.getByText(name).closest('div[class*="bg-card"]') as HTMLElement
}

async function gotoModelsTab() {
  fireEvent.click(screen.getByRole('tab', { name: '模型' }))
  await waitFor(() => expect(screen.getByText('已注册模型 (1)')).toBeInTheDocument())
}

beforeEach(() => {
  vi.resetAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('LlmSettingsPage 保存动作 pending（await 窗口可见进行中态）', () => {
  it('保存默认模型：await 窗口 disabled +「保存中...」，落定复位 + 成功 toast', async () => {
    await renderLoaded()
    await gotoModelsTab()
    let resolveSave: () => void = () => {}
    mock.saveDefaults.mockReturnValue(
      new Promise((resolve) => {
        resolveSave = resolve
      }),
    )

    const btn = screen.getByRole('button', { name: '保存默认模型' })
    fireEvent.click(btn)

    // await 窗口：禁用 + 文案切换，再点不触发第二次
    expect(screen.getByRole('button', { name: '保存中...' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '保存中...' }))
    expect(mock.saveDefaults).toHaveBeenCalledTimes(1)

    resolveSave(configFixture.defaults)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '保存默认模型' })).toBeEnabled(),
    )
    expect(toast.success).toHaveBeenCalledWith('默认模型已保存')
  })

  it('保存默认模型失败：toast 报错 + 按钮复位可重试（不残留 disabled）', async () => {
    await renderLoaded()
    await gotoModelsTab()
    mock.saveDefaults.mockRejectedValueOnce({ message: '后端不可写' })

    fireEvent.click(screen.getByRole('button', { name: '保存默认模型' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存默认模型失败', { description: '后端不可写' }),
    )
    expect(screen.getByRole('button', { name: '保存默认模型' })).toBeEnabled()
  })

  it('更新 Key：await 窗口按钮「保存中...」disabled；失败 toast + 复位', async () => {
    await renderLoaded()
    let resolveKey: () => void = () => {}
    mock.updateProviderConfig.mockReturnValue(
      new Promise((resolve) => {
        resolveKey = resolve
      }),
    )

    fireEvent.change(screen.getByPlaceholderText('输入新的 API Key'), {
      target: { value: 'sk-new' },
    })
    fireEvent.click(within(cardOf('OpenAI')).getByRole('button', { name: '更新 Key' }))
    // await 窗口：保存中 + 禁用（连点被挡）
    expect(within(cardOf('OpenAI')).getByRole('button', { name: /保存中/ })).toBeDisabled()
    expect(mock.updateProviderConfig).toHaveBeenCalledTimes(1)

    resolveKey(configFixture.providers)
    // 成功落定：spinner 退场（按钮随输入清空回合法 disabled 态，非 pending 残留）
    await waitFor(() =>
      expect(within(cardOf('OpenAI')).queryByRole('button', { name: /保存中/ })).not.toBeInTheDocument(),
    )

    // 失败路径：toast + spinner 退场（诚实复位可重试）
    mock.updateProviderConfig.mockRejectedValueOnce({ message: 'key rejected' })
    fireEvent.change(screen.getByPlaceholderText('输入新的 API Key'), {
      target: { value: 'sk-bad' },
    })
    fireEvent.click(within(cardOf('OpenAI')).getByRole('button', { name: '更新 Key' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存密钥失败', { description: 'key rejected' }),
    )
    await waitFor(() =>
      expect(within(cardOf('OpenAI')).queryByRole('button', { name: /保存中/ })).not.toBeInTheDocument(),
    )
  })

  it('删除模型：await 窗口「删除中...」disabled；落定复位', async () => {
    await renderLoaded()
    await gotoModelsTab()
    let resolveDelete: () => void = () => {}
    mock.deleteModel.mockReturnValue(
      new Promise((resolve) => {
        resolveDelete = resolve
      }),
    )

    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(screen.getByRole('button', { name: '删除中...' })).toBeDisabled()

    resolveDelete(configFixture.models)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '删除中...' })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: '删除' })).toBeEnabled()
  })

  it('添加提供商：await 窗口「添加中...」disabled；失败 toast + 复位', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByText('添加自定义提供商（类型来自 litellm，升级 litellm 后自动出现新提供者）'))
    fireEvent.change(screen.getByPlaceholderText('如: myproxy'), { target: { value: 'myproxy' } })

    let resolveAdd: () => void = () => {}
    mock.addProvider.mockReturnValue(
      new Promise((resolve) => {
        resolveAdd = resolve
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: '添加提供商' }))
    expect(screen.getByRole('button', { name: '添加中...' })).toBeDisabled()

    mock.addProvider.mockRejectedValueOnce({ message: 'dup' })
    resolveAdd(configFixture.providers)
    // 成功落定：spinner 退场（表单已重置，按钮随空 ID 回合法 disabled 态）
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '添加中...' })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: '添加提供商' })).toBeDisabled()

    // 失败路径：toast + spinner 退场可重试
    fireEvent.change(screen.getByPlaceholderText('如: myproxy'), { target: { value: 'myproxy2' } })
    fireEvent.click(screen.getByRole('button', { name: '添加提供商' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('添加提供商失败', { description: 'dup' }),
    )
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '添加中...' })).not.toBeInTheDocument(),
    )
  })
})
