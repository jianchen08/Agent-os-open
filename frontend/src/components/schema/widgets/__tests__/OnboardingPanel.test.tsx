// @feature FP-0.2.四 前端Schema 引导面板 widget | @ci frontend-test
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { OnboardingPanel } from '../OnboardingPanel'
import type { Walkthrough } from '@/services/onboarding/types'

// ── 外部依赖 mock（API/宿主服务；组件内部逻辑走真实实现） ──────────────────────

const postProgressUpdate = vi.fn()
const openWorkspacePanelByPath = vi.fn()

vi.mock('@/services/onboarding/content', () => ({
  ONBOARDING_PLUGIN_ID: 'onboarding_service',
  fetchOnboardingContent: vi.fn(async () => ({ walkthroughs: contentFixture })),
  fetchOnboardingProgress: vi.fn(async () => ({ progress: progressFixture })),
  postProgressUpdate: (update: unknown) => postProgressUpdate(update),
  panelPathToTabId: (path: string) => (path === '/tasks' ? 'ws-panel-tasks' : null),
}))

vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn(async (endpoint: string) => ({ data: apiResultsFixture[endpoint] ?? {} })),
  },
}))

vi.mock('@/services/workspacePanelOpener', () => ({
  openWorkspacePanelByPath: (path: string) => openWorkspacePanelByPath(path),
  TOP_NAV_PANELS: { '/tasks': { id: 'ws-panel-tasks' } },
}))

vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: { getPages: vi.fn(() => []) },
}))

vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: (selector: (s: { workspaceTabs: unknown[] }) => unknown) =>
    selector({ workspaceTabs: [] }),
}))

vi.mock('react-markdown', () => ({ default: ({ children }: { children: string }) => <div>{children}</div> }))

// ── 夹具 ──────────────────────────────────────────────────────────────────────

const contentFixture: Walkthrough[] = [
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
          type: 'all',
          conditions: [
            { type: 'api_check', endpoint: '/ext/llm_service/config/llm', json_path: 'providers', op: 'non_empty' },
            { type: 'api_check', endpoint: '/ext/llm_service/config/llm', json_path: 'defaults.chat', op: 'non_empty' },
          ],
        },
      },
      {
        id: 'first_task',
        title: '交给它第一个任务',
        body: '正文',
        cta: { label: '打开任务管理', action: { type: 'open_panel', target: '/tasks' } },
        completion: { type: 'panel_visited', panel: '/tasks' },
      },
    ],
  },
  {
    id: 'task_management',
    title: '任务管理',
    description: '认识任务树',
    order: 2,
    default_open: false,
    steps: [{ id: 'meet', title: '认识任务管理页', body: '正文', completion: { type: 'manual' } }],
  },
]

let progressFixture: Record<string, never> = {}
let apiResultsFixture: Record<string, unknown> = {}

beforeEach(() => {
  vi.clearAllMocks()
  progressFixture = {}
  apiResultsFixture = {}
})

// ── 用例 ──────────────────────────────────────────────────────────────────────

describe('OnboardingPanel', () => {
  it('渲染 walkthrough 导航与默认展开的步骤卡', async () => {
    render(<OnboardingPanel />)
    // 「开始使用」同时出现在左列导航与右栏 h2（findAll 容忍多重匹配）
    expect((await screen.findAllByText('开始使用')).length).toBeGreaterThanOrEqual(1)
    expect(await screen.findByText('任务管理')).toBeInTheDocument()
    expect(await screen.findByText('配置模型与 API')).toBeInTheDocument()
  })

  it('已持久化的 manual 进度直接呈已完成态', async () => {
    progressFixture = { get_started: { welcome: { done: true, done_at: 1, how: 'manual' } } } as never
    render(<OnboardingPanel />)
    expect((await screen.findAllByText('已完成')).length).toBeGreaterThan(0)
  })

  it('manual 步骤「标记完成」按载荷落账', async () => {
    postProgressUpdate.mockResolvedValue({ progress: {} })
    render(<OnboardingPanel />)
    const buttons = await screen.findAllByRole('button', { name: '标记完成' })
    fireEvent.click(buttons[0])
    await waitFor(() =>
      expect(postProgressUpdate).toHaveBeenCalledWith({
        walkthrough_id: 'get_started',
        step_id: 'welcome',
        done: true,
        how: 'manual',
      }),
    )
  })

  it('CTA open_panel 打开宿主页签', async () => {
    render(<OnboardingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '打开任务管理' }))
    expect(openWorkspacePanelByPath).toHaveBeenCalledWith('/tasks')
  })

  it('api_check 双检满足后自动落账（how=复合类型）', async () => {
    apiResultsFixture = {
      '/ext/llm_service/config/llm': { providers: { deepseek: {} }, defaults: { chat: 'm' } },
    }
    postProgressUpdate.mockResolvedValue({ progress: {} })
    render(<OnboardingPanel />)
    await waitFor(() =>
      expect(postProgressUpdate).toHaveBeenCalledWith({
        walkthrough_id: 'get_started',
        step_id: 'configure_llm',
        done: true,
        how: 'all',
      }),
    )
  })

  it('api_check 部分满足（providers 有值、defaults.chat 空）不落账', async () => {
    apiResultsFixture = {
      '/ext/llm_service/config/llm': { providers: { deepseek: {} }, defaults: { chat: '' } },
    }
    render(<OnboardingPanel />)
    await new Promise((r) => setTimeout(r, 20))
    expect(postProgressUpdate).not.toHaveBeenCalled()
  })

  it('向导步骤卡提供「打开配置向导」入口', async () => {
    render(<OnboardingPanel />)
    expect(await screen.findByRole('button', { name: '打开配置向导' })).toBeInTheDocument()
  })
})
