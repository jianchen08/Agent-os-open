// @feature: FP-0.2.九 会话列表按工作空间聚合（工作空间编辑持久化） | @ci: frontend-test
/**
 * Sidebar 编辑会话写回工作空间（updateSessionWorkspace）测试
 *
 * 契约：编辑会话保存时，插件表单里的工作空间字段改动持久化到 thread
 * metadata（会话树聚合/列表徽标真值源）——只存本地执行快照的话
 * session.workspace 不变，列表聚合不随编辑走。
 *
 * store 方法 mock（真实现走网络）；Sidebar + 编辑模态框真实渲染。
 */
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { getSessions, getThreadSchema } from '@/services/api/session'
import { useSessionListStore } from '@/stores/sessionListStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import { openDropdownMenu } from '@/components/session/__tests__/sessionTestUtils'
import type * as sessionMod from '@/services/api/session'
import type * as errorReportingMod from '@/services/errorReporting'
import type { Session } from '@/types'
import type { ThreadField } from '@/services/api/session'

vi.mock('@/services/errorReporting', async (importOriginal) => {
  const actual = await importOriginal<errorReportingMod>()
  return { ...actual, reportError: vi.fn() }
})

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<sessionMod>()
  return {
    ...actual,
    getSessions: vi.fn().mockResolvedValue([]),
    getThreadSchema: vi.fn(),
  }
})

vi.mock('@/services/api/tasks', () => ({
  createProject: vi.fn().mockResolvedValue({ id: 'proj-1' }),
}))

/** 会话表单插件字段声明（形状取自 workspace_lifecycle plugin.json，type 取 string 供 RJSF 渲染输入框） */
const workspaceField: ThreadField = {
  name: 'workspace',
  type: 'string',
  label: '工作空间',
  x_metadata_key: 'workspace',
  x_execution_path: 'workspace.source_path',
}

const editTarget: Session = {
  id: 'thread-edit-1',
  title: '待改空间会话',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-02T00:00:00Z',
  messageCount: 2,
  pipelineIds: [],
  pinned: false,
  starred: false,
}

function mockStore() {
  vi.spyOn(useSessionListStore.getState(), 'renameSession').mockResolvedValue(undefined)
  vi.spyOn(useSessionListStore.getState(), 'updateSessionAgent').mockResolvedValue(undefined)
}

beforeEach(() => {
  // 编辑保存链会把插件表单值写入本地执行快照（localStorage），不清会泄入
  // 下一例的 initialValues（模拟的是不同用户会话的干净起点）
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(getSessions).mockResolvedValue([editTarget])
  vi.mocked(getThreadSchema).mockResolvedValue([workspaceField])
  mockStore()
})

afterEach(cleanup)

async function openEditModal() {
  const queryClient = createTestQueryClient()
  renderWithProviders(<Sidebar />, { queryClient })

  await screen.findByText('待改空间会话')
  openDropdownMenu(screen.getByLabelText('更多操作'))
  fireEvent.click(await screen.findByText('编辑会话'))
  await waitFor(() => {
    expect(document.getElementById('root_workspace')).toBeTruthy()
  })
}

describe('Sidebar 编辑会话写回工作空间', () => {
  it('编辑保存填写工作空间 → updateSessionWorkspace 以 metadata 键值透传', async () => {
    const updateSessionWorkspace = vi
      .spyOn(useSessionListStore.getState(), 'updateSessionWorkspace')
      .mockResolvedValue(undefined)

    await openEditModal()

    fireEvent.change(document.getElementById('root_workspace') as HTMLInputElement, {
      target: { value: 'D:/proj/edited' },
    })
    fireEvent.click(screen.getByRole('button', { name: /保存/ }))

    await waitFor(() => {
      expect(updateSessionWorkspace).toHaveBeenCalledWith(
        'thread-edit-1',
        'D:/proj/edited',
        undefined,
      )
    })
    // 保存链走完：模态框关闭
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /保存/ })).not.toBeInTheDocument()
    })
  })

  it('编辑保存未填工作空间 → 不触发写回（保持原状）', async () => {
    const updateSessionWorkspace = vi
      .spyOn(useSessionListStore.getState(), 'updateSessionWorkspace')
      .mockResolvedValue(undefined)

    await openEditModal()

    fireEvent.click(screen.getByRole('button', { name: /保存/ }))

    await waitFor(() => {
      expect(useSessionListStore.getState().renameSession).toHaveBeenCalled()
    })
    expect(updateSessionWorkspace).not.toHaveBeenCalled()
  })
})
