/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * sessionCreation 共享服务测试
 *
 * Sidebar 与声明式表单页（FormWidget createSession）共用的会话创建流：
 * store.createSession 落会话+主管道；声明了工作空间目录时随建即登记项目，
 * 登记失败不阻断会话创建（会话是主体）。
 */
import { vi } from 'vitest'

const { createProjectMock, createSessionMock, reportErrorMock } = vi.hoisted(() => ({
  createProjectMock: vi.fn(),
  createSessionMock: vi.fn(),
  reportErrorMock: vi.fn(),
}))

vi.mock('@/services/api/tasks', () => ({
  createProject: (...args: unknown[]) => createProjectMock(...args),
}))
vi.mock('@/services/errorReporting', () => ({
  reportError: (...args: unknown[]) => reportErrorMock(...args),
  ErrorSeverity: { ERROR: 'error', WARNING: 'warning' },
  ErrorType: { SERVER: 'server' },
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: { getState: () => ({ createSession: createSessionMock }) },
}))

import { createSessionWithProject, registerSessionProject } from '../sessionCreation'

describe('sessionCreation — 会话创建共享流', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    createSessionMock.mockResolvedValue({ id: 's1', title: '新会话' })
    createProjectMock.mockResolvedValue({ id: 'p1' })
  })

  it('createSessionWithProject：透传标题/agent/插件表单值；无工作空间声明不登记项目', async () => {
    const created = await createSessionWithProject('新会话', 'agentos', {
      fieldMetadata: { isolation_mode: 'isolated' },
    })

    expect(created).toMatchObject({ id: 's1' })
    expect(createSessionMock).toHaveBeenCalledWith('新会话', {
      agentId: 'agentos',
      fieldMetadata: { isolation_mode: 'isolated' },
    })
    expect(createProjectMock).not.toHaveBeenCalled()
  })

  it('createSessionWithProject：声明工作空间目录 → 随建登记项目（同路径幂等复用）', async () => {
    // created.title 优先于入参 title（登记名跟随服务端创建结果）
    createSessionMock.mockResolvedValue({ id: 's1', title: '项目会话' })
    await createSessionWithProject('项目会话', null, {
      fieldMetadata: {},
      executionContext: { workspace: { source_path: 'D:/ws/demo' } },
    })

    expect(createProjectMock).toHaveBeenCalledWith('项目会话', 's1', { path: 'D:/ws/demo' })
  })

  it('项目登记失败不阻断会话创建：错误上报可见，主流程照常返回', async () => {
    createProjectMock.mockRejectedValue(new Error('登记炸了'))

    const created = await createSessionWithProject('新会话', null, {
      fieldMetadata: {},
      executionContext: { workspace: { source_path: 'D:/ws/demo' } },
    })

    expect(created).toMatchObject({ id: 's1' })
    expect(reportErrorMock).toHaveBeenCalledWith(
      '登记炸了',
      expect.objectContaining({ operation: 'registerSessionProject', sessionId: 's1' }),
    )
  })

  it('registerSessionProject：title 为空回退「新会话」登记名', async () => {
    await registerSessionProject('s1', '', {
      fieldMetadata: {},
      executionContext: { workspace: { source_path: 'D:/ws' } },
    })
    expect(createProjectMock).toHaveBeenCalledWith('新会话', 's1', { path: 'D:/ws' })
  })
})
