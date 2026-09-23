// @feature: FP-T12 前端适配 | @ci: frontend-test
// @ci frontend-test
/**
 * PipelineManagerWidget 四态契约测试（OBS-R258-1 components 试点）
 *
 * 面板为多源聚合查询面（runs/states/全量任务/项目登记），四态口径按面拆分：
 * - 主列表面：首载中（主源 pending 且尚无条目）显示加载态，不渲染
 *   「暂无管道运行记录」空态伪装；加载完成无数据才落空态
 * - 项目登记副面：拉取失败降级为留痕横幅 + 手动重试（登记行只是分组/归局
 *   面，不阻断任务列表），不伪装成「无项目」
 */

import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import { renderWithProviders } from '@/test/renderWithProviders'

const seed = vi.hoisted(() => ({
  mockUseRuns: vi.fn(),
  mockUseStates: vi.fn(),
  mockUseTasks: vi.fn(),
  mockUseSessions: vi.fn(),
  mockUseProjects: vi.fn(),
  mockProjectsRefetch: vi.fn(),
}))

// 渲染专用文件（无条目点击）：四个 query hook 是四态契约的塑造对象（逐用例
// 覆写返回形态），任务域端点/导航/工作空间/实时 usage 不进入渲染路径——真实
// 模块惰性加载等价，无需 mock。
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  usePipelineRunsQuery: seed.mockUseRuns,
  usePipelineStatesQuery: seed.mockUseStates,
}))
vi.mock('@/hooks/queries/useAllTasksQuery', () => ({
  useAllTasksQuery: seed.mockUseTasks,
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: seed.mockUseSessions,
}))
vi.mock('@/hooks/queries/useProjectsQuery', () => ({
  useProjectsQuery: seed.mockUseProjects,
}))

/** query 结果造型：首载中 */
const qLoading = () => ({ status: 'pending' as const, isLoading: true, data: undefined })
/** query 结果造型：成功有数据 */
const qReady = <T,>(data: T) => ({ status: 'success' as const, isLoading: false, data })
/** query 结果造型：失败（refetch 可断言重试通道） */
const qError = (msg: string) => ({
  status: 'error' as const,
  isLoading: false,
  error: new Error(msg),
  data: undefined,
  refetch: seed.mockProjectsRefetch,
})

beforeEach(() => {
  seed.mockProjectsRefetch.mockReset()
})

/** 主列表四源就绪基线：全部落「成功有数据」空态（真空态/项目失败用例的
 *  公共前提——主源空才可见空态，项目面单独覆写） */
function seedMainQueriesReady() {
  seed.mockUseRuns.mockReturnValue(qReady({}))
  seed.mockUseStates.mockReturnValue(qReady({}))
  seed.mockUseTasks.mockReturnValue(qReady([]))
  seed.mockUseSessions.mockReturnValue(qReady([]))
}

describe('PipelineManagerWidget 四态契约（OBS-R258-1）', () => {
  it('首载中（主源全部 pending 且尚无条目）：显示加载态，不渲染「暂无」空态伪装', () => {
    seed.mockUseRuns.mockReturnValue(qLoading())
    seed.mockUseStates.mockReturnValue(qLoading())
    seed.mockUseTasks.mockReturnValue(qLoading())
    seed.mockUseSessions.mockReturnValue(qReady([]))
    seed.mockUseProjects.mockReturnValue(qLoading())
    renderWithProviders(<PipelineManagerWidget />)

    expect(screen.getByText('加载中...')).toBeInTheDocument()
    expect(screen.queryByText('暂无管道运行记录')).not.toBeInTheDocument()
  })

  it('加载完成无数据：落「暂无管道运行记录」空态（加载门不遮蔽真空态）', () => {
    seedMainQueriesReady()
    seed.mockUseProjects.mockReturnValue(qReady({ items: [] }))
    renderWithProviders(<PipelineManagerWidget />)

    expect(screen.getByText('暂无管道运行记录')).toBeInTheDocument()
    expect(screen.queryByText('加载中...')).not.toBeInTheDocument()
  })

  it('项目登记拉取失败：降级留痕横幅 + 重试（不伪装成「无项目」，不阻断主列表）', () => {
    seedMainQueriesReady()
    seed.mockUseProjects.mockReturnValue(qError('projects down'))
    renderWithProviders(<PipelineManagerWidget />)

    expect(screen.getByText(/项目登记拉取失败（projects down）/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试拉取项目登记' }))
    expect(seed.mockProjectsRefetch).toHaveBeenCalledTimes(1)
  })
})
