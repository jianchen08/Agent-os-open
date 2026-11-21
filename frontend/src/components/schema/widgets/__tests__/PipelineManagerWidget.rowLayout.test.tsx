// @feature FP-T12 前端适配 | @vision V6 可即用 | @ci frontend-test
/**
 * PipelineManagerWidget 条目行布局契约测试（方案 B，用户 2026-11-20 拍板）
 *
 * 行形态：[树chevron][状态图标][agent图标] 任务名称 …辅助信息簇… [发消息][⋮]
 * - ①标题主位：任务名称=按钮（点按行内展开详情手风琴），min-w-0 + truncate
 *   超长省略，tooltip 给全文；agent 图标替代名字文字（用户裁定 2026-11-06：
 *   代号 ID 不进行内——注册表命中显配置名，未命中回退 stem 短名；方案 B 起
 *   名字只进 tooltip/aria 与详情，行内只有图标，未配置走缺省图标不留空）；
 * - ②展开收起：点整行/点任务名称切换详情展开收起（aria-expanded 提示；
 *   不用 chevron 提示可展开——树形 chevron 已是「子级展开」语义）；
 * - ③按钮区：行级「只看此会话」按钮已下线；常驻只「发消息」「⋮」两件，
 *   操作区独立于辅助信息区且 shrink-0；
 * - ④信息密度：状态图标=类型+状态合一（data-status-kind 取件键，纯展示
 *   不可点），状态文字标签在辅助信息簇；模式徽标并入辅助信息簇（旧
 *   common/mode 双分区退役）；任务状态文字下线；模式 ID 只显示短名。
 *
 * 方案 B 适配登记（旧布局专属、新契约无对应形态的用例删除）：
 * - 「①agent 徽标可压缩（min-w-0/truncate）」删除——agent 名徽标已改固定尺寸
 *   图标（shrink-0），无文字压缩语义；
 * - 「模式/执行者信息归入独立次要区（不侵占共有特征位）」删除——旧
 *   pipeline-entry-common / pipeline-entry-mode 双分区退役，模式徽标并入
 *   辅助信息簇（顺序契约用例承载其位置）；
 * - 「不同模式任务行结构一致：共有区与次要区并列」删除——双分区结构退役，
 *   结构一致性由「行形态顺序恒定」用例承载。
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

  it('①标题主位：min-w-0 + truncate，悬停 tooltip 给全文（含展开语义提示）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)

    const box = (await screen.findAllByText(LONG_TITLE))[0].closest(
      '[data-testid="pipeline-entry-title"]',
    )
    expect(box).not.toBeNull()
    // 超长省略号（truncate）+ 可压缩（min-w-0）——标题不被图标挤没
    expect(box!.className).toContain('truncate')
    expect(box!.className).toContain('min-w-0')
    // 悬停 tooltip：全文 + 展开语义提示（点名称=详情手风琴入口，方案 B）
    expect(box!.getAttribute('title')).toBe(`${LONG_TITLE}（点击展开/收起详细信息）`)
  })

  it('①agent 图标只承载图标：名字不作行内可见文本，进 tooltip/aria（方案 B 图标化）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    // 全名（ID 形态）与短名都不作可见文本（名字转 tooltip/详情，行内只有图标）
    expect(screen.queryByText(LONG_AGENT)).toBeNull()
    expect(screen.queryByText('programming_orchestrator_agent_v2')).toBeNull()
    const icon = document.querySelector('[data-testid="pipeline-entry-agent-icon"]')
    expect(icon).not.toBeNull()
    // 注册表未命中 → stem 短名回退：tooltip 与 aria 都承载名字
    expect(icon!.getAttribute('title')).toBe('programming_orchestrator_agent_v2')
    expect(icon!.getAttribute('aria-label')).toBe('programming_orchestrator_agent_v2')
    // 图标在场（缺省兜底，不留空——agentIcon.ts 单一真值）
    expect(icon!.querySelector('svg')).not.toBeNull()
  })

  it('①agent 注册表命中显配置名：主管道 agentos → 灵汐（名字进 tooltip，不显 ID）', async () => {
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

    const icon = document.querySelector('[data-testid="pipeline-entry-agent-icon"]')
    expect(icon).not.toBeNull()
    expect(icon!.getAttribute('title')).toBe('灵汐')
    expect(icon!.getAttribute('aria-label')).toBe('灵汐')
    // 名字不作行内可见文本（ID「agentos」更不出现）
    expect(screen.queryByText('灵汐')).toBeNull()
    expect(icon!.textContent).not.toContain('agentos')
  })

  it('①状态/时长标签 shrink-0 固定不挤（P1-3 修订：token 空值不渲染占位——原「状态/时长/token 三者」契约让位于空值省略，真值在场时 token 位仍 shrink-0）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    const meta = document.querySelector('[data-testid="pipeline-entry-meta"]')!
    const shrinkFixed = Array.from(meta.querySelectorAll('span')).filter((el) =>
      el.className.includes('shrink-0'),
    )
    // 状态标签 + 耗时（token 空值行无该位，收纳契约见 mobileCompact 测试）
    expect(shrinkFixed.length).toBeGreaterThanOrEqual(2)
    // token 空值不渲染「--」占位（P1-3）
    expect(document.querySelector('[data-testid="pipeline-entry-token"]')).toBeNull()
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

  it('②任务名称按钮 aria-expanded 提示展开态；不用 chevron 提示可展开（方案 B）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    const btn = () => document.querySelector('[data-testid="pipeline-entry-title"]') as HTMLElement
    expect(btn().tagName).toBe('BUTTON')
    expect(btn().getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(btn())
    await waitFor(() => expect(btn().getAttribute('aria-expanded')).toBe('true'))
    // 名称按钮内无 chevron（可展开语义由 hover 下划线表达；树 chevron 只管子级展开）
    expect(btn().querySelector('svg')).toBeNull()
  })

  it('③行级「只看本会话」按钮已下线（其后操作按钮不再被挤出视野）', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    expect(screen.queryAllByLabelText('只看此会话')).toHaveLength(0)
  })

  it('③操作按钮区独立于信息区且 shrink-0；常驻「发消息」可达', async () => {
    seedLongRow()
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText(LONG_TITLE)

    const actions = document.querySelector('[data-testid="pipeline-entry-actions"]')!
    expect(actions.className).toContain('shrink-0')
    // 操作按钮区不在「辅助信息区」内（辅助区可换行自裁剪，标签不推挤操作区）
    expect(actions.closest('[data-testid="pipeline-entry-meta"]')).toBeNull()
    // 常驻 = 发消息（打开对话主入口）+ ⋮（低频菜单），方案 B 行形态
    expect(actions.querySelector('button[aria-label="发消息"]')).not.toBeNull()
    expect(actions.querySelector('[aria-label="更多操作"]')).not.toBeNull()
  })

  it('①区分度输入：无 agent 配置时图标位仍渲染（缺省图标兜底，不留空）', async () => {
    // 对照组：任务无 agent_name → 名字落「未知 agent」，图标走缺省兜底
    // （agentIcon.ts 契约：未配置/未知值不留空）
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

    const icon = document.querySelector('[data-testid="pipeline-entry-agent-icon"]')
    expect(icon).not.toBeNull()
    expect(icon!.getAttribute('aria-label')).toBe('未知 agent')
    expect(icon!.getAttribute('title')).toBe('未知 agent')
    expect(icon!.querySelector('svg')).not.toBeNull()
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

    it('①状态图标合一：类型+状态合一纯展示（data-status-kind 取件键），不可点', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      const icon = row.querySelector('[data-testid="pipeline-entry-status-icon"]')
      expect(icon).not.toBeNull()
      // 类型+状态合一取件键：运行中任务 → running（单一真值 entryStatusIconKind）
      expect(icon!.getAttribute('data-status-kind')).toBe('running')
      // 纯展示：span 非 button（方案 B：状态图标不可点）
      expect(icon!.tagName).toBe('SPAN')
      // 状态文字标签保留在辅助信息簇（同一真值两处呈现：图标 + 文字）
      expect(row.querySelector('[data-testid="pipeline-entry-status"]')!.textContent).toBe('运行中')
    })

    it('④任务区不重复：任务状态文字下线（任务行前已有类型标识）', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      expect(row.textContent).not.toContain('任务:')
    })

    it('②③模式徽标归辅助信息簇：在 pipeline-entry-meta 内（旧次要区已退役）', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      // 旧 common/mode 双分区容器退役（方案 B：行形态单区）
      expect(row.querySelector('[data-testid="pipeline-entry-mode"]')).toBeNull()
      expect(row.querySelector('[data-testid="pipeline-entry-common"]')).toBeNull()
      // 模式徽标在辅助信息簇内
      const meta = row.querySelector('[data-testid="pipeline-entry-meta"]')
      expect(meta).not.toBeNull()
      expect(meta!.querySelector(`[data-testid="mode-badge-${MODE}"]`)).not.toBeNull()
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

    it('③agent 图标单份：名字短名进 tooltip（来源与全路径不进行内可见文本）', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      const icons = row.querySelectorAll('[data-testid="pipeline-entry-agent-icon"]')
      expect(icons).toHaveLength(1)
      expect(icons[0].getAttribute('title')).toBe('programming_orchestrator_agent_v2')
      // 短名与 mode_ 前缀全路径都不进行内可见文本（名字只在 tooltip/详情）
      expect(row.textContent).not.toContain('programming_orchestrator_agent_v2')
      expect(row.textContent).not.toContain('mode_coding/')
    })

    it('行形态顺序恒定：状态图标 → agent 图标 → 任务名称 → 辅助簇 → 操作区（方案 B）', async () => {
      seedModeRow()
      renderPmAllStatuses(<PipelineManagerWidget />)
      const title = (await screen.findAllByText(LONG_TITLE))[0]
      const row = entryRow(title)
      // 主行容器直接子元素的 testid 序列即行形态（树 chevron 居首：有子级时是
      // 按钮、无子级时为占位 span，本行无子级；时长/token 在辅助簇内、
      // token 空值不渲染占位——真值在场时恒在 duration 之后）
      const order = Array.from(row.firstElementChild!.children)
        .map((el) => el.getAttribute('data-testid'))
        .filter(Boolean)
      expect(order).toEqual([
        'pipeline-entry-status-icon',
        'pipeline-entry-agent-icon',
        'pipeline-entry-title',
        'pipeline-entry-meta',
        'pipeline-entry-actions',
      ])
    })

    it('模式徽标只在模式在场时渲染（无模式行无徽标、有模式行有——对照双组输入）', async () => {
      // 对照组：无 mode state 的任务行（agent 图标走缺省兜底，但不产生模式徽标）
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
      expect(plain.querySelector('[data-testid^="mode-badge-"]')).toBeNull()
      // 有模式的行才有徽标
      const modeRow = (await screen.findAllByText(LONG_TITLE))[0].closest(
        'div[data-entry-key]',
      ) as HTMLElement
      expect(modeRow.querySelector('[data-testid^="mode-badge-"]')).not.toBeNull()
    })
  })
})
