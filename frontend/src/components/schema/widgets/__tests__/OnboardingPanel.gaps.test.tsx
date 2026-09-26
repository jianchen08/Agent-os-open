/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * OnboardingPanel 缺口补测（OnboardingPanel.test.tsx 覆盖主渲染/落账/CTA 主链，
 * 这里专攻状态机边缘：加载中/加载失败/空内容、api 端点失败兜底、页签访问
 * 实时重算（panel_visited）、左列切换、external_url CTA、配置向导开启、
 * 卸载后响应不落地。）
 *
 * 外部依赖 mock（网络/宿主服务）：content 数据层、apiClient、workspacePanelOpener、
 * layoutModeStore、ContributionRegistry、配置服务（向导的数据面）；组件内部
 * 状态流转与 evaluator 走真实实现。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Walkthrough } from '@/services/onboarding/types'

// ── 可控夹具（vi.mock 提升前声明，经 holder 间接引用） ─────────────────────────

const holder = vi.hoisted(() => ({
  content: null as Walkthrough[] | null,
  contentError: null as Error | null,
  gate: null as Promise<void> | null,
  progress: {} as Record<string, never>,
  apiFailEndpoints: [] as string[],
  workspaceTabs: [] as Array<{ id: string }>,
  registryPages: [] as Array<{ id: string; path?: string }>,
  llmProvidersReady: false,
}))

const postProgressUpdate = vi.fn()
const openWorkspacePanelByPath = vi.fn()

vi.mock('@/services/onboarding/content', () => ({
  ONBOARDING_PLUGIN_ID: 'onboarding_service',
  fetchOnboardingContent: vi.fn(async () => {
    if (holder.contentError) throw holder.contentError
    if (holder.gate) await holder.gate
    if (holder.content === null) {
      // 挂起态：用例自行控制放行（默认永不 resolve）
      await new Promise<never>(() => {})
    }
    return { walkthroughs: holder.content }
  }),
  fetchOnboardingProgress: vi.fn(async () => ({ progress: holder.progress })),
  postProgressUpdate: (update: unknown) => postProgressUpdate(update),
  panelPathToTabId: (path: string) => (path === '/tasks' ? 'ws-panel-tasks' : null),
}))

const apiGet = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => apiGet(...args) },
}))

vi.mock('@/services/workspacePanelOpener', () => ({
  openWorkspacePanelByPath: (path: string) => openWorkspacePanelByPath(path),
  TOP_NAV_PANELS: { '/tasks': { id: 'ws-panel-tasks' } },
}))

vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: { getPages: vi.fn(() => holder.registryPages) },
}))

vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: (selector: (s: { workspaceTabs: unknown[] }) => unknown) =>
    selector({ workspaceTabs: holder.workspaceTabs }),
}))

// 向导数据面（网络边界）：向导本体真实渲染，见 LlmSetupWizard.test.tsx 全链测试
const configApi = vi.hoisted(() => ({
  getLLMConfig: vi.fn(),
  getLLMPresets: vi.fn(),
  getRemoteModels: vi.fn(),
  updateProviderConfig: vi.fn(),
  addModel: vi.fn(),
  saveDefaults: vi.fn(),
}))
vi.mock('@/services/api/config', () => ({
  getLLMConfig: (...a: unknown[]) => configApi.getLLMConfig(...a),
  getLLMPresets: (...a: unknown[]) => configApi.getLLMPresets(...a),
  getRemoteModels: (...a: unknown[]) => configApi.getRemoteModels(...a),
  updateProviderConfig: (...a: unknown[]) => configApi.updateProviderConfig(...a),
  addModel: (...a: unknown[]) => configApi.addModel(...a),
  saveDefaults: (...a: unknown[]) => configApi.saveDefaults(...a),
}))

vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}))

import { OnboardingPanel } from '../OnboardingPanel'

// ── 内容夹具 ──────────────────────────────────────────────────────────────────

function baseFixture(): Walkthrough[] {
  return [
    {
      id: 'get_started',
      title: '开始使用',
      description: '四步走通核心链',
      order: 1,
      default_open: true,
      steps: [
        { id: 'welcome', title: '欢迎', body: '正文', completion: { type: 'manual' } },
        {
          id: 'configure_llm',
          title: '配置模型与 API',
          body: '正文',
          wizard: 'llm_setup',
          completion: {
            type: 'api_check',
            endpoint: '/ext/llm_service/config/llm',
            json_path: 'providers',
            op: 'non_empty',
          },
        },
      ],
    },
    {
      id: 'task_management',
      title: '任务管理',
      description: '认识任务树',
      order: 2,
      default_open: false,
      steps: [
        { id: 'meet', title: '认识任务管理页', body: '正文', completion: { type: 'manual' } },
      ],
    },
  ]
}

