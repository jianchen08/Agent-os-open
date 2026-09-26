// @feature FP-0.2.四 前端Schema 引导向导 widget | @ci frontend-test
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LlmSetupWizard } from '../onboarding/LlmSetupWizard'

const updateProviderConfig = vi.fn()
const addModel = vi.fn()
const saveDefaults = vi.fn()
const getRemoteModels = vi.fn()

vi.mock('@/services/api/config', () => ({
  getLLMConfig: vi.fn(async () => ({
    providers: { deepseek: { keys: [] }, zai: { keys: [{ id: 'zai_main', api_key: 'sk-zai-real' }] } },
    models: {},
    defaults: { chat: '', embedding: '', tiers: {} },
  })),
  getLLMPresets: vi.fn(async () => ({
    provider_groups: [{ label: '国内', providers: [['deepseek', 'DeepSeek'], ['zai', '智谱']] }],
    common_provider_types: [],
    thinking_strength: { levels: [], allowed_keys: [] },
  })),
  getRemoteModels: (providerId: string) => getRemoteModels(providerId),
  updateProviderConfig: (...args: unknown[]) => updateProviderConfig(...args),
  addModel: (...args: unknown[]) => addModel(...args),
  saveDefaults: (...args: unknown[]) => saveDefaults(...args),
}))

vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

beforeEach(() => {
  vi.clearAllMocks()
  updateProviderConfig.mockResolvedValue({
    deepseek: { keys: [] },
    zai: { keys: [{ id: 'zai_main', api_key: 'sk-zai-real' }] },
  })
  addModel.mockResolvedValue({ models: {}, added_ids: [] })
  saveDefaults.mockResolvedValue({ chat: '', embedding: '', tiers: {} })
  getRemoteModels.mockResolvedValue({
    provider: 'deepseek',
    models: [{ id: 'deepseek-chat', owned_by: 'deepseek' }],
  })
})

describe('LlmSetupWizard', () => {
  it('展示预置提供商并标注已配置状态（zai 有 Key、deepseek 未配置）', async () => {
    render(<LlmSetupWizard onConfigured={() => {}} onOpenAdvanced={() => {}} />)
    expect(await screen.findByText('DeepSeek')).toBeInTheDocument()
    expect(screen.getByText('智谱')).toBeInTheDocument()
    expect(screen.getByText('已配置 Key')).toBeInTheDocument()
  })

  it('完整流：选提供商 → 存 Key 拉模型 → 设默认模型（同设置页服务层语义）', async () => {
    const onConfigured = vi.fn()
    render(<LlmSetupWizard onConfigured={onConfigured} onOpenAdvanced={() => {}} />)
    fireEvent.click(await screen.findByText('DeepSeek'))
    fireEvent.change(screen.getByTestId('wizard-api-key'), { target: { value: 'sk-test-1' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并获取模型' }))
    // Key 写入走 updateProviderConfig（keys[0].id 兜底 `${providerId}_main`）
    await waitFor(() =>
      expect(updateProviderConfig).toHaveBeenCalledWith('deepseek', {
        keys: [{ id: 'deepseek_main', api_key: 'sk-test-1' }],
      }),
    )
    expect(getRemoteModels).toHaveBeenCalledWith('deepseek')
    fireEvent.click(await screen.findByRole('radio'))
    fireEvent.click(screen.getByRole('button', { name: '设为默认模型' }))
    await waitFor(() => expect(saveDefaults).toHaveBeenCalledWith({ chat: 'deepseek-chat' }))
    expect(addModel).toHaveBeenCalledWith('deepseek-chat', {
      provider: 'deepseek',
      model_name: 'deepseek-chat',
      display_name: 'deepseek-chat',
    })
    expect(onConfigured).toHaveBeenCalled()
    expect(screen.getByTestId('wizard-done')).toBeInTheDocument()
  })

  it('已有模型的提供商不重复 addModel，只设默认', async () => {
    const { getLLMConfig } = await import('@/services/api/config')
    ;(getLLMConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      providers: { deepseek: { keys: [] } },
      models: { 'deepseek-chat': { provider: 'deepseek', model_name: 'deepseek-chat' } },
      defaults: { chat: '', embedding: '', tiers: {} },
    })
    render(<LlmSetupWizard onConfigured={() => {}} onOpenAdvanced={() => {}} />)
    fireEvent.click(await screen.findByText('DeepSeek'))
    fireEvent.change(screen.getByTestId('wizard-api-key'), { target: { value: 'sk-2' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并获取模型' }))
    fireEvent.click(await screen.findByRole('radio'))
    fireEvent.click(screen.getByRole('button', { name: '设为默认模型' }))
    await waitFor(() => expect(saveDefaults).toHaveBeenCalled())
    expect(addModel).not.toHaveBeenCalled()
  })
})
