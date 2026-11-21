// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * LlmSettingsPage 覆盖缺口补充测试（单界面重构版，与 LlmSettingsPage.test 互补）：
 * - 配置加载失败（OBS-R258-1 四态）：整页 ErrorState + 重试（refetch），
 *   不渲染零数据伪装物；非 Error 拒绝回退固定文案；预置声明副面降级不阻断主面
 * - 错误路径族：默认模型保存/删除模型/模型设置保存/更新 Key/并发保存/
 *   添加提供商/删除提供商 全部失败 → toast.error 带 getApiMsg 描述
 * - 成功路径补全：删除模型/删除提供商/并发设置（展开+输入+保存）
 * - 自定义提供商表单：选中「自定义」懒加载 litellm 类型目录（成功渲染 /
 *   失败回退声明常用类型 / 已加载幂等不重复拉）
 * - 拉取添加部分失败 → toast.warning 带失败清单；全部失败 → toast.error
 * - 无提供商时落「自定义」：提示语 + 表单直接可用
 * - 已注册模型行的「参数」面板：完整字段保存 → updateModel 合并保留原字段
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
    'm-2': {
      provider: 'anthropic',
      model_name: 'm-2',
      display_name: '模型二',
      default_params: { temperature: 0.5, max_tokens: 100000 },
    },
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
      api_base: 'https://api.anthropic.com/v1',
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
  mock.getRemoteModels.mockResolvedValue({
    provider: 'openai',
    models: [
      { id: 'gpt-a', owned_by: 'openai' },
      { id: 'gpt-b', owned_by: 'openai' },
    ],
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
  await waitFor(() => expect(screen.getByText('已注册模型 (2)')).toBeInTheDocument())
}

/** 模型区容器（模型选择段：拉取/手填） */
function modelSection(): HTMLElement {
  return screen.getByRole('heading', { name: '模型', level: 3 }).closest('section') as HTMLElement
}

/** 已注册模型段容器（逐行条目） */
function modelRegistrySection(): HTMLElement {
  return screen.getByRole('heading', { name: /已注册模型/ }).closest('section') as HTMLElement
}

beforeEach(() => {
  // resetAllMocks 而非 clearAllMocks：mockRejectedValueOnce 排队的 once 实现
  // 不被 clear 清除，会泄漏进后续用例（前一条未触发的失败实现被后一条吃掉）
  vi.resetAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('LlmSettingsPage 缺口：加载失败与降级', () => {
  it('配置加载失败（Error 实例）：整页 ErrorState + 重试，不渲染零数据伪装物（OBS-R258-1 铁律 1/2）', async () => {
    mock.getLLMConfig.mockRejectedValue(new Error('net down'))
    mock.getLLMPresets.mockResolvedValue(presetsFixture)
    renderWithProviders(<LlmSettingsPage />)

    expect(await screen.findByText('net down')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重试/ })).toBeInTheDocument()
    // 失败态不渲染任何"零数据"伪装物
    expect(screen.queryByText(/暂无模型/)).not.toBeInTheDocument()
    expect(screen.queryByText('已注册模型 (0)')).not.toBeInTheDocument()

    // 重试 → refetch（仍然失败不崩）
    fireEvent.click(screen.getByRole('button', { name: /重试/ }))
    await waitFor(() => expect(mock.getLLMConfig).toHaveBeenCalledTimes(2))
    expect(screen.getByText('net down')).toBeInTheDocument()
  })

  it('配置加载失败（非 Error 拒绝）：回退固定文案「无法连接服务器，请检查网络后重试」', async () => {
    mock.getLLMConfig.mockRejectedValue({ message: 'boom' })
    mock.getLLMPresets.mockResolvedValue(presetsFixture)
    renderWithProviders(<LlmSettingsPage />)

    expect(await screen.findByText('无法连接服务器，请检查网络后重试')).toBeInTheDocument()
  })

  it('预置声明拉取失败：副面降级不阻断主配置面（OBS-R258-1 铁律 4）', async () => {
    mock.getLLMConfig.mockResolvedValue(configFixture)
    mock.getLLMPresets.mockRejectedValue(new Error('presets down'))
    renderWithProviders(<LlmSettingsPage />)

    // 声明缺席 → 全部提供商落「其他已添加」组，模型列表照常渲染
    expect(await screen.findByText('其他已添加')).toBeInTheDocument()
    expect(screen.getByText('已注册模型 (2)')).toBeInTheDocument()
    // 无提供商时也能选到「自定义」
    expect(screen.getByRole('button', { name: '自定义（新增提供商）' })).toBeInTheDocument()
  })

  it('无提供商配置：选中项落「自定义」，提示先新增提供商', async () => {
    mock.getLLMConfig.mockResolvedValue({
      models: {},
      providers: {},
      defaults: { chat: '', embedding: '', tiers: {} },
    })
    mock.getLLMPresets.mockResolvedValue(presetsFixture)
    mock.getProviderTypes.mockResolvedValue({ types: ['openai'] })
    renderWithProviders(<LlmSettingsPage />)

    expect(await screen.findByText('已注册模型 (0)')).toBeInTheDocument()
    expect(await screen.findByLabelText('Base URL')).toBeInTheDocument()
    expect(screen.getByText(/暂无模型——在上方选择提供商后/)).toBeInTheDocument()
  })
})

describe('LlmSettingsPage 缺口：提供商操作', () => {
  it('并发设置：展开 → 修改并发与 RPM → 保存成功；失败 toast', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: /并发设置/ }))

    const spins = screen.getAllByRole('spinbutton')
    fireEvent.change(spins[0], { target: { value: '9' } })
    fireEvent.change(spins[1], { target: { value: '30' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并发设置' }))
    await waitFor(() =>
      expect(mock.updateProviderConfig).toHaveBeenCalledWith('openai', {
        keys: [{ id: 'openai_main', max_concurrent: 9, rpm: 30 }],
      }),
    )
    expect(toast.success).toHaveBeenCalledWith('已保存 openai 的并发设置')

    mock.updateProviderConfig.mockRejectedValueOnce({ message: 'busy' })
    fireEvent.click(screen.getByRole('button', { name: '保存并发设置' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存并发设置失败', { description: 'busy' }),
    )
  })

  it('更新 Key：已配置时点「更换 Key」展开输入 → 保存明文；失败 toast', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: '更换 Key' }))
    const input = await screen.findByLabelText('填写 openai 的 API Key')
    fireEvent.change(input, { target: { value: 'sk-rotated' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 Key' }))
    await waitFor(() =>
      expect(mock.updateProviderConfig).toHaveBeenCalledWith('openai', {
        keys: [{ id: 'openai_main', api_key: 'sk-rotated' }],
      }),
    )
    expect(toast.success).toHaveBeenCalledWith('已保存 openai 的 Key（写入 .env，立即生效）')

    // 失败路径：再次点「更换 Key」展开后提交 → toast + spinner 退场（诚实复位可重试）
    mock.updateProviderConfig.mockRejectedValueOnce({ message: 'key rejected' })
    fireEvent.click(await screen.findByRole('button', { name: '更换 Key' }))
    fireEvent.change(await screen.findByLabelText('填写 openai 的 API Key'), {
      target: { value: 'sk-bad' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存 Key' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存密钥失败', { description: 'key rejected' }),
    )
  })

  it('未配置提供商：Key 输入直填保存（无需先点「更换 Key」）', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'Claude' }))
    const input = await screen.findByLabelText('填写 anthropic 的 API Key')
    fireEvent.change(input, { target: { value: 'sk-anthropic' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 Key' }))
    await waitFor(() =>
      expect(mock.updateProviderConfig).toHaveBeenCalledWith('anthropic', {
        keys: [{ id: 'anthropic_main', api_key: 'sk-anthropic' }],
      }),
    )
  })

  it('删除提供商：成功静默更新配置；失败 toast', async () => {
    await renderLoaded()
    // 删除按钮在提供商段与模型条目都有，按段限定作用域
    const providerSection = screen
      .getByRole('heading', { name: '提供商', level: 3 })
      .closest('section') as HTMLElement
    fireEvent.click(within(providerSection).getByRole('button', { name: '删除' }))
    await waitFor(() => expect(mock.deleteProvider).toHaveBeenCalledWith('openai'))
    expect(toast.error).not.toHaveBeenCalled()

    mock.deleteProvider.mockRejectedValueOnce({ message: 'has children' })
    fireEvent.click(within(providerSection).getByRole('button', { name: '删除' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('删除提供商失败', { description: 'has children' }),
    )
  })
})

describe('LlmSettingsPage 缺口：自定义提供商表单', () => {
  it('选中「自定义」懒加载类型目录；提交成功后重置；失败 toast', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: '自定义（新增提供商）' }))

    // 目录加载完成后渲染全量类型（常用 2 + litellm 独占 zai/gemini）
    await waitFor(
      () => {
        expect(screen.getByText('litellm 全部（4）')).toBeInTheDocument()
        expect(screen.getByText('zai')).toBeInTheDocument()
        expect(screen.getByText('gemini')).toBeInTheDocument()
      },
      { timeout: 10_000 },
    )

    fireEvent.change(screen.getByLabelText('提供商 ID'), { target: { value: 'myproxy2' } })
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://p2/v1' } })
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'sk-p2' } })
    fireEvent.click(screen.getByRole('button', { name: '添加提供商' }))
    await waitFor(() =>
      expect(mock.addProvider).toHaveBeenCalledWith('myproxy2', {
        type: 'openai',
        api_base: 'https://p2/v1',
        api_key: 'sk-p2',
      }),
    )

    // 失败 → toast.error
    fireEvent.click(screen.getByRole('button', { name: '自定义（新增提供商）' }))
    fireEvent.change(screen.getByLabelText('提供商 ID'), { target: { value: 'p3' } })
    mock.addProvider.mockRejectedValueOnce({ message: 'dup' })
    fireEvent.click(screen.getByRole('button', { name: '添加提供商' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('添加提供商失败', { description: 'dup' }),
    )
  }, 30_000)

  it('类型目录拉取失败：回退声明常用类型（不阻塞表单）', async () => {
    await renderLoaded()
    mock.getProviderTypes.mockRejectedValueOnce(new Error('catalog down'))
    fireEvent.click(screen.getByRole('button', { name: '自定义（新增提供商）' }))
    // 回退：组头计数 = 声明常用类型数（2），无 litellm 独占项
    await waitFor(() => expect(screen.getByText('litellm 全部（2）')).toBeInTheDocument())
    expect(screen.queryByText('zai')).not.toBeInTheDocument()
    expect(screen.getByLabelText('提供商 ID')).toBeInTheDocument()
  })

  it('类型目录成功加载后：再进「自定义」不重复拉取', async () => {
    await renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: '自定义（新增提供商）' }))
    await waitFor(() => expect(screen.getByText('zai')).toBeInTheDocument())
    const callsAfterFirst = mock.getProviderTypes.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: 'OpenAI' }))
    fireEvent.click(screen.getByRole('button', { name: '自定义（新增提供商）' }))
    await waitFor(() => expect(screen.getByLabelText('提供商 ID')).toBeInTheDocument())
    expect(mock.getProviderTypes.mock.calls.length).toBe(callsAfterFirst)
  })
})

