/** @feature FP-MIGR 0.1→0.2迁移 @vision V6 可即用 @ci frontend-test */
/**
 * 执行控制 API 服务测试（0.2 已移除该模块）
 *
 * 0.2 迁移后 src/services/api/executionControl.ts 已整体移除：暂停/恢复/取消/审批/注入等
 * 执行控制能力改由内核管道 + interaction/review 插件驱动，前端不再直连 /api/v1/execution/*。
 * 下面的用例整体标 describe.skip，避免调用已删函数导致 TypeError 红色失败。
 * 待后续按新交互契约重写后再恢复。
 */

/* eslint-disable import-x/order */
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 0.2 已移除 executionControl 模块（文件不存在，无法 import）。
// 这里定义本地 stub 占位，使用例骨架可解析；整体 describe.skip 不会真正调用。
const approveExecution = vi.fn()
const cancelExecution = vi.fn()
const controlExecution = vi.fn()
const getExecutionStatus = vi.fn()
const getExecutionSteps = vi.fn()
const injectAgentMessage = vi.fn()
const pauseExecution = vi.fn()
const resumeExecution = vi.fn()
const rollbackExecution = vi.fn()

// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

// 0.2 已移除该模块：整体 skip，保留用例骨架待后续按新契约重写。
describe.skip('执行控制 API（0.2 已移除该模块：executionControl）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('controlExecution - 通用执行控制', () => {
    it('应该成功发送控制命令', async () => {
      const mockResponse = { id: 'exec-1', status: 'paused' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await controlExecution('exec-1', 'pause')

      expect(result).toEqual(mockResponse)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/execution/exec-1/control', {
        action: 'pause',
        params: undefined,
      })
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(controlExecution('', 'pause')).rejects.toThrow('执行 ID 不能为空')
    })
  })

  describe('pauseExecution - 暂停执行', () => {
    it('应该成功暂停执行', async () => {
      const mockResponse = { id: 'exec-1', status: 'paused' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await pauseExecution('exec-1')

      expect(result.status).toBe('paused')
    })
  })

  describe('resumeExecution - 恢复执行', () => {
    it('应该成功恢复执行', async () => {
      const mockResponse = { id: 'exec-1', status: 'running' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await resumeExecution('exec-1')

      expect(result.status).toBe('running')
    })
  })

  describe('cancelExecution - 取消执行', () => {
    it('应该成功取消执行', async () => {
      const mockResponse = { id: 'exec-1', status: 'cancelled' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await cancelExecution('exec-1')

      expect(result.status).toBe('cancelled')
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/execution/exec-1/cancel')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(cancelExecution('')).rejects.toThrow('执行 ID 不能为空')
    })
  })

  describe('rollbackExecution - 回滚执行', () => {
    it('应该成功回滚到指定步骤', async () => {
      const mockResponse = { id: 'exec-1', status: 'running' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await rollbackExecution('exec-1', 'step-2')

      expect(result).toEqual(mockResponse)
    })
  })

  describe('injectAgentMessage - 注入消息', () => {
    it('应该成功注入消息', async () => {
      const mockResponse = { id: 'exec-1', status: 'running' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await injectAgentMessage('exec-1', { content: '测试消息' })

      expect(result).toEqual(mockResponse)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/execution/exec-1/inject', {
        content: '测试消息',
      })
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(injectAgentMessage('', { content: 'test' })).rejects.toThrow('执行 ID 不能为空')
    })

    it('应该在消息内容为空时抛出错误', async () => {
      await expect(injectAgentMessage('exec-1', { content: '' })).rejects.toThrow(
        '消息内容不能为空',
      )
    })
  })

  describe('getExecutionStatus - 获取执行状态', () => {
    it('应该成功获取执行状态', async () => {
      const mockResponse = {
        id: 'exec-1',
        status: 'running',
        intent: '测试任务',
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      const result = await getExecutionStatus('exec-1')

      expect(result).toEqual(mockResponse)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/execution/exec-1')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(getExecutionStatus('')).rejects.toThrow('执行 ID 不能为空')
    })
  })

  describe('getExecutionSteps - 获取执行步骤', () => {
    it('应该成功获取执行步骤列表', async () => {
      const mockSteps = [
        { id: 'step-1', name: '步骤1', status: 'completed' },
        { id: 'step-2', name: '步骤2', status: 'running' },
      ]
      vi.mocked(apiClient.get).mockResolvedValueOnce({
        data: { steps: mockSteps },
      })

      const result = await getExecutionSteps('exec-1')

      expect(result).toEqual(mockSteps)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/execution/exec-1/steps')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(getExecutionSteps('')).rejects.toThrow('执行 ID 不能为空')
    })
  })

  describe('approveExecution - 审批执行', () => {
    it('应该成功批准执行', async () => {
      const mockResponse = { id: 'exec-1', status: 'running' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await approveExecution('exec-1', { action: 'approve' })

      expect(result).toEqual(mockResponse)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/execution/exec-1/approve', {
        action: 'approve',
      })
    })

    it('应该成功拒绝执行', async () => {
      const mockResponse = { id: 'exec-1', status: 'cancelled' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await approveExecution('exec-1', {
        action: 'reject',
        comment: '不符合要求',
      })

      expect(result.status).toBe('cancelled')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(approveExecution('', { action: 'approve' })).rejects.toThrow('执行 ID 不能为空')
    })
  })
})
