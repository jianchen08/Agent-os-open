// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * PipelineManagerWidget 覆盖缺口补充测试（与既有 PipelineManagerWidget.test 互补，不重复）：
 * - 工具栏：视图切换 / 类型筛选（全部·任务·会话）/ 状态筛选（含 paused→suspended 别名）
 * - 树视图：执行中/最近完成分组折叠、会话主管道（pipelineIds）子级缩进、
 *   项目挂靠任务缩进、组头计数
 * - 行内操作（树+列表双视图）：复制 ID（clipboard 成功/拒绝/缺失）、暂停/恢复/取消、
 *   操作失败 console 告警、打开工作空间（含既有 Tab 仅激活）、打开对话按钮
 * - 打开对话分流：归属会话直跳、导航失败回退 setActiveSession、回退再失败通知、
 *   既有同管道 Tab 仅切换、孤儿条目建子标签的 6 态状态映射
 * - 详情面板：结束/错误/已结束=否/Token 汇总/Token 实时/进度条（含 >100 clamp）
 * - 列表视图空态、项目行点击不开对话、删除弹窗 Esc/取消关闭
 * - 任务管道 ID 双取（metadata.pipelineRunId）、实时 token 优先、total_tokens 三形态
 *
 * 行为对齐（2026-09-23，产品行为有意变更后测试跟进，禁止回退产品代码）：
 * - 面板状态筛选缺省「运行中」（5971fdaa1，用户裁定 2026-09-21：面板默认只看
 *   运行中）——需全量条目可见的用例经 renderPmAllStatuses 显式切「全部」；
 * - 项目登记行去管道语义（a2585be70：登记行无运行态，不进执行中分组/状态
 *   筛选/耗时 ticker）——登记行相关断言同样只在「全部」视图可见；
 * - 会话主管道身份 = session.pipelineIds[0]（61e54023e）——树缩进/导航用例
 *   种子按该语义播种（主管道挂会话 pipelineIds[0]）。
 */

import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
// 顺序约束：先于被测组件 import——vi.mock 工厂体在被测组件初始化时执行，
// 届时本模块必须已求值（见 pmTestUtils 头注）
// eslint-disable-next-line import-x/order -- pmTestUtils 须先于被测组件求值（vi.mock 工厂时序约束，见 pmTestUtils 头注；禁用排序自动修防回归）
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { AgentTab } from '@/types/task'

// 交互全量文件：行内操作/删除弹窗/打开工作空间/通知回退全部走真实 handler 路径，
// 全部端点与句柄经 pmSeed 可断言；sessionStore 未进入面板渲染树，无需 mock。
vi.mock('@/services/api/tasks', () => pmMod.tasksApi())
vi.mock('@/services/api/client', () => pmMod.apiClient())
vi.mock('@/services/pipelineNavigator', () => pmMod.pipelineNavigator())
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => pmMod.pipelineRunsQuery())
vi.mock('@/hooks/queries/useAllTasksQuery', () => pmMod.allTasksQuery())
vi.mock('@/hooks/queries/useLongTermTasksQuery', () => pmMod.longTermTasksQuery())
vi.mock('@/hooks/queries/useSessionsQuery', () => pmMod.sessionsQuery())
vi.mock('@/stores/contextUsageStore', () => pmMod.contextUsageStore())
vi.mock('@/stores/sessionListStore', () => pmMod.sessionListStore())

// ────────────────────────── 公共基建 ──────────────────────────

/** 标准播种：2 任务 + 1 挂起任务 + 1 metadata 双取任务 + 2 会话管道 + 1 孤儿管道 */
function seedStandard() {
  pmSeed.runs.r1 = {
    pipeline_id: 'pipe-running', run_id: 'run-1', thread_id: 'th-1',
    status: 'running', started_at: '2026-09-01T00:00:00Z',
  }
  pmSeed.runs.r2 = {
    pipeline_id: 'pipe-done', run_id: 'run-2', thread_id: 'th-1', status: 'completed',
    started_at: '2026-09-01T00:01:00Z', ended_at: '2026-09-01T00:02:00Z',
    total_tokens: { input: 10, output: 20, total: 51234 },
  }
  pmSeed.runs.r3 = {
    pipeline_id: 'pipe-orph', run_id: 'run-3', status: 'suspended',
    started_at: '2026-09-01T00:03:00Z',
  }
  pmSeed.tasks.push(
    { id: 't-run', title: '运行任务', status: 'running', pipeline_run_id: 'pipe-running', agent_name: 'general_agent' },
    { id: 't-susp', title: '挂起任务', status: 'suspended', pipeline_run_id: 'pipe-susp', agent_name: 'general_agent', timestamps: { startedAt: '2026-09-01T00:04:00Z' } },
    { id: 't-meta', title: 'meta任务', status: 'completed', metadata: { pipelineRunId: 'pipe-meta' }, timestamps: { startedAt: '2026-09-01T00:05:00Z' } },
  )
}

