// @feature FP-T12 前端组件补测 | @ci: frontend-test
/**
 * 任务行「⋮」菜单按条目形态出动作（方案 B：低频动作唯一常驻入口，任何宽度
 * 都在菜单内——旧「桌面平铺/移动菜单」双形态退役）：
 * - 运行中任务：停止（暂停任务）/ 删除管道 在场（2026-11-11「打叉」位改删除；
 *   方案 B 词汇=「停止（暂停任务）」）；
 * - 挂起任务：继续（恢复任务）在场、停止不在场；
 * - 带 workspacePath 的任务：打开工作空间在场；
 * - 常驻「发消息」为打开对话主入口（方案 B：菜单不再含「打开对话」）。
 * 方案 B 适配登记：旧用例「菜单动作点击链：打开对话/任务现场/…」改写——
 * 打开对话改行内常驻「发消息」、任务现场入口移除（用户裁定 2026-11-20），
 * 接线不断契约改由现有动作面（工作空间/复制/停止）承载（非删除）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { pmMod, pmSeed, renderPmAllStatuses, resetPmContainers } from './pmTestUtils'
// eslint-disable-next-line import-x/order -- mock 装配时序约束（见 failureAttribution 同款）
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

/** 播种一条管道任务（run + state + 长期任务面各一行） */
function seedPipeline(
  pipelineId: string,
  title: string,
  runStatus: string,
  workspacePath?: string,
): void {
  pmSeed.runs[pipelineId] = {
    pipeline_id: pipelineId,
    run_id: `run-${pipelineId}`,
    thread_id: `th-${pipelineId}`,
    title,
    status: runStatus,
    started_at: '2026-11-19T18:00:00.771+00:00',
  }
  pmSeed.states[pipelineId] = {
    pipeline_id: pipelineId,
    thread_id: `th-${pipelineId}`,
    source: 'memory',
    state: {
      run_status: runStatus,
      // 工作区坐标 state 真源键（ws_meta.path/workspace，见 widget workspacePath 注释）
      ...(workspacePath ? { workspace: workspacePath } : {}),
    },
  }
  pmSeed.tasks.push({ id: `t-${pipelineId}`, title, status: runStatus, pipeline_run_id: pipelineId })
}

/** 打开某条目的「⋯」菜单并等渲染 */
async function openMenu(entryKey: string): Promise<void> {
  const more = screen.getByTestId(`pipeline-entry-more-${entryKey}`)
  fireEvent.pointerDown(more)
  await screen.findByText('复制管道 ID')
}

beforeEach(() => {
  resetPmContainers()
  // 方案 B：「⋮」菜单是低频动作唯一形态（旧移动/桌面双形态退役；宽度驱动
  // 的只是辅助信息显隐，jsdom 无 CSS 排版不可单测）
  window.innerWidth = 375
})

describe('条目菜单按形态出动作', () => {
  it('运行中任务：停止（暂停任务）与删除管道在场；常驻「发消息」在场', async () => {
    seedPipeline('pipe-run', '运行中任务', 'running')
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('运行中任务')

    // 方案 B：打开对话主入口 = 行内常驻「发消息」（不在菜单）
    expect(screen.getByLabelText('发消息')).toBeInTheDocument()
    await openMenu('pipe-run')
    expect(screen.getByText('停止（暂停任务）')).toBeInTheDocument()
    expect(screen.getByText('删除管道')).toBeInTheDocument()
    expect(screen.queryByText('继续（恢复任务）')).not.toBeInTheDocument()
  })

  it('挂起任务：继续（恢复任务）在场、停止退场；点继续走 resume 动作', async () => {
    seedPipeline('pipe-sus', '挂起任务', 'suspended')
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('挂起任务')

    await openMenu('pipe-sus')
    expect(screen.getByText('继续（恢复任务）')).toBeInTheDocument()
    expect(screen.queryByText('停止（暂停任务）')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('继续（恢复任务）'))
    await waitFor(() => expect(pmSeed.resumeTask).toHaveBeenCalled())
  })

  it('菜单动作点击链：工作空间/复制/停止的 onSelect 均可走通（动作面接线不断）', async () => {
    seedPipeline('pipe-act', '动作任务', 'running', 'D:/ws/act')
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('动作任务')

    for (const label of ['打开工作空间', '复制管道 ID', '停止（暂停任务）']) {
      await openMenu('pipe-act')
      const item = screen.getByText(label)
      fireEvent.click(item)
      // onSelect 已接线（点击后菜单卸载，不抛错不残留）
      await waitFor(() => {
        expect(screen.queryByText('复制管道 ID')).not.toBeInTheDocument()
      })
    }
    // 动作至少落一面（停止走 tasksApi 的 pauseTask 桩）
    await waitFor(() => expect(pmSeed.pauseTask).toHaveBeenCalled())
  })

  it('带 workspacePath 的任务：打开工作空间在场', async () => {
    seedPipeline('pipe-ws', '带空间任务', 'completed', 'D:/ws/demo')
    renderPmAllStatuses(<PipelineManagerWidget />)
    await screen.findAllByText('带空间任务')

    await openMenu('pipe-ws')
    expect(screen.getByText('打开工作空间')).toBeInTheDocument()
  })

  // 注：项目行「⋮」菜单分支（打开项目文件夹/删除项目）可达——EntryActionsMenu
  // 对 project 条目同样渲染（方案 B 后菜单为唯一低频动作入口），项目族用例
  // 见 PipelineManagerWidget.test.tsx「项目行渲染删除按钮」等。
})
