/**
 * 执行控制 API 服务测试
 *
 * 测试任务执行的控制接口：暂停、恢复、取消、回滚等
 * 与后端 /api/v1/execution/* 端点对齐
 */

/* eslint-disable import-x/order */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  approveExecution,
  cancelExecution,
  controlExecution,
  getExecutionStatus,
  getExecutionSteps,
  injectAgentMessage,
  pauseExecution,
  resumeExecution,
  rollbackExecution,
} from '@/services/api/executionControl'
// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

 
import apiClient from '@/services/api/client'

describe('执行控制 API', () => {
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