/** meta 任务替换为带工作空间坐标的载荷（打开工作空间族用例共用） */
function seedMetaWorkspaceTask() {
  pmSeed.tasks[2] = {
    ...pmSeed.tasks[2],
    metadata: { pipelineRunId: 'pipe-meta', ws_meta: { path: 'D:/ws/meta' } },
  }
}

/** 注入可写 clipboard 并渲染面板（复制 ID 用例共用；返回 writeText 句柄）。
 *  以「全部」状态渲染：meta任务等终态条目须在行内才可触发行内操作 */
function renderWithClipboard() {
  const writeText = vi.fn<(t: string) => Promise<void>>().mockResolvedValue(undefined)
  setClipboard({ writeText })
  renderPmAllStatuses(<PipelineManagerWidget />)
  return writeText
}

function resetStores() {
  useAgentTabStore.setState({ tabs: [], activeTabId: null })
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  useNotificationStore.setState({ notifications: [] })
}

function setClipboard(value: { writeText: (t: string) => Promise<void> } | undefined) {
  Object.defineProperty(window.navigator, 'clipboard', { value, configurable: true })
}

/** 定位含指定条目名的行内按钮（title 或 aria-label 精确匹配；树行=div，列表行=tr） */
function rowButton(entryName: string, selector: string): HTMLElement {
  const el = screen.getAllByText(entryName)[0]
  const row = (el.closest('tr') ?? el.closest('div')) as HTMLElement
  const btn = row.querySelector(selector) as HTMLElement | null
  expect(btn, `row button ${selector} for ${entryName}`).toBeTruthy()
  return btn as HTMLElement
}

/** 树视图默认收起：先展开父行（首个 chevron）再取子行元素 */
async function expandTreeChild(parentName: string, childName: string) {
  const parentRow = ((await screen.findAllByText(parentName))[0]).closest('div') as HTMLElement
  fireEvent.click(parentRow.querySelector('button') as HTMLElement)
  return (await screen.findAllByText(childName))[0]
}

function makeTab(partial: Partial<AgentTab> & { id: string }): AgentTab {
  return {
    agentId: partial.id,
    agentName: partial.id,
    agentLevel: 2,
    path: [],
    status: 'running',
    hasUnread: false,
    canClose: true,
    ...partial,
  }
}