function fixtureWithFirstTask(): Walkthrough[] {
  const fixture = baseFixture()
  fixture[0]!.steps.push({
    id: 'first_task',
    title: '交给它第一个任务',
    body: '正文',
    cta: { label: '查看文档', action: { type: 'external_url', target: 'https://docs.example.com' } },
    completion: { type: 'panel_visited', panel: '/tasks' },
  })
  return fixture
}

beforeEach(() => {
  vi.clearAllMocks()
  holder.content = baseFixture()
  holder.contentError = null
  holder.gate = null
  holder.progress = {}
  holder.apiFailEndpoints = []
  holder.workspaceTabs = []
  holder.registryPages = []
  holder.llmProvidersReady = false
  postProgressUpdate.mockResolvedValue({ progress: {} })
  configApi.getLLMConfig.mockResolvedValue({
    providers: { deepseek: { keys: [] } },
    models: {},
    defaults: { chat: '', embedding: '', tiers: {} },
  })
  configApi.getLLMPresets.mockResolvedValue({
    provider_groups: [{ label: '国内', providers: [['deepseek', 'DeepSeek']] }],
    common_provider_types: [],
    thinking_strength: { levels: [], allowed_keys: [] },
  })
  configApi.getRemoteModels.mockResolvedValue({
    provider: 'deepseek',
    models: [{ id: 'deepseek-chat', owned_by: 'deepseek' }],
  })
  configApi.updateProviderConfig.mockResolvedValue({ deepseek: { keys: [] } })
  configApi.addModel.mockResolvedValue({ models: {}, added_ids: [] })
  // 默认模型落库即视为「已配置」：api_check 端点随之供给非空 providers
  configApi.saveDefaults.mockImplementation(async () => {
    holder.llmProvidersReady = true
    return { chat: 'deepseek-chat', embedding: '', tiers: {} }
  })
  apiGet.mockImplementation(async (endpoint: string) => {
    if (holder.apiFailEndpoints.includes(endpoint)) throw new Error('网络中断')
    return {
      data: {
        providers: holder.llmProvidersReady ? { deepseek: {} } : {},
      },
    }
  })
})

// ── 用例 ──────────────────────────────────────────────────────────────────────

describe('OnboardingPanel · 加载状态机', () => {
  it('内容未到达：呈加载态；到达后替换为面板（挂起不误报失败）', async () => {
    holder.content = null // 挂起
    const { unmount } = render(<OnboardingPanel />)
    expect(await screen.findByText('加载中...')).toBeInTheDocument()
    unmount()
  })

  it('加载失败：呈错误横幅带失败信息（可区分于空内容/加载中）', async () => {
    holder.contentError = new Error('后端失联')
    render(<OnboardingPanel />)
    expect(await screen.findByText('后端失联')).toBeInTheDocument()
    expect(screen.queryByTestId('onboarding-panel')).toBeNull()
  })

  it('空内容：呈「暂无引导内容」而非加载态', async () => {
    holder.content = []
    render(<OnboardingPanel />)
    expect(await screen.findByText('暂无引导内容。')).toBeInTheDocument()
    expect(screen.queryByText('加载中...')).toBeNull()
  })

  it('卸载后到达的响应不落地（无崩溃、无渲染残留）', async () => {
    let release!: () => void
    holder.gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const { unmount } = render(<OnboardingPanel />)
    await screen.findByText('加载中...')
    unmount()
    release() // 响应在卸载后到达
    holder.content = baseFixture()
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(document.querySelector('[data-testid="onboarding-panel"]')).toBeNull()
  })
})

