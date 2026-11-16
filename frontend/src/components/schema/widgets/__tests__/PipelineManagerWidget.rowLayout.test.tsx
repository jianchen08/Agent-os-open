// @feature FP-T12 前端适配 | @vision V6 可即用 | @ci frontend-test
/**
 * PipelineManagerWidget 条目行布局契约测试（2026-10-23 用户反馈三问题）
 *
 * 覆盖任务管理面板条目行的三块布局/交互契约：
 * - ①标题优先：任务标题占主位（min-w-0 + truncate 超长省略号）；行首身份位 =
 *   agent 名徽标（用户裁定 2026-11-06：任务/会话类型徽标退役、代号 ID 不进行内
 *   ——注册表命中显配置名，未命中回退 stem 短名）；状态/时长/token shrink-0；
 * - ②展开收起放宽：点整行任意位置切换详情展开/收起（不只详情按钮），详情按钮
 *   带箭头提示展开态（aria-expanded + rotate-90）；
 * - ③按钮区：行级「只看此会话」按钮已下线（用户裁定无用且把其后操作按钮挤出
 *   视野）；操作按钮区独立于信息区且 shrink-0，标签再多也不被推出屏幕外。
 *
 * 契约载体：data-testid（pipeline-entry-title / -meta / -agent / -actions），
 * - ④信息密度（2026-10-25 用户反馈同类信息重复表达）：状态只留文字标签
 *   （不渲染独立状态图标）；任务状态文字下线；模式徽标不进行内，收进
 *   详情面板且为纯文字单形态；模式 ID 只显示短名（不显全路径）。
 * 布局断言读 className（jsdom 无真实排版，class 契约即布局契约）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
// eslint-disable-next-line import-x/order -- pmTestUtils 须先于被测组件求值（vi.mock 工厂时序约束，见 pmTestUtils 头注；禁用排序自动修防回归）
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

vi.mock('@/services/api/tasks', () => pmMod.tasksApi())
vi.mock('@/services/api/client', () => pmMod.apiClient())
vi.mock('@/services/pipelineNavigator', () => pmMod.pipelineNavigator())
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => pmMod.pipelineRunsQuery())
vi.mock('@/hooks/queries/useAllTasksQuery', () => pmMod.allTasksQuery())
vi.mock('@/hooks/queries/useLongTermTasksQuery', () => pmMod.longTermTasksQuery())
vi.mock('@/hooks/queries/useSessionsQuery', () => pmMod.sessionsQuery())
vi.mock('@/hooks/queries/useAgentsQuery', () => pmMod.agentsQuery())
vi.mock('@/stores/contextUsageStore', () => pmMod.contextUsageStore())
vi.mock('@/stores/sessionListStore', () => pmMod.sessionListStore())
vi.mock('@/services/api/modes', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useModesRegistry: () => pmSeed.modesRegistry,
}))

/** 超长任务标题（标题主位契约的区分度输入：长到必然溢出） */
const LONG_TITLE = '超长任务标题'.repeat(8)
/** 长 agent 全名（mode_xxx/ 前缀 + 长 stem，用户 2026-10-23 实报的形态） */
const LONG_AGENT = 'mode_coding/programming_orchestrator_agent_v2'

/** 播种单条运行中任务条目（标题长 + agent 名长） */
function seedLongRow() {
  pmSeed.runs.r1 = {
    pipeline_id: 'pipe-1',
    run_id: 'run-1',
    thread_id: 'th-1',
    // 统一管道标题（ADR 2026-10-27）：行名 = run.title
    title: LONG_TITLE,
    status: 'running',
    started_at: '2026-10-01T00:00:00Z',
  }
  pmSeed.tasks.push({
    id: 't-1',
    title: LONG_TITLE,
    status: 'running',
    pipeline_run_id: 'pipe-1',
    agent_name: LONG_AGENT,
  })
}