beforeEach(() => {
  resetPmContainers()
  resetStores()
  setClipboard(undefined)
  pmSeed.navigateToPipeline.mockReset().mockResolvedValue(true)
  pmSeed.readSessions.mockReset().mockReturnValue([])
  pmSeed.ensureSessionsLoaded.mockReset().mockResolvedValue([])
  pmSeed.setActiveSession.mockReset().mockResolvedValue(undefined)
  pmSeed.pauseTask.mockReset().mockResolvedValue(undefined)
  pmSeed.resumeTask.mockReset().mockResolvedValue(undefined)
  pmSeed.cancelTask.mockReset().mockResolvedValue(undefined)
  pmSeed.workspaceOpen.mockReset().mockResolvedValue({ data: { success: true } })
  pmSeed.fetchProjects.mockReset().mockResolvedValue({ items: pmSeed.projects })
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ────────────────────────── 工具栏筛选 ──────────────────────────

describe('PipelineManagerWidget 工具栏筛选', () => {
  it('类型筛选：任务/会话/全部 三态互斥切换（列表视图断言）', async () => {
    seedStandard()
    // 断言含终态任务（meta任务）：状态筛选显式切「全部」（缺省只看运行中，5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')
    fireEvent.click(screen.getByTitle('列表视图'))

    // 任务：会话条目全部隐藏
    fireEvent.click(screen.getByRole('button', { name: '任务' }))
    expect(screen.queryByText('会话 th-1')).toBeNull()
    expect(screen.queryByText('pipe-orph')).toBeNull()
    expect(screen.getByText('运行任务')).toBeInTheDocument()
    expect(screen.getByText('挂起任务')).toBeInTheDocument()
    expect(screen.getByText('meta任务')).toBeInTheDocument()

    // 会话：只留会话条目
    fireEvent.click(screen.getByRole('button', { name: '会话' }))
    expect(screen.queryByText('运行任务')).toBeNull()
    expect(screen.getByText('会话 th-1')).toBeInTheDocument()
    expect(screen.getByText('pipe-orph')).toBeInTheDocument()

    // 全部：恢复（工具栏有两个「全部」：类型筛选在前、状态筛选在后）
    fireEvent.click(screen.getAllByRole('button', { name: '全部' })[0])
    expect(screen.getByText('运行任务')).toBeInTheDocument()
    expect(screen.getByText('会话 th-1')).toBeInTheDocument()

    // 树 ⇄ 列表双向切换：回到树视图后分组表头回归
    fireEvent.click(screen.getByTitle('树视图（会话分组）'))
    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
  })

  it('状态筛选：已完成按运行态过滤；已暂停别名命中 suspended', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')
    fireEvent.click(screen.getByTitle('列表视图'))

    fireEvent.click(screen.getByRole('button', { name: '已完成' }))
    expect(screen.getByText('meta任务')).toBeInTheDocument()
    expect(screen.getByText('会话 th-1')).toBeInTheDocument()
    expect(screen.queryByText('运行任务')).toBeNull()
    expect(screen.queryByText('挂起任务')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '已暂停' }))
    expect(screen.getByText('挂起任务')).toBeInTheDocument()
    expect(screen.getByText('pipe-orph')).toBeInTheDocument()
    expect(screen.queryByText('meta任务')).toBeNull()
  })
})

// ────────────────────────── 树视图分组与层级 ──────────────────────────

describe('PipelineManagerWidget 树视图', () => {
  it('执行中/最近完成分组渲染，组头折叠收起条目再展开恢复', async () => {
    seedStandard()
    // 「最近完成」组含终态条目：状态筛选显式切「全部」（缺省只看运行中，5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)

    expect(await screen.findByText(/执行中的管道/)).toBeInTheDocument()
    expect(screen.getByText(/最近完成/)).toBeInTheDocument()
    expect(screen.getByText('运行任务')).toBeInTheDocument()
    expect(screen.getByText('meta任务')).toBeInTheDocument()

    // 折叠执行中分组：其下条目收起
    fireEvent.click(screen.getByText(/执行中的管道/))
    expect(screen.queryByText('运行任务')).toBeNull()
    expect(screen.queryByText('pipe-orph')).toBeNull()
    // 最近完成分组不受影响
    expect(screen.getByText('meta任务')).toBeInTheDocument()

    // 再展开恢复
    fireEvent.click(screen.getByText(/执行中的管道/))
    expect(screen.getByText('运行任务')).toBeInTheDocument()
  })

  it('会话主管道（session.pipelineIds）优先：其余管道缩进挂其下', async () => {
    seedStandard()
    // 主管道身份 = session.pipelineIds[0]（61e54023e）；子管道 pipe-done 为终态，
    // 须「全部」视图可见（缺省只看运行中，5971fdaa1）
    pmSeed.sessions.push({ id: 'th-1', title: '主会话', pipelineIds: ['pipe-running'] })
    renderPmAllStatuses(<PipelineManagerWidget />)

    // 主管道条目行（任务一对一绑定 → 行名 = 任务名）展开子级
    fireEvent.click(
      (await screen.findAllByText('运行任务'))[0].closest('div')?.querySelector('button') as HTMLElement,
    )
    const childRow = (await screen.findAllByText('主会话'))[0].closest('div')
    expect(childRow?.getAttribute('style') ?? '').toMatch(/padding-left:\s*2[48]px/)
  })

  it('项目挂靠任务（metadata.parent_project_id）挂项目分组节点下', async () => {
    // 项目登记行已去管道语义（a2585be70：无运行态、不进状态筛选）——「全部」视图
    // 才保留登记行/分组节点；挂靠任务无 runs 证据落 unknown 视图态（BUG-21）
    pmSeed.projects.push({ id: 'proj-9', goal: '挂靠项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    pmSeed.tasks.push({
      id: 't-p', title: '挂靠任务', status: 'running', pipeline_run_id: 'pipe-p',
      metadata: { parent_project_id: 'proj-9' },
    })
    renderPmAllStatuses(<PipelineManagerWidget />)

    fireEvent.click(
      (await screen.findAllByText('挂靠项目'))[0].closest('div')?.querySelector('button') as HTMLElement,
    )
    const taskRow = (await screen.findAllByText('挂靠任务'))[0].closest('div')
    expect(taskRow?.getAttribute('style') ?? '').toMatch(/padding-left:\s*2[48]px/)
  })
})

