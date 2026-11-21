// @feature: FP-0.2.九 会话树按工作空间聚合 | @ci: frontend-test
/**
 * Sidebar 会话树顶栏一键新建（createSessionInWorkspace）测试
 *
 * 契约：顶栏「+」不开弹窗直接创建会话，与弹窗创建同链路——
 * - createSession 携带 fieldMetadata {workspace, workspace_mode:'plain'}
 *   （metadata 出生落库，映射面读 metadata.workspace 回显）；
 * - executionContext.workspace.source_path 触发项目登记（createProject，
 *   同弹窗路径幂等复用）。
 *
 * store 方法与 API 层 mock（内部走真网络）；Sidebar 本体真实渲染。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { getSessions } from '@/services/api/session'
import { createProject } from '@/services/api/tasks'
import { useSessionListStore } from '@/stores/sessionListStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import type * as sessionMod from '@/services/api/session'
import type * as errorReportingMod from '@/services/errorReporting'
import type { Session } from '@/types'

vi.mock('@/services/api/tasks', () => ({
  createProject: vi.fn(),
}))

vi.mock('@/services/errorReporting', async (importOriginal) => {
  const actual = await importOriginal<errorReportingMod>()
  return { ...actual, reportError: vi.fn() }
})

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<sessionMod>()
  return {
    ...actual,
    getSessions: vi.fn().mockResolvedValue([]),
    getThreadSchema: vi.fn().mockResolvedValue([]),
  }
})

const workspaceSession: Session = {
  id: 'thread-ws-1',
  title: '项目会话',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-02T00:00:00Z',
  messageCount: 3,
  workspace: 'D:/proj/demo',
  workspaceMode: 'plain',
  pipelineIds: [],
  pinned: false,
  starred: false,
}

const defaultSession: Session = {
  id: 'thread-default-1',
  title: '闲聊会话',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-03T00:00:00Z',
  messageCount: 1,
  pipelineIds: [],
  pinned: false,
  starred: false,
}

function mockStore() {
  vi.spyOn(useSessionListStore.getState(), 'createSession').mockImplementation(async () => {
    // 最小会话形状：registerSessionProject 只消费 id/title
    return { id: 'thread-new-1', title: '灵汐' } as never
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(createProject).mockResolvedValue({ id: 'proj-1' } as never)
})

describe('Sidebar 会话树顶栏一键新建', () => {
  beforeEach(() => {
    mockStore()
    vi.mocked(getSessions).mockResolvedValue([workspaceSession, defaultSession])
  })

  it('顶栏「+」→ createSession 携带 workspace metadata + createProject 登记该空间', async () => {
    const queryClient = createTestQueryClient()
    renderWithProviders(<Sidebar />, { queryClient })

    // 会话树顶栏出现（目录名封面），默认空间会话不进树
    expect(await screen.findByTestId('workspace-tree-header')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('workspace-tree-create'))

    await waitFor(() => {
      expect(useSessionListStore.getState().createSession).toHaveBeenCalledWith(
        undefined,
        expect.objectContaining({
          fieldMetadata: { workspace: 'D:/proj/demo', workspace_mode: 'plain' },
        }),
      )
    })
    await waitFor(() => {
      expect(createProject).toHaveBeenCalledWith('灵汐', 'thread-new-1', {
        path: 'D:/proj/demo',
      })
    })
  })

  it('创建失败 → reportError 上报（operation=createSessionInWorkspace）', async () => {
    const { reportError } = await import('@/services/errorReporting')
    vi.spyOn(useSessionListStore.getState(), 'createSession').mockRejectedValue(
      new Error('会话创建失败'),
    )

    const queryClient = createTestQueryClient()
    renderWithProviders(<Sidebar />, { queryClient })

    fireEvent.click(await screen.findByTestId('workspace-tree-create'))

    await waitFor(() => {
      expect(reportError).toHaveBeenCalledWith(
        '会话创建失败',
        expect.objectContaining({
          componentName: 'Sidebar',
          operation: 'createSessionInWorkspace',
        }),
      )
    })
    expect(createProject).not.toHaveBeenCalled()
  })
})
