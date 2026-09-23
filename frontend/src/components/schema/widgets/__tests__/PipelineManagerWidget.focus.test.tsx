// @feature: FP-T12 前端适配(OBS-R259-1 通知点击路由跳转) | @ci: frontend-test
/**
 * PipelineManagerWidget 任务定位测试（通知点击 → focusTaskId）
 *
 * 契约（OBS-R259-1）：focusTaskId 非空时定位该任务行——解除状态筛选收窄
 * （面板默认只看运行中，终态任务行必须可见）、展开树祖先链、行内滚动定位
 * 并短暂高亮（data-located）；任务行不在列表（未含/已清理）→ 只保留开签
 * 不定位（筛选不动、无定位标记）。
 *
 * mock 约定沿用 PipelineManagerWidget.test.tsx：runs/states/全量任务三个
 * query hook 播种数据。
 */
import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { renderWithProviders } from '@/test/renderWithProviders'

const seed = vi.hoisted(() => {
  /** 终态任务行（默认「运行中」筛选下不可见——定位用例的筛选收窄前提） */
  const RUNS: Record<string, unknown> = {
    r1: {
      pipeline_id: 'pipe-x',
      run_id: 'run-x',
      thread_id: 'th-x',
      status: 'completed',
      started_at: '2026-09-01T00:00:00Z',
    },
    r2: {
      pipeline_id: 'pipe-parent',
      run_id: 'run-parent',
      thread_id: 'th-p',
      status: 'completed',
      started_at: '2026-09-01T00:00:00Z',
    },
    r3: {
      pipeline_id: 'pipe-child',
      run_id: 'run-child',
      thread_id: 'th-p',
      status: 'completed',
      started_at: '2026-09-01T00:01:00Z',
    },
  }
  const ALL_TASKS: Record<string, unknown>[] = [
    {
      id: 'task-x',
      title: '被定位任务',
      status: 'completed',
      pipeline_run_id: 'pipe-x',
      agent_name: 'general_agent',
    },
    {
      id: 'task-parent',
      title: '父任务',
      status: 'completed',
      pipeline_run_id: 'pipe-parent',
      agent_name: 'general_agent',
    },
    {
      id: 'task-child',
      title: '嵌套子任务',
      status: 'completed',
      pipeline_run_id: 'pipe-child',
      parent_task_id: 'task-parent',
      agent_name: 'general_agent',
    },
  ]
  const mockUseAllTasksQuery = vi.fn(() => ({ data: ALL_TASKS }))
  const mockUsePipelineRunsQuery = vi.fn(() => ({ data: RUNS }))
  const mockUsePipelineStatesQuery = vi.fn(() => ({ data: {} }))
  return {
    RUNS,
    ALL_TASKS,
    mockUseAllTasksQuery,
    mockUsePipelineRunsQuery,
    mockUsePipelineStatesQuery,
  }
})

vi.mock('@/services/api/tasks', () => ({
  // 定位专用文件（无条目点击）：任务域端点只有 useProjectsQuery 的
  // fetchProjects 进入渲染路径
  fetchProjects: vi.fn(() => Promise.resolve({ items: [] })),
}))
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: seed.mockUsePipelineRunsQuery,
  usePipelineStatesQuery: seed.mockUsePipelineStatesQuery,
}))
vi.mock('@/hooks/queries/useAllTasksQuery', () => ({
  useAllTasksQuery: seed.mockUseAllTasksQuery,
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))

/** 行定位标记查询（属性值精确匹配，不经选择器转义） */
function locatedRow(key: string): Element | null {
  for (const el of document.querySelectorAll('[data-entry-key]')) {
    if (el.getAttribute('data-entry-key') === key) return el
  }
  return null
}

function renderWidget(focusTaskId?: string) {
  return renderWithProviders(<PipelineManagerWidget focusTaskId={focusTaskId} />)
}

beforeEach(() => {
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
})

describe('PipelineManagerWidget focusTaskId 任务定位', () => {
  it('定位终态任务行：解除「运行中」筛选收窄，行可见且带定位标记', async () => {
    renderWidget('task-x')

    // 默认筛选只看运行中——定位必须让终态行可见（筛选被解除）
    const row = await screen.findByText('被定位任务')
    expect(row).toBeInTheDocument()
    const marked = locatedRow('pipe-x')
    expect(marked).not.toBeNull()
    expect(marked!.getAttribute('data-located')).toBe('true')
  })

  it('任务行不在列表（未知任务 id）：筛选不动、无定位标记（只保留开签语义）', () => {
    renderWidget('task-missing')

    expect(screen.queryByText('被定位任务')).not.toBeInTheDocument()
    expect(locatedRow('pipe-x')).toBeNull()
  })

  it('不传 focusTaskId：面板按默认「运行中」筛选渲染，无定位标记', () => {
    renderWithProviders(<PipelineManagerWidget />)

    expect(screen.queryByText('被定位任务')).not.toBeInTheDocument()
    expect(locatedRow('pipe-x')).toBeNull()
  })

  it('嵌套子任务：祖先链展开后子任务行可见并带定位标记', async () => {
    renderWidget('task-child')

    const row = await screen.findByText('嵌套子任务')
    expect(row).toBeInTheDocument()
    const marked = locatedRow('pipe-child')
    expect(marked).not.toBeNull()
    expect(marked!.getAttribute('data-located')).toBe('true')
  })

  it('列表视图同样支持定位（行 data-entry-key + 定位标记）', async () => {
    const { container } = renderWidget('task-x')
    fireEvent.click(screen.getByText('列表'))

    await screen.findByText('被定位任务')
    expect(container.querySelector('[data-entry-key="pipe-x"]')?.getAttribute('data-located')).toBe(
      'true',
    )
  })
})