// ────────────────────────── 行内操作 ──────────────────────────

describe('PipelineManagerWidget 行内操作', () => {
  it('复制管道 ID：clipboard 写入 pipeline_id；拒绝与缺失均静默', async () => {
    seedStandard()
    const writeText = renderWithClipboard()
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[title="复制管道 ID"]'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('pipe-running'))

    // clipboard 拒绝 → 静默
    writeText.mockRejectedValueOnce(new Error('denied'))
    fireEvent.click(rowButton('运行任务', 'button[title="复制管道 ID"]'))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
    expect(console.error).not.toHaveBeenCalled()

    // clipboard 缺失 → 静默
    setClipboard(undefined)
    fireEvent.click(rowButton('运行任务', 'button[title="复制管道 ID"]'))
    await waitFor(() => expect(console.error).not.toHaveBeenCalled())
  })

  it('暂停/取消运行中任务 → 对应端点 + 任务列表失效刷新；失败只告警不崩', async () => {
    seedStandard()
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[title="暂停任务"]'))
    await waitFor(() => expect(pmSeed.pauseTask).toHaveBeenCalledWith('t-run'))
    expect(pmSeed.invalidateLongTermTasks).toHaveBeenCalled()

    fireEvent.click(rowButton('运行任务', 'button[title="取消任务"]'))
    await waitFor(() => expect(pmSeed.cancelTask).toHaveBeenCalledWith('t-run'))
    expect(pmSeed.invalidateLongTermTasks).toHaveBeenCalled()

    // 操作失败 → console.error 兜底，不崩
    pmSeed.pauseTask.mockRejectedValueOnce(new Error('pause boom'))
    fireEvent.click(rowButton('运行任务', 'button[title="暂停任务"]'))
    await waitFor(() => expect(console.error).toHaveBeenCalled())
  })

  it('恢复挂起任务 → resume 端点', async () => {
    seedStandard()
    // 挂起条目不在缺省「运行中」视图：状态筛选显式切「全部」（5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('挂起任务')

    fireEvent.click(rowButton('挂起任务', 'button[title="恢复任务"]'))
    await waitFor(() => expect(pmSeed.resumeTask).toHaveBeenCalledWith('t-susp'))
    expect(pmSeed.invalidateLongTermTasks).toHaveBeenCalled()
  })

  it('打开工作空间：任务条目用 taskId 开 ws-tree Tab；已有 Tab 仅激活', async () => {
    seedStandard()
    seedMetaWorkspaceTask()
    useLayoutModeStore.setState({
      workspaceTabs: [
        { id: 'ws-tree-t-meta', title: 'meta任务', moduleId: '__dynamic__', component: 'file_tree', dataSource: 'workspace://t-meta', isActive: false, isPinned: false },
      ],
    })
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('meta任务')

    fireEvent.click(rowButton('meta任务', 'button[title="打开工作空间: D:/ws/meta"]'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0]).toMatchObject({ id: 'ws-tree-t-meta', isActive: true })
  })

  it('打开对话按钮（树视图）：归属会话条目走导航器', async () => {
    seedStandard()
    pmSeed.readSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切「全部」（5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    // 树默认收起：pipe-done 挂 pipe-running 下，先展开父行
    await expandTreeChild('运行任务', '会话 th-1')

    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))

    await waitFor(() =>
      expect(pmSeed.navigateToPipeline).toHaveBeenCalledWith(
        'pipe-done',
        expect.objectContaining({ taskId: undefined, agentLevel: 2 }),
      ),
    )
    // 导航成功 → 不回退切换会话
    expect(pmSeed.setActiveSession).not.toHaveBeenCalled()
  })

  it('导航返回 false → 回退切换归属会话；回退失败 → 用户可见通知', async () => {
    seedStandard()
    pmSeed.readSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    pmSeed.navigateToPipeline.mockReset().mockResolvedValue(false)
    const addNotification = vi
      .spyOn(useNotificationStore.getState(), 'addNotification')
      .mockImplementation(() => {})
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切「全部」（5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '会话 th-1')

    // 回退失败版本：先验证通知分支
    pmSeed.setActiveSession.mockReset().mockRejectedValue(new Error('switch boom'))
    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))
    await waitFor(() =>
      expect(addNotification).toHaveBeenCalledWith(
        expect.objectContaining({
          title: '无法打开对话',
          message: expect.stringContaining('pipe-done'),
        }),
      ),
    )

    // 回退成功版本：setActiveSession 收到归属会话 id
    pmSeed.setActiveSession.mockReset().mockResolvedValue(undefined)
    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))
    await waitFor(() => expect(pmSeed.setActiveSession).toHaveBeenCalledWith('th-1'))
    addNotification.mockRestore()
  })

  it('导航器拒绝（throw）→ 落回退切换会话', async () => {
    seedStandard()
    pmSeed.readSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    pmSeed.navigateToPipeline.mockReset().mockRejectedValue(new Error('nav down'))
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切「全部」（5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '会话 th-1')

    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))

    await waitFor(() => expect(pmSeed.setActiveSession).toHaveBeenCalledWith('th-1'))
  })

  it('已有同管道 Tab 的孤儿条目 → 仅切换既有 Tab，不新建', async () => {
    seedStandard()
    useAgentTabStore.setState({
      tabs: [makeTab({ id: 'tab-x', pipelineRunId: 'pipe-running' })],
      activeTabId: null,
    })
    renderWithProviders(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(screen.getAllByText('运行任务')[0])

    await waitFor(() => expect(useAgentTabStore.getState().activeTabId).toBe('tab-x'))
    expect(useAgentTabStore.getState().tabs).toHaveLength(1)
  })

  it('孤儿条目点击按运行状态映射建子标签（6 态）', async () => {
    const cases = [
      ['running', 'running'],
      ['completed', 'completed'],
      ['failed', 'failed'],
      ['suspended', 'waiting_input'],
      ['cancelled', 'waiting_input'],
      ['unknown', 'unknown'],
    ] as const
    for (const [runStatus, tabStatus] of cases) {
      resetPmContainers()
      resetStores()
      const pid = `st-${runStatus}`
      pmSeed.runs.a = {
        pipeline_id: pid, run_id: `run-${pid}`, status: runStatus,
        started_at: '2026-09-01T00:00:00Z',
      }
      // 非 running 态条目不在缺省「运行中」视图：每轮显式切「全部」（5971fdaa1）；
      // 上一轮先 unmount——renderPmAllStatuses 按序取第二个「全部」按钮，多面板
      // 同存会点中旧面板的筛选
      const { unmount } = renderPmAllStatuses(<PipelineManagerWidget />)
      fireEvent.click((await screen.findAllByText(pid))[0])

      await waitFor(() =>
        expect(
          useAgentTabStore.getState().tabs.some(
            (t) => t.pipelineRunId === pid && t.status === tabStatus,
          ),
        ).toBe(true),
      )
      unmount()
      resetStores()
    }
  })
})