describe('LlmSettingsPage 缺口：默认模型指派与添加参数草稿', () => {
  it('默认对话/向量模型下拉选择 → 保存默认模型带上草稿值', async () => {
    await renderLoaded()
    // FieldRow 根 = label 的父 div；下拉项按钮 = mocked SelectItem
    const chatRow = screen.getByText('默认对话模型').closest('div') as HTMLElement
    const embeddingRow = screen.getByText('默认向量模型').closest('div') as HTMLElement
    fireEvent.click(within(chatRow).getByRole('button', { name: 'm-1' }))
    fireEvent.click(within(embeddingRow).getByRole('button', { name: 'm-2' }))
    fireEvent.click(screen.getByRole('button', { name: '保存默认模型' }))
    await waitFor(() =>
      expect(mock.saveDefaults).toHaveBeenCalledWith(
        expect.objectContaining({ chat: 'm-1', embedding: 'm-2' }),
      ),
    )
    expect(toast.success).toHaveBeenCalledWith('默认模型已保存')
  })

  it('「添加时一并填写模型参数」：展开落触碰标记、编辑参数随添加模型提交；收起早退', async () => {
    await renderLoaded()
    const details = screen
      .getByText('添加时一并填写模型参数（可选）')
      .closest('details') as HTMLDetailsElement
    expect(details).not.toBeNull()

    // 展开（jsdom 不自动派发 toggle，手工置 open 后派发，与真实展开同形）
    details.open = true
    fireEvent(details, new Event('toggle'))
    // 展开（触碰标记落位）后编辑思考模式 → onChange 落草稿
    fireEvent.change(within(details).getByLabelText('思考模式'), {
      target: { value: 'enabled' },
    })

    // 手填模型走添加链路：草稿参与提交（default_params 合并 thinking.type）
    mock.addModel.mockResolvedValueOnce({ models: configFixture.models, added_ids: ['manual-x'] })
    const panel = modelSection()
    fireEvent.change(within(panel).getByLabelText('自定义模型名'), {
      target: { value: 'manual-x' },
    })
    fireEvent.keyDown(within(panel).getByLabelText('自定义模型名'), { key: 'Enter' })
    // 草稿计数上屏后再点（负载下 Enter→草稿落位有异步窗口，即时点会找不到 (1)）
    await waitFor(() =>
      expect(within(panel).getByRole('button', { name: /添加模型 \(1\)/ })).toBeInTheDocument(),
    )
    fireEvent.click(within(panel).getByRole('button', { name: /添加模型 \(1\)/ }))
    await waitFor(() =>
      expect(mock.addModel).toHaveBeenCalledWith(
        'manual-x',
        expect.objectContaining({
          default_params: expect.objectContaining({ thinking: { type: 'enabled' } }),
        }),
      ),
    )

    // 收起路径：toggle 且 open=false → 早退（不重复置触碰标记，不抛错）
    details.open = false
    fireEvent(details, new Event('toggle'))
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('未展开参数面板时手填模型：草稿不参与提交（远端事实上限不被默认值覆盖）', async () => {
    await renderLoaded()
    mock.addModel.mockResolvedValueOnce({ models: configFixture.models, added_ids: ['plain-1'] })
    const panel = modelSection()
    fireEvent.change(within(panel).getByLabelText('自定义模型名'), {
      target: { value: 'plain-1' },
    })
    fireEvent.keyDown(within(panel).getByLabelText('自定义模型名'), { key: 'Enter' })
    // 草稿计数上屏后再点（负载下 Enter→草稿落位有异步窗口，即时点会找不到 (1)）
    await waitFor(() =>
      expect(within(panel).getByRole('button', { name: /添加模型 \(1\)/ })).toBeInTheDocument(),
    )
    fireEvent.click(within(panel).getByRole('button', { name: /添加模型 \(1\)/ }))
    await waitFor(() => expect(mock.addModel).toHaveBeenCalledTimes(1))
    expect(mock.addModel).toHaveBeenCalledWith(
      'plain-1',
      expect.not.objectContaining({ context_window: expect.anything() }),
    )
  })
})

describe('LlmSettingsPage 缺口：错误路径与成功路径', () => {
  it('默认模型保存失败 / 删除模型失败 / 模型设置保存失败 → toast.error 带描述', async () => {
    await renderLoaded()
    mock.saveDefaults.mockRejectedValueOnce(undefined)
    fireEvent.click(screen.getByRole('button', { name: '保存默认模型' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存默认模型失败', {
        description: '保存默认模型失败',
      }),
    )

    mock.deleteModel.mockRejectedValueOnce({ message: 'has refs' })
    fireEvent.click(within(modelRegistrySection()).getAllByRole('button', { name: '删除' })[1])
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('删除模型失败', { description: 'has refs' }),
    )

    mock.updateModel.mockRejectedValueOnce({ message: 'bad params' })
    fireEvent.click(within(modelRegistrySection()).getAllByRole('button', { name: /参数/ })[1])
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('保存模型设置失败', { description: 'bad params' }),
    )
  })

  it('删除模型成功：静默更新配置（不弹错）', async () => {
    await renderLoaded()
    mock.deleteModel.mockResolvedValueOnce({ 'm-2': configFixture.models['m-2'] })
    fireEvent.click(within(modelRegistrySection()).getAllByRole('button', { name: '删除' })[0])
    await waitFor(() => expect(mock.deleteModel).toHaveBeenCalledWith('m-1'))
    await waitFor(() => expect(screen.getByText('已注册模型 (1)')).toBeInTheDocument())
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('模型行「参数」面板：完整字段保存 → updateModel 合并保留原字段', async () => {
    await renderLoaded()
    const paramButtons = within(modelRegistrySection()).getAllByRole('button', { name: /参数/ })
    fireEvent.click(paramButtons[1])
    const panel = paramButtons[1].closest('div.bg-card') as HTMLElement

    // 上下文窗口（样例模型未设置 → 空输入）
    fireEvent.change(within(panel).getByPlaceholderText('未设置'), { target: { value: '1000000' } })
    // 采样参数 temperature 0.5 → 0.9
    fireEvent.change(within(panel).getByDisplayValue('0.5'), { target: { value: '0.9' } })
    // think 参数
    fireEvent.change(within(panel).getByLabelText('思考模式'), { target: { value: 'enabled' } })
    fireEvent.change(within(panel).getByLabelText('推理力度'), { target: { value: 'high' } })
    // 自定义参数：字符串与数字各一条（类型自动解析）
    fireEvent.change(within(panel).getByLabelText('自定义参数名'), {
      target: { value: 'service_tier' },
    })
    fireEvent.change(within(panel).getByLabelText('自定义参数值'), { target: { value: 'auto' } })
    fireEvent.click(within(panel).getByRole('button', { name: '加入' }))
    fireEvent.change(within(panel).getByLabelText('自定义参数名'), { target: { value: 'top_k' } })
    fireEvent.change(within(panel).getByLabelText('自定义参数值'), { target: { value: '50' } })
    fireEvent.keyDown(within(panel).getByLabelText('自定义参数值'), { key: 'Enter' })
    expect(within(panel).getByText(/service_tier=auto/)).toBeInTheDocument()
    expect(within(panel).getByText(/top_k=50/)).toBeInTheDocument()

    fireEvent.click(within(panel).getByRole('button', { name: '保存设置' }))
    await waitFor(() =>
      expect(mock.updateModel).toHaveBeenCalledWith('m-2', {
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
      }),
    )
    expect(toast.success).toHaveBeenCalledWith('已保存 m-2 的模型设置')
  })

  it('拉取添加部分失败 → toast.warning 带失败清单；全部失败 → toast.error', async () => {
    await renderLoaded()
    const panel = modelSection()
    // 第一轮：两个模型一成一败 → warning
    mock.addModel.mockResolvedValueOnce({ models: configFixture.models, added_ids: ['gpt-a'] })
    mock.addModel.mockRejectedValueOnce({ message: 'dup' })
    fireEvent.click(within(panel).getByRole('button', { name: /拉取模型列表/ }))
    const customInput = within(panel).getByLabelText('自定义模型名')
    await waitFor(() => expect(within(panel).getByText('gpt-a')).toBeInTheDocument())
    fireEvent.change(customInput, { target: { value: 'gpt-a' } })
    fireEvent.keyDown(customInput, { key: 'Enter' })
    fireEvent.change(customInput, { target: { value: 'gpt-b' } })
    fireEvent.keyDown(customInput, { key: 'Enter' })
    fireEvent.click(within(panel).getByRole('button', { name: /添加模型 \(2\)/ }))
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith('已添加 1 个模型', {
        description: expect.stringContaining('gpt-b'),
      }),
    )

    // 第二轮：全部失败 → error
    mock.addModel.mockRejectedValue({ message: 'dup' })
    fireEvent.change(within(panel).getByLabelText('自定义模型名'), { target: { value: 'gpt-c' } })
    fireEvent.keyDown(within(panel).getByLabelText('自定义模型名'), { key: 'Enter' })
    // 草稿计数上屏后再点（负载下 Enter→草稿落位有异步窗口，即时点会找不到 (1)）
    await waitFor(() =>
      expect(within(panel).getByRole('button', { name: /添加模型 \(1\)/ })).toBeInTheDocument(),
    )
    fireEvent.click(within(panel).getByRole('button', { name: /添加模型 \(1\)/ }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('添加模型失败', { description: 'gpt-c' }),
    )
  })

  it('拉取失败：同界面显示错误并可继续手填模型名', async () => {
    await renderLoaded()
    mock.getRemoteModels.mockRejectedValueOnce({ message: 'provider down' })
    const panel = modelSection()
    fireEvent.click(within(panel).getByRole('button', { name: /拉取模型列表/ }))
    await waitFor(() => expect(within(panel).getByText('provider down')).toBeInTheDocument())
    expect(within(panel).getByText(/仍可在下方手动输入模型名添加/)).toBeInTheDocument()

    // 手填路径仍可用
    mock.addModel.mockResolvedValueOnce({ models: configFixture.models, added_ids: ['x'] })
    fireEvent.change(within(panel).getByLabelText('自定义模型名'), {
      target: { value: 'manual-1' },
    })
    fireEvent.keyDown(within(panel).getByLabelText('自定义模型名'), { key: 'Enter' })
    // 草稿计数上屏后再点（负载下 Enter→草稿落位有异步窗口，即时点会找不到 (1)）
    await waitFor(() =>
      expect(within(panel).getByRole('button', { name: /添加模型 \(1\)/ })).toBeInTheDocument(),
    )
    fireEvent.click(within(panel).getByRole('button', { name: /添加模型 \(1\)/ }))
    await waitFor(() => expect(mock.addModel).toHaveBeenCalledTimes(1))
  })
})