describe('PipelineManagerWidget 条目行布局契约', () => {
  beforeEach(() => {
    resetPmContainers()
  })

  it('①标题主位：min-w-0 + truncate，悬停 tooltip 给全文', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)

    const title = (await screen.findAllByText(LONG_TITLE))[0]
    const box = title.closest('[data-testid="pipeline-entry-title"]')
    expect(box).not.toBeNull()
    // 超长省略号（truncate）+ 可压缩（min-w-0）——标题不被标签挤没
    expect(box!.className).toContain('truncate')
    expect(box!.className).toContain('min-w-0')
    // 悬停 tooltip：全文（省略号之外仍可读）
    expect(box!.getAttribute('title')).toBe(LONG_TITLE)
  })

  it('①agent 徽标只显名：ID/前缀不作可见文本也不进 tooltip（用户裁定 2026-11-06）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    // 全名（ID 形态）不作可见文本（否则遮住标题）
    expect(screen.queryByText(LONG_AGENT)).toBeNull()
    const badge = document.querySelector('[data-testid="pipeline-entry-agent"]')
    expect(badge).not.toBeNull()
    // 注册表未命中 → stem 短名回退
    expect(badge!.textContent).toBe('programming_orchestrator_agent_v2')
    // ID 不进悬停提示：title 恒缺席（显示名在场即无 ID 表达）
    expect(badge!.getAttribute('title')).toBeNull()
  })

  it('①agent 注册表命中显配置名：主管道 agentos → 灵汐（不显 ID）', async () => {
    pmSeed.agents.push({ id: 'agentos', configId: 'agentos', name: '灵汐' })
    pmSeed.runs.r1 = {
      pipeline_id: 'pipe-main',
      run_id: 'run-main',
      thread_id: 'th-main',
      title: '新会话',
      status: 'running',
      started_at: '2026-10-01T00:00:00Z',
    }
    // 执行身份 state 真值（session router 交道 overlay 写入的 agent.id）
    pmSeed.states['pipe-main'] = {
      pipeline_id: 'pipe-main',
      thread_id: 'th-main',
      source: 'memory',
      state: { 'agent.id': 'agentos' },
    }
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('新会话')

    const badge = document.querySelector('[data-testid="pipeline-entry-agent"]')
    expect(badge).not.toBeNull()
    expect(badge!.textContent).toBe('灵汐')
    expect(badge!.textContent).not.toContain('agentos')
  })

  it('①agent 徽标可压缩（min-w-0）：不把标题与操作按钮挤出视野', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    const badge = document.querySelector('[data-testid="pipeline-entry-agent"]')!
    expect(badge.className).toContain('min-w-0')
    expect(badge.className).toContain('truncate')
  })

  it('①状态/时长/token 标签 shrink-0 固定不挤', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    const meta = document.querySelector('[data-testid="pipeline-entry-meta"]')!
    const shrinkFixed = Array.from(meta.querySelectorAll('span')).filter((el) =>
      el.className.includes('shrink-0'),
    )
    // 状态标签 + 耗时 + token 三者均不被压缩变形
    expect(shrinkFixed.length).toBeGreaterThanOrEqual(3)
  })

  it('②整行点按切换详情展开/收起（不只详情按钮）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    const title = (await screen.findAllByText(LONG_TITLE))[0]

    // 首次点整行 → 详情展开
    fireEvent.click(title)
    expect(await screen.findByText('管道 ID')).toBeInTheDocument()

    // 再点整行 → 详情收起（交互不再只由详情按钮兜着）
    fireEvent.click(title)
    await waitFor(() => expect(screen.queryByText('管道 ID')).toBeNull())
  })

  it('②详情按钮带箭头提示展开态（aria-expanded + rotate-90）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    const btn = () => document.querySelector('button[aria-label="切换详细信息"]') as HTMLElement
    expect(btn()).not.toBeNull()
    expect(btn().getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(btn())
    await waitFor(() => expect(btn().getAttribute('aria-expanded')).toBe('true'))
    const svg = btn().querySelector('svg')
    expect(svg?.getAttribute('class')).toContain('rotate-90')
  })

  it('③行级「只看本会话」按钮已下线（其后操作按钮不再被挤出视野）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    expect(screen.queryAllByLabelText('只看此会话')).toHaveLength(0)
  })

  it('③操作按钮区独立于信息区且 shrink-0：不被标签挤出屏幕外', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    const actions = document.querySelector('[data-testid="pipeline-entry-actions"]')!
    expect(actions.className).toContain('shrink-0')
    // 操作按钮区不在「信息区」内（信息区可压缩自裁剪，标签不推挤操作区）
    expect(actions.closest('[data-testid="pipeline-entry-meta"]')).toBeNull()
    // 打开对话按钮仍在行内可达
    expect(
      actions.querySelector('button[aria-label="打开对话"]'),
    ).not.toBeNull()
  })

  it('①区分度输入：无 agent 名时渲染 agent 徽标位（不产生空徽标）', async () => {
    // 对照组：任务无 agent_name → 不渲染 agent 徽标
    pmSeed.runs.r1 = {
      pipeline_id: 'pipe-2',
      run_id: 'run-2',
      thread_id: 'th-2',
      title: '无 agent 任务',
      status: 'running',
      started_at: '2026-10-01T00:00:00Z',
    }
    pmSeed.tasks.push({
      id: 't-2',
      title: '无 agent 任务',
      status: 'running',
      pipeline_run_id: 'pipe-2',
    })
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('无 agent 任务')

    expect(document.querySelector('[data-testid="pipeline-entry-agent"]')).toBeNull()
  })

  describe('PipelineManagerWidget 条目行信息密度（同类信息单一表达形态）', () => {
    /** 模式键（模式徽标 + 模式 ID 短名契约用） */
    const MODE = 'mode_coding/code_writer'

    /** 模式面板页声明（模式徽标可渲染的前置：查不到映射 = 不渲染） */
    function seedModePanelPage() {
      contributionRegistry.clear()
      contributionRegistry.loadFromSchema({
        plugin_contributes: [
          {
            plugin_id: 'plug_code',
            plugin_name: 'plug_code',
            contributes: {
              pages: [
                {
                  id: 'code_desk',
                  title: '编码面板',
                  space: 'workspace',
                  slot: 'tab',
                  widget: 'webview',
                  mode: MODE,
                },
              ],
            },
          },
        ],
        plugin_configs: [],
      })
    }

    /** 带模式标签的运行中任务行（state.mode → modeTags） */
    function seedModeRow() {
      seedLongRow()
      pmSeed.usePipelineStatesQuery.mockReturnValue({
        data: {
          'pipe-1': {
            pipeline_id: 'pipe-1',
            thread_id: 'th-1',
            source: 'memory',
            state: { mode: MODE, 'task.status': 'running' },
          },
        },
      })
    }

    /** 行容器（data-entry-key 定位：整行点按的宿主） */
    function entryRow(titleText: HTMLElement): HTMLElement {
      return (titleText.closest('div[data-entry-key]') ?? titleText.closest('div')) as HTMLElement
    }

    beforeEach(() => {
      seedModePanelPage()
      pmSeed.modesRegistry.modes = [
        {
          mode: MODE,
          name: '编码模式',
          pipelines: [],
          presenter: { source: 'none' },
          tool_card: 'native',
          icon: '🔨',
          plugin_id: 'plug_code',
        },
      ]
    })

    afterEach(() => {
      contributionRegistry.clear()
    })

    it('①状态单一形态：只留状态文字标签，不渲染独立状态图标', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      // 状态图标（h-4 w-4）下线——状态只剩文字标签一种表达
      expect(row.querySelectorAll('svg.h-4.w-4')).toHaveLength(0)
      // 状态文字保留（颜色语义在文字徽标上）
      expect(row.textContent).toContain('运行中')
    })

    it('④任务区不重复：任务状态文字下线（任务行前已有类型标识）', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      expect(row.textContent).not.toContain('任务:')
    })

    it('②③模式标记归入次要区：不在共有特征区内（agent 名已升行首身份位）', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      // 模式徽标在行内次要区（agent 名徽标已上移行首，不在次要区重复表达）
      const modeArea = row.querySelector('[data-testid="pipeline-entry-mode"]')
      expect(modeArea).not.toBeNull()
      expect(modeArea!.querySelector(`[data-testid="mode-badge-${MODE}"]`)).not.toBeNull()
      // 模式名不混入共有特征区
      expect(row.querySelector('[data-testid="pipeline-entry-common"]')!.textContent).not.toContain(
        '编码模式',
      )
    })

    it('②模式徽标单形态：纯文字（无图标），详情面板不重复展示', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const badge = await screen.findByTestId(`mode-badge-${MODE}`)
      // 单一表达形态：只文字，无图标
      expect(badge.textContent).toBe('编码模式')
      expect(badge.textContent).not.toContain('🔨')
      // 详情面板不重复展示模式（同一信息只保留一处表达）
      fireEvent.click(title)
      await screen.findByText('管道 ID')
      expect(screen.queryByTestId('pipeline-detail-mode')).toBeNull()
    })

    it('②模式 ID 只显示短名（无 registry 声明时取末段，不显全路径）', async () => {
      pmSeed.modesRegistry.modes = [] // registry 无该模式声明
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const badge = await screen.findByTestId(`mode-badge-${MODE}`)
      expect(badge.textContent).toBe('code_writer')
      expect(badge.textContent).not.toContain('mode_coding/')
    })

    it('③agent 只显短名（来源与全路径不进可见文本）', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      const badges = row.querySelectorAll('[data-testid="pipeline-entry-agent"]')
      expect(badges).toHaveLength(1)
      expect(badges[0].textContent).toBe('programming_orchestrator_agent_v2')
      // 短名不显示 mode_ 前缀全路径
      expect(row.textContent).not.toContain('mode_coding/')
    })

    it('共有特征区顺序恒定：标题 → 状态 → 运行时长 → token', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const common = entryRow(title).querySelector('[data-testid="pipeline-entry-common"]')
      expect(common).not.toBeNull()
      const order = Array.from(common!.children).map((el) => el.getAttribute('data-testid'))
      expect(order).toEqual([
        'pipeline-entry-title',
        'pipeline-entry-status',
        'pipeline-entry-duration',
        'pipeline-entry-token',
      ])
    })

    it('模式/执行者信息归入独立次要区（不侵占共有特征位）', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      const modeArea = row.querySelector('[data-testid="pipeline-entry-mode"]')
      expect(modeArea).not.toBeNull()
      // 次要区不在共有特征区内——共有特征位置不被模式信息挤动
      expect(modeArea!.closest('[data-testid="pipeline-entry-common"]')).toBeNull()
      // 共有特征区内不含模式/执行者元素
      const common = row.querySelector('[data-testid="pipeline-entry-common"]')!
      expect(common.querySelector('[data-testid="pipeline-entry-agent"]')).toBeNull()
      expect(common.querySelector('[data-testid^="mode-badge-"]')).toBeNull()
    })

    it('模式特有信息只在该模式存在时显示（无模式/执行者的行无次要区）', async () => {
      // 对照组：无 agent、无 mode state 的任务行
      pmSeed.runs.r2 = {
        pipeline_id: 'pipe-plain',
        run_id: 'run-plain',
        thread_id: 'th-plain',
        title: '无模式任务',
        status: 'running',
        started_at: '2026-10-01T00:00:00Z',
      }
      pmSeed.tasks.push({
        id: 't-plain',
        title: '无模式任务',
        status: 'running',
        pipeline_run_id: 'pipe-plain',
      })
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      await screen.findAllByText(LONG_TITLE)
      const plain = (await screen.findAllByText('无模式任务'))[0].closest(
        'div[data-entry-key]',
      ) as HTMLElement
      expect(plain.querySelector('[data-testid="pipeline-entry-mode"]')).toBeNull()
      // 有模式/执行者的行才有次要区
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      expect(entryRow(title).querySelector('[data-testid="pipeline-entry-mode"]')).not.toBeNull()
    })

    it('不同模式任务行结构一致：共有区与次要区并列，差异只在次要区内容', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const meta = entryRow(title).querySelector('[data-testid="pipeline-entry-meta"]')!
      const kids = Array.from(meta.children).map((el) => el.getAttribute('data-testid'))
      // 共有区在前、次要区在后 —— 两块并列，模式信息不插入共有区内部
      expect(kids[0]).toBe('pipeline-entry-common')
      expect(kids).toContain('pipeline-entry-mode')
    })
  })
})