// ────────────────────────── 详情面板 ──────────────────────────

describe('PipelineManagerWidget 详情面板', () => {
  it('结束时间 / Token 汇总 / Token 实时 行完整呈现', async () => {
    seedStandard()
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切「全部」（5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '会话 th-1')
    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="切换详细信息"]'))

    expect(await screen.findByText('结束')).toBeInTheDocument()
    expect(screen.getByText('input=10 · output=20 · total=51234')).toBeInTheDocument()
    // token 值两处呈现：条目行 token 列 + 详情「Token 实时」行
    expect(screen.getByText('Token 实时')).toBeInTheDocument()
    expect(screen.getAllByText('51,234').length).toBeGreaterThanOrEqual(2)

    // 再点一次详细信息 → 收起详情（toggle 对称：树操作不动详情，详情自身可关）
    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="切换详细信息"]'))
    await waitFor(() => expect(screen.queryByText('Token 实时')).toBeNull())
  })

  it('state 真值行：错误、已结束=是', async () => {
    seedStandard()
    pmSeed.states.s1 = {
      pipeline_id: 'pipe-running',
      state: { raw_error: 'boom 消息', ended: true },
    }
    renderWithProviders(<PipelineManagerWidget />)
    fireEvent.click(rowButton('运行任务', 'button[aria-label="切换详细信息"]'))

    expect(await screen.findByText('错误')).toBeInTheDocument()
    expect(screen.getByText('boom 消息')).toBeInTheDocument()
    expect(screen.getByText('已结束')).toBeInTheDocument()
    expect(screen.getByText('是')).toBeInTheDocument()
  })

  it('任务进度条：正常值按百分比渲染，超界值 clamp 到 100%', async () => {
    seedStandard()
    pmSeed.tasks[0] = { ...pmSeed.tasks[0], progress: { progressPercent: 42 } }
    pmSeed.tasks[2] = { ...pmSeed.tasks[2], progress: { progressPercent: 150 } }
    // meta任务（终态）不在缺省「运行中」视图：切「全部」（5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行任务')

    fireEvent.click(rowButton('运行任务', 'button[aria-label="切换详细信息"]'))
    expect(await screen.findByText('42%')).toBeInTheDocument()
    // 进度条 = 百分比文案的兄弟容器内的满高条
    const barWrap = screen.getByText('42%').previousElementSibling as HTMLElement
    expect((barWrap.firstElementChild as HTMLElement).style.width).toBe('42%')

    fireEvent.click(rowButton('meta任务', 'button[aria-label="切换详细信息"]'))
    expect(await screen.findByText('150%')).toBeInTheDocument()
    const wrap150 = screen.getByText('150%').previousElementSibling as HTMLElement
    expect((wrap150.firstElementChild as HTMLElement).style.width).toBe('100%')
  })

  it.each([
    [{ total: 63456 }, '63,456'],
    [{ total_tokens: 74567 }, '74,567'],
    [{ output: 85678 }, '85,678'],
  ])('total_tokens 形态 %s → token 列展示 %s', async (tokens, expected) => {
    seedStandard()
    pmSeed.runs.r2.total_tokens = tokens
    // 待展开子行（pipe-done，终态）不在缺省「运行中」视图：切「全部」（5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await expandTreeChild('运行任务', '会话 th-1')
    expect((await screen.findAllByText(expected)).length).toBeGreaterThanOrEqual(1)
  })

  it('实时 usage 优先于快照 total_tokens', async () => {
    seedStandard()
    pmSeed.usage.usageByPipeline['pipe-running'] = {
      promptTokens: 1, completionTokens: 2, totalTokens: 999999,
    }
    renderWithProviders(<PipelineManagerWidget />)
    expect((await screen.findAllByText('999,999')).length).toBeGreaterThanOrEqual(1)
  })
})

