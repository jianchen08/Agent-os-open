// @feature FP-T12 前端适配（OBS-R258-1 交互反馈即时性收口） | @ci: frontend-test
/**
 * PipelineManagerPauseConfirmDialog 诚实状态机测试（#3）：
 * - 确认 await 窗口：确认按钮 disabled +「暂停中...」（confirming 消费 store 级
 *   actingTaskIds，弹窗不持本地 useState）
 * - 失败：toast 落在操作点 + 弹窗留驻可重试（成功才关，不假装成功）
 * - 成功：刷新任务列表 + onClose
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PipelineManagerPauseConfirmDialog, type PauseConfirmTarget } from '../PipelineManagerPauseConfirmDialog'

const apiMocks = vi.hoisted(() => ({ cancelTask: vi.fn() }))
vi.mock('@/services/api/tasks', () => apiMocks)

const invalidateMocks = vi.hoisted(() => ({ invalidateLongTermTasks: vi.fn() }))
vi.mock('@/hooks/queries/useLongTermTasksQuery', () => invalidateMocks)

const toastMocks = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn(), info: vi.fn() }))
vi.mock('@/components/ui/sonner', () => ({ toast: toastMocks }))

const TARGET: PauseConfirmTarget = { taskId: 't-1', name: '运行任务' }

function renderDialog(onClose = vi.fn()) {
  return { onClose, ...renderWithProviders(<PipelineManagerPauseConfirmDialog target={TARGET} onClose={onClose} />) }
}

describe('PipelineManagerPauseConfirmDialog 暂停确认诚实状态机', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useLongTermTaskStore.setState({ actingTaskIds: new Set() })
  })

  it('确认 await 窗口：按钮 disabled +「暂停中...」，成功后 onClose 且刷新任务列表', async () => {
    let resolveCancel: () => void = () => {}
    apiMocks.cancelTask.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveCancel = resolve
      }),
    )
    const { onClose } = renderDialog()

    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }))

    // await 窗口内：确认/取消按钮禁用，确认钮转圈文案
    const confirmingBtn = screen.getByRole('button', { name: /暂停中/ })
    expect(confirmingBtn).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
    expect(useLongTermTaskStore.getState().actingTaskIds.has('t-1')).toBe(true)

    resolveCancel()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(invalidateMocks.invalidateLongTermTasks).toHaveBeenCalled()
    expect(useLongTermTaskStore.getState().actingTaskIds.has('t-1')).toBe(false)
  })

  it('失败：toast 报错 + 弹窗留驻（onClose 不调），确认态复位可重试', async () => {
    apiMocks.cancelTask.mockRejectedValueOnce(new Error('端点故障'))
    const { onClose } = renderDialog()

    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }))

    await waitFor(() =>
      expect(toastMocks.error).toHaveBeenCalledWith('暂停任务失败', { description: '端点故障' }),
    )
    // 不假装成功：弹窗留驻
    expect(screen.getByText(/任务将暂停，可恢复/)).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    // 确认态复位可重试
    await waitFor(() => expect(screen.getByRole('button', { name: '确认暂停' })).toBeEnabled())
  })

  it('非 Error 拒绝（apiClient ApiError 形态）：toast 回退通用文案', async () => {
    apiMocks.cancelTask.mockRejectedValueOnce({ message: '无权限' })
    renderDialog()

    fireEvent.click(screen.getByRole('button', { name: '确认暂停' }))
    await waitFor(() =>
      expect(toastMocks.error).toHaveBeenCalledWith('暂停任务失败', { description: '无权限' }),
    )
  })
})
