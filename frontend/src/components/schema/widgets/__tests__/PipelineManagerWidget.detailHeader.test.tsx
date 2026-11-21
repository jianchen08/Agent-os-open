// @feature FP-T12 前端适配 | @vision V6 可即用 | @ci frontend-test
/**
 * 管道详情标题测试（详情面板顶部标题：管道名 + 运行状态）
 *
 * 缺陷：展开管道详情后，详情面板顶部只有「管道 ID / 运行 ID / 归属 …」键值行，
 * 没有任何标题——用户看不出当前详情属于哪个管道、处于什么状态（管道名与状态
 * 只在上一行的行内标签里，详情独立读时缺位）。
 *
 * 契约（本文件锁死，勿回退）：
 * ① 详情标题 = 管道名（全量文本，不截断数据）+ 运行状态文字标签；
 * ② 状态文案随运行态走（三个运行态的区分度输入证明非写死文案——同一断言结构
 *   在三组输入下列表化展开）；
 * ③ 长标题不撑破布局：标题复用工作区标签栏的渐变遮蔽收边件（TabLabel →
 *   useFadeOverflow），状态标签 shrink-0 不被压缩；
 * ④ 项目登记行无运行态：标题照显，状态标签不渲染（不伪造状态）；
 * ⑤ 详情既有区块（键值行）保持原状——标题只增量加在顶部。
 */
import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
// eslint-disable-next-line import-x/order -- pmTestUtils 须先于被测组件求值（vi.mock 工厂时序约束，见 pmTestUtils 头注；禁用排序自动修防回归）
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'

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

/** 超长管道标题（区分度输入：长到在窄详情面板里必然溢出） */
const LONG_TITLE = '超长管道标题'.repeat(10)

/** 播种一条任务管道（runs + 任务行同名同状态，标题/状态按入参区分） */
function seedTaskRun(title: string, status: string) {
  pmSeed.runs.r1 = {
    pipeline_id: 'pipe-1',
    run_id: 'run-1',
    thread_id: 'th-1',
    title,
    status,
    started_at: '2026-11-01T00:00:00Z',
    ...(status === 'running' ? {} : { ended_at: '2026-11-01T01:00:00Z' }),
  }
  pmSeed.tasks.push({
    id: 't-1',
    title,
    status,
    pipeline_run_id: 'pipe-1',
  })
}

/** 渲染并展开首行详情（详情区块：键值行「管道 ID」在场即已展开） */
async function openDetail(title: string) {
  renderPmAllStatuses(<PipelineManagerWidget />)
  const row = (await screen.findAllByText(title))[0]
  fireEvent.click(row)
  await screen.findByText('管道 ID')
}

/** 详情标题容器（详情面板顶部标题区） */
function detailHeader(): HTMLElement | null {
  return document.querySelector('[data-testid="pipeline-detail-header"]')
}

/** 详情标题里的状态标签 */
function detailStatus(): HTMLElement | null {
  return document.querySelector('[data-testid="pipeline-detail-status"]')
}

describe('管道详情标题（详情面板顶部：管道名 + 运行状态）', () => {
  beforeEach(() => {
    resetPmContainers()
  })

  it('①详情顶部有标题：管道名全文 + 运行状态标签', async () => {
    seedTaskRun('修复登录缺陷', 'running')
    await openDetail('修复登录缺陷')

    const header = detailHeader()
    expect(header).not.toBeNull()
    // 标题在主位（弹性可压缩的容器），状态标签在其后
    expect(header!.className).toContain('min-w-0')
    expect(header!.textContent).toContain('修复登录缺陷')
    // 状态标签 = 运行态文案（单一真值源 pipelineStatusVisuals）
    expect(detailStatus()!.textContent).toBe('运行中')
  })

  it('②状态随运行态变化：running/completed/failed 三组输入各显对应文案', async () => {
    // 三组有区分度输入：状态不同、标题不同、结束时间有无不同
    const cases: Array<{ title: string; status: string; expectLabel: string }> = [
      { title: '甲管道', status: 'running', expectLabel: '运行中' },
      { title: '乙管道', status: 'completed', expectLabel: '已完成' },
      { title: '丙管道', status: 'failed', expectLabel: '失败' },
    ]
    for (const c of cases) {
      resetPmContainers()
      seedTaskRun(c.title, c.status)
      const ret = renderPmAllStatuses(<PipelineManagerWidget />)
      const row = (await screen.findAllByText(c.title))[0]
      fireEvent.click(row)
      await screen.findByText('管道 ID')
      expect(detailHeader()!.textContent).toContain(c.title)
      expect(detailStatus()!.textContent).toBe(c.expectLabel)
      ret.unmount()
    }
  })

  it('③长标题不撑破布局：复用标签栏渐变遮蔽收边 + 状态标签不压缩', async () => {
    seedTaskRun(LONG_TITLE, 'running')
    await openDetail(LONG_TITLE)

    const header = detailHeader()!
    // 标题收边件 = 工作区/对话标签栏同一件（TabLabel → useFadeOverflow）
    const titleEl = header.querySelector('[data-testid="tab-label"]')
    expect(titleEl).not.toBeNull()
    expect(titleEl!.textContent).toBe(LONG_TITLE)
    expect(titleEl!.className).toContain('overflow-hidden')
    expect(titleEl!.className).toContain('whitespace-nowrap')
    // 状态标签固定不挤（shrink-0）
    expect(detailStatus()!.className).toContain('shrink-0')
  })

  it('④项目登记行无运行态：标题照显、状态标签不渲染（不伪造状态）', async () => {
    pmSeed.fetchProjects.mockResolvedValue({
      items: [
        { id: 'proj-1', goal: '挂靠项目甲', timestamps: { createdAt: '2026-11-01T00:00:00Z' } },
      ],
    })
    pmSeed.projects.push({
      id: 'proj-1',
      goal: '挂靠项目甲',
      timestamps: { createdAt: '2026-11-01T00:00:00Z' },
    })
    const ret = renderPmAllStatuses(<PipelineManagerWidget />)
    // 方案 B 唯一详情入口 = 点任务名称（行内手风琴）——项目行同走标题按钮
    // 【疑似生产 bug，报告待裁】当前 TreeParts 对 project 行不给详情入口：
    // 标题按钮 onClick 对 project 直接 return（L635）、行容器 onClick 亦排除
    // project（L562），旧「切换详细信息」按钮已随方案 B 退役——项目 ID/路径/
    // 登记时间 详情卡无任何可达入口（EntryDetail 项目分支成死代码、标题
    // tooltip 仍承诺「点击展开」）。本用例按方案 B 契约书写，红 = bug 实证。
    fireEvent.click((await screen.findAllByText('挂靠项目甲'))[0])
    await screen.findByText('项目 ID')

    const header = detailHeader()
    expect(header).not.toBeNull()
    expect(header!.textContent).toContain('挂靠项目甲')
    expect(detailStatus()).toBeNull()
    ret.unmount()
  })

  it('⑤详情既有键值行不被标题改动：管道 ID/运行 ID/归属 仍在', async () => {
    seedTaskRun('既有区块不动', 'running')
    await openDetail('既有区块不动')

    for (const label of ['管道 ID', '运行 ID', '归属', '开始']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    // 标题在键值行之前（顶部），不插入行中间
    const header = detailHeader()!
    const firstRow = screen.getByText('管道 ID').closest('div')!
    expect(header.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