// ────────────────────────── 列表视图 ──────────────────────────

describe('PipelineManagerWidget 列表视图', () => {
  it('空数据：树/列表双视图空态占位', async () => {
    renderWithProviders(<PipelineManagerWidget />)
    expect((await screen.findAllByText('暂无管道运行记录')).length).toBeGreaterThanOrEqual(1)

    fireEvent.click(screen.getByTitle('列表视图'))
    expect(screen.getByText('暂无管道运行记录')).toBeInTheDocument()
  })

  it('行内操作齐备：打开对话/暂停/取消/恢复/复制/工作空间', async () => {
    seedStandard()
    seedMetaWorkspaceTask()
    pmSeed.readSessions.mockReturnValue([{ id: 'th-1', title: '会话A' }])
    const writeText = renderWithClipboard()
    await screen.findAllByText('运行任务')
    fireEvent.click(screen.getByTitle('列表视图'))

    fireEvent.click(rowButton('会话 th-1', 'button[aria-label="打开对话"]'))
    await waitFor(() => expect(pmSeed.navigateToPipeline).toHaveBeenCalledWith('pipe-done', expect.anything()))

    fireEvent.click(rowButton('运行任务', 'button[aria-label="复制管道 ID"]'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('pipe-running'))

    fireEvent.click(rowButton('运行任务', 'button[title="暂停任务"]'))
    await waitFor(() => expect(pmSeed.pauseTask).toHaveBeenCalledWith('t-run'))

    fireEvent.click(rowButton('运行任务', 'button[title="取消任务"]'))
    await waitFor(() => expect(pmSeed.cancelTask).toHaveBeenCalledWith('t-run'))

    fireEvent.click(rowButton('挂起任务', 'button[title="恢复任务"]'))
    await waitFor(() => expect(pmSeed.resumeTask).toHaveBeenCalledWith('t-susp'))

    fireEvent.click(rowButton('meta任务', 'button[title="打开工作空间: D:/ws/meta"]'))
    await waitFor(() =>
      expect(
        useLayoutModeStore.getState().workspaceTabs.some((t) => t.dataSource === 'workspace://t-meta'),
      ).toBe(true),
    )
  })

  it('项目行点击不开对话（对照：普通条目行点击会打开对话）', async () => {
    seedStandard()
    pmSeed.projects.push({ id: 'proj-l', goal: '列表项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    // 项目登记行无运行态（a2585be70）：不进状态筛选，仅「全部」视图可见；
    // 对照组 pipe-orph（挂起）同理（缺省只看运行中，5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findByText('列表项目')
    fireEvent.click(screen.getByTitle('列表视图'))

    // 项目行：点击无动作
    fireEvent.click(screen.getAllByText('列表项目')[0])
    await waitFor(() => expect(useAgentTabStore.getState().tabs).toHaveLength(0))

    // 对照组：普通条目行点击 → 打开对话（孤儿直建子标签）
    fireEvent.click(screen.getAllByText('pipe-orph')[0])
    await waitFor(() =>
      expect(
        useAgentTabStore.getState().tabs.some((t) => t.pipelineRunId === 'pipe-orph'),
      ).toBe(true),
    )
  })
})

// ────────────────────────── 删除弹窗关闭 ──────────────────────────

describe('PipelineManagerWidget 项目删除弹窗关闭', () => {
  async function openDialog() {
    pmSeed.projects.push({ id: 'proj-d', goal: '待删项目', timestamps: { createdAt: '2026-09-01T00:00:00Z' } })
    // 项目登记行无运行态（a2585be70）：不进状态筛选，仅「全部」视图可见
    renderPmAllStatuses(<PipelineManagerWidget />)
    fireEvent.click((await screen.findByText('待删项目')).closest('div')?.querySelector('button[aria-label="删除项目"]') as HTMLElement)
    expect(await screen.findByText(/请选择名下子任务的处置方式/)).toBeTruthy()
  }

  it('取消按钮关闭弹窗且不调删除端点（口径可来回切换）', async () => {
    await openDialog()
    // 口选级联再切回默认口径：radio onChange 双向生效
    fireEvent.click(screen.getByLabelText('连同子任务一起删除（不可恢复）'))
    fireEvent.click(screen.getByLabelText('仅删除项目——名下子任务挂起并保留'))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() =>
      expect(screen.queryByText(/请选择名下子任务的处置方式/)).toBeNull(),
    )
    expect(pmSeed.deleteProject).not.toHaveBeenCalled()
  })

  it('Esc 关闭弹窗', async () => {
    await openDialog()
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByText(/请选择名下子任务的处置方式/)).toBeNull(),
    )
  })
})

