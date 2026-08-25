/** @feature FP-0.2.四 前端Schema | @ci frontend-test */
/**
 * DebugExecutionRecordsPage「清空全部」按钮行为测试
 *
 * 2026-08-24 clear-all stub 做实配套：验证 confirm 二次确认、API 调用、
 * 成功后批量失效受影响缓存（执行记录/会话/任务/LLM 快照等）、
 * 失败时后端 detail 透传展示（409 运行中管道等）。
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { queryKeys } from '@/services/query/queryKeys'
import { renderWithProviders, createTestQueryClient } from '@/test/renderWithProviders'

vi.mock('@/services/api/executionRecords', () => ({
  getExecutionRecordsSessions: vi.fn(),
  getExecutionRecords: vi.fn(),
  clearAllExecutionRecords: vi.fn(),
}))

import * as api from '@/services/api/executionRecords'
import { DebugExecutionRecordsPage } from '../DebugExecutionRecordsPage'

const mockGetSessions = vi.mocked(api.getExecutionRecordsSessions)
const mockGetRecords = vi.mocked(api.getExecutionRecords)
const mockClearAll = vi.mocked(api.clearAllExecutionRecords)

function mockConfirm(ret: boolean) {
  return vi.spyOn(window, 'confirm').mockReturnValue(ret)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockGetSessions.mockResolvedValue({ sessions: [], total: 0 })
  mockGetRecords.mockResolvedValue({ records: [], total: 0, session_id: null })
})

describe('DebugExecutionRecordsPage 清空全部', () => {
  it('渲染清空按钮（桌面与面板 embedded 共用组件）', async () => {
    renderWithProviders(<DebugExecutionRecordsPage />)
    expect(screen.getByRole('button', { name: /清空全部/ })).toBeInTheDocument()
    await waitFor(() => expect(mockGetRecords).toHaveBeenCalled())
  })

  it('embedded 面板模式同样渲染清空按钮（PageShell embedded 下 actions 工具行）', async () => {
    renderWithProviders(<DebugExecutionRecordsPage embedded />)
    expect(screen.getByRole('button', { name: /清空全部/ })).toBeInTheDocument()
    await waitFor(() => expect(mockGetRecords).toHaveBeenCalled())
  })

  it('confirm 取消时不调用清理 API', async () => {
    mockConfirm(false)
    const user = userEvent.setup()
    renderWithProviders(<DebugExecutionRecordsPage />)
    await user.click(screen.getByRole('button', { name: /清空全部/ }))
    expect(mockClearAll).not.toHaveBeenCalled()
    // confirm 文案包含不可撤销警示
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('不可撤销'))
  })

  it('confirm 确认后调用 API 并批量失效受影响缓存', async () => {
    mockConfirm(true)
    mockClearAll.mockResolvedValue({
      success: true,
      message: 'ok',
      cleared_count: 11,
      payload_files_deleted: 3,
      backup_path: '/x/bak',
    })
    const queryClient = createTestQueryClient()
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')
    const user = userEvent.setup()
    renderWithProviders(<DebugExecutionRecordsPage />, { queryClient })

    await user.click(screen.getByRole('button', { name: /清空全部/ }))

    await waitFor(() => expect(mockClearAll).toHaveBeenCalledTimes(1))
    await waitFor(() => {
      expect(screen.getByText(/已清理 11 条/)).toBeInTheDocument()
    })
    // 执行记录（前缀失效：覆盖全部会话分条）+ 会话 + 任务 + LLM 快照 + 聊天会话
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.executionRecordsPrefix })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.debugSessions })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.debugTasks })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.llmPayloadDiagPrefix })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.sessions })
  })

  it('失败时透传后端 detail（409 运行中管道）', async () => {
    mockConfirm(true)
    const err = Object.assign(new Error('Request failed with status code 409'), {
      response: { status: 409, data: { error: 'clear failed', detail: '管道 p1 正在运行，请等待任务结束后再清理' } },
    })
    mockClearAll.mockRejectedValue(err)
    const user = userEvent.setup()
    renderWithProviders(<DebugExecutionRecordsPage />)

    await user.click(screen.getByRole('button', { name: /清空全部/ }))

    await waitFor(() => {
      expect(screen.getByText(/正在运行/)).toBeInTheDocument()
    })
    // 失败后按钮可再次点击（非永久禁用）
    expect(screen.getByRole('button', { name: /清空全部/ })).toBeEnabled()
  })

  it('清理进行中按钮禁用防重复触发', async () => {
    mockConfirm(true)
    let resolveFn: (v: api.ClearAllExecutionRecordsResponse) => void = () => {}
    mockClearAll.mockReturnValue(
      new Promise((resolve) => {
        resolveFn = resolve
      }) as Promise<api.ClearAllExecutionRecordsResponse>,
    )
    const user = userEvent.setup()
    renderWithProviders(<DebugExecutionRecordsPage />)

    await user.click(screen.getByRole('button', { name: /清空全部/ }))
    // 清理中：文案切换 + 禁用防重复触发
    const busyButton = screen.getByRole('button', { name: /清理中/ })
    expect(busyButton).toBeDisabled()

    resolveFn({
      success: true,
      message: 'ok',
      cleared_count: 0,
      payload_files_deleted: 0,
      backup_path: null,
    })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /清空全部/ })).toBeEnabled()
    })
  })
})
