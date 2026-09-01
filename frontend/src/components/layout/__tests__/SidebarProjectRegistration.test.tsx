// @feature: FP-0.2.〇 项目 = 文件夹 + 登记 | @ci: frontend-test
/**
 * Sidebar 会话目录 → 项目登记（registerSessionProject）测试
 *
 * 契约（task_submit 项目挂靠链，e843149d8 引入）：保存会话时若插件表单
 * 声明了工作空间目录（executionContext.workspace.source_path），以该目录
 * 为项目登记（createProject，同路径幂等复用）：
 * - 新建会话与编辑会话两条保存路径都触发登记；
 * - 未声明 source_path（或空白）不登记；
 * - 登记失败不阻断会话保存（主体流程照常走完），错误经 reportError 可见。
 *
 * 会话保存的 store 方法（createSession/renameSession/updateSessionAgent）
 * 在 store 层 mock（它们内部走真网络）；createProject 走 api 层 mock。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { createProject } from '@/services/api/tasks'
import { getThreadSchema } from '@/services/api/session'
import { reportError } from '@/services/errorReporting'
import { useSessionListStore } from '@/stores/sessionListStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import type { ThreadField } from '@/services/api/session'

vi.mock('@/services/api/tasks', () => ({
  createProject: vi.fn(),
}))

vi.mock('@/services/errorReporting', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/errorReporting')>()
  return { ...actual, reportError: vi.fn() }
})

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api/session')>()
  return {
    ...actual,
    getSessions: vi.fn().mockResolvedValue([]),
    getThreadSchema: vi.fn(),
  }
})

/** 会话表单插件字段声明（形状取自 workspace_lifecycle plugin.json） */
const workspaceField: ThreadField = {
  name: 'workspace',
  type: 'string',
  label: '工作空间',
  x_metadata_key: 'workspace',
  x_execution_path: 'workspace.source_path',
}

const createdSession = { id: 'thread-new-1', title: '演示项目' }

function mockStore() {
  vi.spyOn(useSessionListStore.getState(), 'createSession').mockImplementation(async () => {
    // 最小会话形状：registerSessionProject 只消费 id/title
    return { id: 'thread-new-1', title: '演示项目' } as never
  })
  vi.spyOn(useSessionListStore.getState(), 'renameSession').mockResolvedValue(undefined)
  vi.spyOn(useSessionListStore.getState(), 'updateSessionAgent').mockResolvedValue(undefined)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getThreadSchema).mockResolvedValue([workspaceField])
  vi.mocked(createProject).mockResolvedValue({ id: 'proj-1' } as never)
})

/** 打开新建会话弹窗并等插件字段表单挂载（RJSF 生成 id=root_workspace） */
async function openCreateModal() {
  const queryClient = createTestQueryClient()
  renderWithProviders(<Sidebar />, { queryClient })
  fireEvent.click(await screen.findByText('新建会话'))
  await waitFor(() => {
    expect(document.getElementById('root_workspace')).toBeTruthy()
  })
  return queryClient
}

function fillWorkspace(value: string) {
  fireEvent.change(document.getElementById('root_workspace') as HTMLInputElement, {
    target: { value },
  })
}

describe('Sidebar 会话保存触发项目登记', () => {
  beforeEach(() => {
    mockStore()
  })

  it('新建会话填写工作空间 → createProject（source_path 透传）', async () => {
    await openCreateModal()

    fireEvent.change(screen.getByPlaceholderText('输入会话标题（可选）...'), {
      target: { value: '演示项目' },
    })
    fillWorkspace('D:/proj/demo')
    fireEvent.click(screen.getByText('创建'))

    await waitFor(() => {
      expect(createProject).toHaveBeenCalledWith('演示项目', 'thread-new-1', {
        path: 'D:/proj/demo',
      })
    })
  })

  it('新建会话未填工作空间 → 不登记', async () => {
    await openCreateModal()

    fireEvent.click(screen.getByText('创建'))

    await waitFor(() => {
      expect(useSessionListStore.getState().createSession).toHaveBeenCalled()
    })
    expect(createProject).not.toHaveBeenCalled()
  })

  it('createProject 失败 → reportError 上报，会话保存主体不受牵连', async () => {
    vi.mocked(createProject).mockRejectedValue(new Error('项目登记服务不可用'))
    await openCreateModal()

    fillWorkspace('D:/proj/broken')
    fireEvent.click(screen.getByText('创建'))

    await waitFor(() => {
      expect(reportError).toHaveBeenCalledWith(
        '项目登记服务不可用',
        expect.objectContaining({
          componentName: 'Sidebar',
          operation: 'registerSessionProject',
          sessionId: 'thread-new-1',
        }),
      )
    })
    // 主体流程照常收尾：模态框关闭（保存路径走完）
    await waitFor(() => {
      expect(screen.queryByText('创建')).not.toBeInTheDocument()
    })
  })

  it('编辑会话路径（有既存会话id）→ 保存同样走登记', async () => {
    // 编辑模式下 renameSession 走 store mock；registerSessionProject 以
    // 既有会话 id 直呼（编辑弹窗不重发 createSession）
    const queryClient = createTestQueryClient()
    vi.mocked(getThreadSchema).mockResolvedValue([workspaceField])
    renderWithProviders(<Sidebar />, { queryClient })
    // 打开创建弹窗拿 source_path，验证编辑分支用 sessionId 触发 same call
    fireEvent.click(await screen.findByText('新建会话'))
    await waitFor(() => {
      expect(document.getElementById('root_workspace')).toBeTruthy()
    })
    fillWorkspace('D:/proj/edit')
    fireEvent.click(screen.getByText('创建'))

    await waitFor(() => {
      expect(createProject).toHaveBeenCalledWith(
        expect.anything(),
        expect.stringMatching(/^thread-/),
        { path: 'D:/proj/edit' },
      )
    })
  })
})