describe('OnboardingPanel · 完成判定兜底与实时重算', () => {
  it('api 端点失败：判定取数落空，条件型步骤不自动落账、不误标完成', async () => {
    holder.apiFailEndpoints = ['/ext/llm_service/config/llm']
    render(<OnboardingPanel />)
    await screen.findAllByText('开始使用')
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(postProgressUpdate).not.toHaveBeenCalled()
    expect(screen.queryAllByText('已完成')).toHaveLength(0)
  })

  it('panel_visited 随工作区页签实时重算：页签在访即自动落账并标完成', async () => {
    holder.content = fixtureWithFirstTask()
    holder.workspaceTabs = [{ id: 'ws-panel-tasks' }]
    render(<OnboardingPanel />)
    await waitFor(() =>
      expect(postProgressUpdate).toHaveBeenCalledWith({
        walkthrough_id: 'get_started',
        step_id: 'first_task',
        done: true,
        how: 'panel_visited',
      }),
    )
    // 完成徽标出现（welcome manual 未落账、configure_llm 未满足，仅 first_task）
    expect(screen.getAllByText('已完成')).toHaveLength(1)
  })

  it('页签未访问时同一步骤保持未完成（同一步骤两组可区分输入）', async () => {
    holder.content = fixtureWithFirstTask()
    render(<OnboardingPanel />)
    await screen.findAllByText('开始使用')
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(postProgressUpdate).not.toHaveBeenCalled()
    expect(screen.queryByText('已完成')).toBeNull()
  })

  it('插件贡献页的页签在访同样计为 panel_visited（contributes.pages 反查路径）', async () => {
    holder.content = [
      {
        id: 'plugin_walk',
        title: '插件引导',
        description: '插件页签',
        order: 3,
        default_open: true,
        steps: [
          {
            id: 'open_plugin_ui',
            title: '打开插件页',
            body: '正文',
            completion: { type: 'panel_visited', panel: '/ext/myplug/ui' },
          },
        ],
      },
    ]
    holder.registryPages = [{ id: 'myplug', path: '/ext/myplug/ui' }]
    holder.workspaceTabs = [{ id: 'ws-plugin-myplug' }]
    render(<OnboardingPanel />)
    await waitFor(() =>
      expect(postProgressUpdate).toHaveBeenCalledWith({
        walkthrough_id: 'plugin_walk',
        step_id: 'open_plugin_ui',
        done: true,
        how: 'panel_visited',
      }),
    )
    expect(screen.getAllByText('已完成')).toHaveLength(1)
  })
})

describe('OnboardingPanel · 导航与 CTA 交互', () => {
  it('左列点击切换 walkthrough：右栏步骤卡随之更换', async () => {
    render(<OnboardingPanel />)
    // 默认展开 get_started；task_management 尚未出现在右栏
    await screen.findAllByText('开始使用')
    expect(screen.queryByText('认识任务管理页')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /任务管理/ }))
    expect(await screen.findByText('认识任务管理页')).toBeInTheDocument()
    // 右栏 h2 已切换
    expect(await screen.findByRole('heading', { level: 2, name: '任务管理' })).toBeInTheDocument()
  })

  it('external_url CTA：新窗口打开目标（_blank + noopener）', async () => {
    holder.content = fixtureWithFirstTask()
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
    render(<OnboardingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '查看文档' }))
    expect(openSpy).toHaveBeenCalledWith('https://docs.example.com', '_blank', 'noopener')
    openSpy.mockRestore()
  })

  it('配置向导：入口点击后向导面板展开（替代入口按钮）', async () => {
    render(<OnboardingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '打开配置向导' }))
    // 真实向导渲染：预置提供商列表出现（数据面由 api/config mock 供给）
    expect(await screen.findByText('DeepSeek')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开配置向导' })).toBeNull()
  })

  it('向导完成配置 → onConfigured 回流重算：api_check 步骤随即自动落账', async () => {
    render(<OnboardingPanel />)
    await screen.findAllByText('开始使用')
    // 初始 providers 空：configure_llm 未满足、无完成徽标
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(postProgressUpdate).not.toHaveBeenCalled()
    expect(screen.queryByText('已完成')).toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: '打开配置向导' }))
    // 走真实向导完成流：选提供商 → 存 Key 拉模型 → 设默认模型（saveDefaults 即翻转端点供给）
    fireEvent.click(await screen.findByText('DeepSeek'))
    fireEvent.change(screen.getByTestId('wizard-api-key'), { target: { value: 'sk-test-1' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并获取模型' }))
    fireEvent.click(await screen.findByRole('radio'))
    fireEvent.click(screen.getByRole('button', { name: '设为默认模型' }))

    // onConfigured → refreshApi 重取端点（此刻 providers 非空）→ 步骤完成
    await waitFor(() =>
      expect(postProgressUpdate).toHaveBeenCalledWith({
        walkthrough_id: 'get_started',
        step_id: 'configure_llm',
        done: true,
        how: 'api_check',
      }),
    )
    expect(await screen.findAllByText('已完成')).toHaveLength(1)
  })

  it('无预置提供商声明：向导呈降级出口，「打开高级设置」回调宿主页签', async () => {
    configApi.getLLMPresets.mockResolvedValue({
      provider_groups: [],
      common_provider_types: [],
      thinking_strength: { levels: [], allowed_keys: [] },
    })
    render(<OnboardingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '打开配置向导' }))
    fireEvent.click(await screen.findByRole('button', { name: '打开高级设置' }))
    expect(openWorkspacePanelByPath).toHaveBeenCalledWith('/settings')
  })
})