// ────────────────────────── 秒级 ticker ──────────────────────────

describe('PipelineManagerWidget 秒级 ticker', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('有运行中条目时每秒刷新耗时列（fake clock 驱动）', async () => {
    vi.useFakeTimers()
    const startedAt = new Date(Date.now() - 61_000).toISOString()
    pmSeed.runs.a = {
      pipeline_id: 'tick-p', run_id: 'run-tick', status: 'running', started_at: startedAt,
    }
    // query hook 全 mock：数据同步就绪，无需异步等待（fake timers 下 findBy 会挂起）
    renderWithProviders(<PipelineManagerWidget />)
    expect(screen.getByText('1m 1s')).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(screen.getByText('2m 1s')).toBeInTheDocument()
  })
})

// ────────────────────────── 任务管道 ID 双取 ──────────────────────────

describe('PipelineManagerWidget 任务解析边缘', () => {
  it('仅 metadata.pipelineRunId 的任务照常成行并可打开对话（agentId 落任务 id）', async () => {
    seedStandard()
    // meta任务（终态）不在缺省「运行中」视图：切「全部」（5971fdaa1）
    renderPmAllStatuses(<PipelineManagerWidget />)
    expect(await screen.findAllByText('meta任务').then((els) => els.length)).toBeGreaterThanOrEqual(1)

    fireEvent.click(screen.getAllByText('meta任务')[0])
    await waitFor(() =>
      expect(
        useAgentTabStore.getState().tabs.some(
          (t) => t.pipelineRunId === 'pipe-meta' && t.agentId === 't-meta',
        ),
      ).toBe(true),
    )
  })
})
