// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * LlmSettingsPage 覆盖缺口补充测试（与既有 LlmSettingsPage.test 互补，不重复）：
 * - 配置加载失败：错误横幅 + 重试（refetch）；console.error 上报
 * - 错误路径族：默认模型保存/添加模型/删除模型/模型设置保存/更新 Key/
 *   并发保存/添加提供商/删除提供商 全部失败 → toast.error 带 getApiMsg 描述
 *   （含 reject(undefined) 萰底描述）
 * - 成功路径补全：删除模型/删除提供商/并发设置（展开+输入+保存）/添加提供商
 *   （含表单重置）
 * - 自定义提供商表单：懒加载 litellm 类型目录（成功渲染 / 失败回退声明常用类型 /
 *   已加载幂等不重复拉）
 * - 拉取添加部分失败 → toast.warning 带失败清单
 * - 同组多提供者按 has_key 排序（未配置排前）
 * - 默认模型五个下拉的 onChange（chat/tiers×3/embedding）
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

/** 同一预置组放两个 provider 且 has_key 相异（驱动排序比较器真实执行） */
const configFixture = {
  models: {
    'm-1': { provider: 'openai', model_name: 'm-1', display_name: '模型一' },
    'm-2': { provider: 'anthropic', model_name: 'm-2', display_name: '模型二' },
  },
  providers: {
    openai: {
      type: 'openai',
      api_base: 'https://api.openai.com/v1',
      keys: [{ id: 'openai_main', api_key: 'sk-masked', max_concurrent: 6, rpm: 10 }],
      has_key: true,
      env_var: 'OPENAI_API_KEY',
    },
    anthropic: {
      type: 'openai',
      keys: [{ id: 'anthropic_main', api_key: '${ANTHROPIC_KEY}', max_concurrent: 6, rpm: 10 }],
      has_key: false,
      env_var: 'ANTHROPIC_KEY',
    },
  },
  defaults: { chat: '', embedding: '', tiers: {} },
}

const presetsFixture = {
  provider_groups: [
    {
      label: '国际',
      providers: [
        ['openai', 'OpenAI'],
        ['anthropic', 'Claude'],
      ],
    },
  ],
  common_provider_types: ['openai', 'anthropic'],
  thinking_strength: { levels: ['high', 'medium', 'low'], allowed_keys: ['thinking'] },
}

async function renderLoaded() {
  mock.getLLMConfig.mockResolvedValue(configFixture)
  mock.getLLMPresets.mockResolvedValue(presetsFixture)
  mock.getProviderTypes.mockResolvedValue({ types: ['openai', 'anthropic', 'zai', 'gemini'] })
  // 变更端点默认成功响应：组件把返回值写回 config，缺省 undefined 会把
  // providers/models 置空导致后续查询全空（真返回形状对齐 services/api/config）
  mock.getRemoteModels.mockResolvedValue({
    provider: 'openai',
    models: [{ id: 'gpt-a', owned_by: 'openai' }, { id: 'gpt-b', owned_by: 'openai' }],
  })
  // 返回形状对齐组件消费契约：provider/model 变更端点直接返回映射本身
  // （仅 addModel 包 {models, added_ids} 信封）；双层包裹会把 config 写坏
  mock.updateProviderConfig.mockResolvedValue(configFixture.providers)
  mock.deleteProvider.mockResolvedValue(configFixture.providers)
  mock.addProvider.mockResolvedValue(configFixture.providers)
  mock.saveDefaults.mockResolvedValue(configFixture.defaults)
  mock.deleteModel.mockResolvedValue(configFixture.models)
  mock.updateModel.mockResolvedValue(configFixture.models)
  mock.addModel.mockResolvedValue({ models: configFixture.models, added_ids: ['x'] })
  renderWithProviders(<LlmSettingsPage />)
  await waitFor(() => expect(screen.getByText('国际（2）')).toBeInTheDocument())
}

async function gotoModelsTab() {
  fireEvent.click(screen.getByRole('tab', { name: '模型' }))
  await waitFor(() => expect(screen.getByText('已注册模型 (2)')).toBeInTheDocument())
}

/** 取 provider 卡片根节点（卡片含显示名） */
function cardOf(name: string): HTMLElement {
  return screen.getByText(name).closest('div[class*="bg-card"]') as HTMLElement
}

beforeEach(() => {
  // resetAllMocks 而非 clearAllMocks：mockRejectedValueOnce 排队的 once 实现
  // 不被 clear 清除，会泄漏进后续用例（前一条未触发的失败实现被后一条吃掉）
  vi.resetAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('LlmSettingsPage 缺口：加载失败与重试', () => {
  it('配置加载失败：横幅提示 + console 上报 + 重试触发 refetch', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    mock.getLLMConfig.mockRejectedValue(new Error('net down'))
    mock.getLLMPresets.mockResolvedValue(presetsFixture)
    renderWithProviders(<LlmSettingsPage />)

    expect(await screen.findByText('无法连接服务器，请检查网络后重试')).toBeInTheDocument()
    expect(errSpy).toHaveBeenCalled()
    // 空配置兜底：提供商 Tab 显示空态
    expect(screen.getByText('暂无提供商')).toBeInTheDocument()

    // 重试 → refetch（仍然失败不崩）
    fireEvent.click(screen.getByRole('button', { name: /重试/ }))
    await waitFor(() => expect(mock.getLLMConfig).toHaveBeenCalledTimes(2))
  })

  it('同组多提供者按 has_key 排序：未配置排前', async () => {
    await renderLoaded()
    const section = screen.getByText('国际（2）').closest('section') as HTMLElement
    const ids = within(section).getAllByText(/^(openai|anthropic)$/).map((e) => e.textContent)
    expect(ids.indexOf('anthropic')).toBeLessThan(ids.indexOf('openai'))
  })
})

describe('LlmSettingsPage 缺口：提供商卡片操作', () => {
  it('并发设置：展开 → 修改并发与 RPM → 保存成功；失败 toast', async () => {
    await renderLoaded()
    fireEvent.click(within(cardOf('OpenAI')).getByRole('button', { name: /并发设置/ }))

    const inputs = within(cardOf('OpenAI')).getAllByRole('spinbutton')
    fireEvent.change(inputs[0], { target: { value: '9' } })
    fireEvent.change(inputs[1], { target: { value: '30' } })
    fireEvent.click(within(cardOf('OpenAI')).getByRole('button', { name: '保存并发设置' }))
    await waitFor(() =>
      expect(mock.updateProviderConfig).toHaveBeenCalledWith('openai', {
        keys: [{ id: 'openai_main', max_concurrent: 9, rpm: 30 }],
      }),
    )
    expect(toast.success).toHaveBeenCalledWith('已保存 openai 的并发设置')

    // 失败 → toast.error 带 API 描述
    mock.updateProviderConfig.mockRejectedValueOnce({ message: 'busy' })
    fireEvent.click(within(cardOf('OpenAI')).getByRole('button', { name: '保存并发设置' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存并发设置失败', { description: 'busy' }),
    )
  })

  it('更新 Key 失败 → toast.error；已配置掩码回显', async () => {
    await renderLoaded()
    mock.updateProviderConfig.mockRejectedValueOnce({ message: 'key rejected' })
    const card = cardOf('OpenAI')
    fireEvent.change(screen.getByPlaceholderText('输入新的 API Key'), {
      target: { value: 'sk-bad' },
    })
    fireEvent.click(within(card).getByRole('button', { name: '更新 Key' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存密钥失败', { description: 'key rejected' }),
    )
  })

  it('删除提供商：成功静默更新配置；失败 toast', async () => {
    await renderLoaded()
    fireEvent.click(within(cardOf('Claude')).getByRole('button', { name: '删除' }))
    await waitFor(() => expect(mock.deleteProvider).toHaveBeenCalledWith('anthropic'))
    expect(toast.error).not.toHaveBeenCalled()
    // 返回剩余 providers → openai 卡仍在
    expect(within(cardOf('OpenAI')).getByText('已配置')).toBeInTheDocument()

    mock.deleteProvider.mockRejectedValueOnce({ message: 'has children' })
    fireEvent.click(within(cardOf('OpenAI')).getByRole('button', { name: '删除' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('删除提供商失败', { description: 'has children' }),
    )
  })

  it('添加自定义提供商：懒加载类型目录、表单提交成功后重置；失败 toast', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByText(/添加自定义提供商/))

    // 展开 details 触发懒加载目录；目录加载完成后组件渲染的是「终态」：组头
    // 计数 = litellm 已知类型全量（4），组内条目 = 全量剔除常用（zai/gemini）。
    // presets 就绪态（组头（2）、条目为空）与懒加载态（组头（4）、含独占项）
    // 是两个互斥形态——「（2）+zai」的同笼等待要求组件从不渲染的中间态，
    // 只能确定性红（批九 FE 车道两次实测），故断言终态四类型齐全。
    await waitFor(
      () => {
        expect(screen.getByText('litellm 全部（4）')).toBeInTheDocument()
        expect(screen.getByText('zai')).toBeInTheDocument()
        expect(screen.getByText('gemini')).toBeInTheDocument()
      },
      { timeout: 10_000 },
    )

    fireEvent.change(screen.getByLabelText('提供商 ID'), { target: { value: 'myproxy2' } })
    fireEvent.change(screen.getByLabelText('API Base'), { target: { value: 'https://p2/v1' } })
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'sk-p2' } })
    mock.addProvider.mockResolvedValueOnce({ providers: configFixture.providers })
    fireEvent.click(screen.getByRole('button', { name: '添加提供商' }))
    await waitFor(() =>
      expect(mock.addProvider).toHaveBeenCalledWith('myproxy2', {
        type: 'openai',
        api_base: 'https://p2/v1',
        api_key: 'sk-p2',
      }),
    )
    // 表单重置
    expect(screen.getByLabelText('提供商 ID')).toHaveValue('')

    // 失败 → toast.error
    fireEvent.change(screen.getByLabelText('提供商 ID'), { target: { value: 'p3' } })
    mock.addProvider.mockRejectedValueOnce({ message: 'dup' })
    fireEvent.click(screen.getByRole('button', { name: '添加提供商' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('添加提供商失败', { description: 'dup' }),
    )
  }, 30_000) // 整页多次渲染+懒目录加载，FE 全量车道与其他重负载共跑时 10s 默认会被饿死（批九三次实测）

  it('类型目录拉取失败回退声明常用类型；已加载后再展开不重复拉取', async () => {
    await renderLoaded()
    mock.getProviderTypes.mockRejectedValueOnce(new Error('catalog down'))
    fireEvent.click(screen.getByText(/添加自定义提供商/))
    // 回退：目录失败但声明常用类型仍在「常用」组
    await waitFor(() => expect(mock.getProviderTypes).toHaveBeenCalled())
    expect(screen.getAllByText('anthropic').length).toBeGreaterThan(0)

    // 关闭再展开：已加载（或已回退置位）→ 不再重复拉取
    const calls = mock.getProviderTypes.mock.calls.length
    fireEvent.click(screen.getByText(/添加自定义提供商/))
    fireEvent.click(screen.getByText(/添加自定义提供商/))
    expect(mock.getProviderTypes.mock.calls.length).toBe(calls)
  })
})

describe('LlmSettingsPage 缺口：模型 Tab 操作', () => {
  it('五个默认模型下拉可改选；保存成功更新配置，失败 toast（含 undefined 错误的兜底描述）', async () => {
    await renderLoaded()
    await gotoModelsTab()
    const section = screen.getByText('默认模型').closest('section') as HTMLElement

    // 每个下拉（chat/large/medium/small/embedding）点击同一个模型选项 → onChange 全走
    const labels = ['默认对话模型', 'large 档位', 'medium 档位', 'small 档位', '默认向量模型']
    expect(within(section).getAllByText(/档位|默认对话模型|默认向量模型/).length).toBe(5)
    for (const label of labels) {
      const labelEl = within(section).getByText(label)
      // FieldRow：label 与内容 div 是兄弟，经共同父行（sm:flex-row）定位
      const row = labelEl.closest('div[class*="sm:flex-row"]') as HTMLElement
      fireEvent.click(within(row).getByRole('button', { name: 'm-1' }))
    }

    mock.saveDefaults.mockResolvedValueOnce({
      chat: 'm-1', embedding: 'm-1', tiers: { large: 'm-1', medium: 'm-1', small: 'm-1' },
    })
    fireEvent.click(within(section).getByRole('button', { name: '保存默认模型' }))
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('默认模型已保存'))
    expect(mock.saveDefaults).toHaveBeenCalledWith(
      expect.objectContaining({ chat: 'm-1', embedding: 'm-1' }),
    )

    // 失败：reject(undefined) → getApiMsg 落回调用方兜底文案
    mock.saveDefaults.mockRejectedValueOnce(undefined)
    fireEvent.click(within(section).getByRole('button', { name: '保存默认模型' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存默认模型失败', {
        description: '保存默认模型失败',
      }),
    )
  })

  it('删除模型：成功更新列表；失败 toast', async () => {
    await renderLoaded()
    await gotoModelsTab()
    const modelSection = (screen.getByText('已注册模型 (2)').closest('section')) as HTMLElement

    mock.deleteModel.mockResolvedValueOnce({ 'm-1': configFixture.models['m-1'] })
    fireEvent.click(within(modelSection).getAllByRole('button', { name: '删除' })[0])
    await waitFor(() => expect(mock.deleteModel).toHaveBeenCalled())
    expect(toast.error).not.toHaveBeenCalled()

    mock.deleteModel.mockRejectedValueOnce({ message: 'in use' })
    fireEvent.click(within(modelSection).getAllByRole('button', { name: '删除' })[0])
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('删除模型失败', { description: 'in use' }),
    )
  })

  it('模型参数保存失败 → toast.error（成功路径归既有测试）', async () => {
    await renderLoaded()
    await gotoModelsTab()
    const modelSection = (screen.getByText('已注册模型 (2)').closest('section')) as HTMLElement
    fireEvent.click(within(modelSection).getAllByRole('button', { name: '参数' })[0])

    mock.updateModel.mockRejectedValueOnce({ message: 'validation down' })
    fireEvent.click(within(modelSection).getByRole('button', { name: '保存设置' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存模型设置失败', {
        description: 'validation down',
      }),
    )
  })

  it('手动添加模型失败 → toast.error（reject(undefined) 走兜底描述）', async () => {
    await renderLoaded()
    await gotoModelsTab()
    fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: 'm-new' } })
    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: '新模型' } })
    // 提供商下拉：选 anthropic（label 与内容 div 是兄弟，经父行定位）
    const providerLabel = screen.getByText('提供商')
    const providerRow = providerLabel.closest('div[class*="sm:flex-row"]') as HTMLElement
    fireEvent.click(within(providerRow).getByRole('button', { name: 'anthropic' }))

    mock.addModel.mockRejectedValueOnce(undefined)
    fireEvent.click(screen.getByRole('button', { name: '添加模型' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('添加模型失败', { description: '添加模型失败' }),
    )
  })

  it('拉取添加部分失败 → toast.warning 带失败清单；全部成功 → success', async () => {
    await renderLoaded()
    const card = cardOf('OpenAI')
    // 第一轮：两个模型一成一败 → warning
    mock.addModel.mockResolvedValueOnce({
      models: configFixture.models, added_ids: ['gpt-a'],
    })
    mock.addModel.mockRejectedValueOnce({ message: 'dup' })
    fireEvent.click(within(card).getByRole('button', { name: /拉取模型/ }))

    const customInput = await screen.findByPlaceholderText(/自定义模型名/)
    fireEvent.change(customInput, { target: { value: 'gpt-a' } })
    fireEvent.keyDown(customInput, { key: 'Enter' })
    fireEvent.change(customInput, { target: { value: 'gpt-b' } })
    fireEvent.keyDown(customInput, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: /添加所选/ }))
    await waitFor(() => expect(mock.addModel).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith('已添加 1 个模型', {
        description: expect.stringContaining('gpt-b'),
      }),
    )

    // 第二轮：全部成功 → success toast
    fireEvent.click(within(cardOf('OpenAI')).getByRole('button', { name: /拉取模型/ }))
    const input2 = await screen.findByPlaceholderText(/自定义模型名/)
    fireEvent.change(input2, { target: { value: 'gpt-c' } })
    fireEvent.keyDown(input2, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: /添加所选/ }))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        '已添加 1 个模型到 openai',
      ),
    )
  })
})
