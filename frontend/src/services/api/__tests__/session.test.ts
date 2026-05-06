/**
 * 会话和消息API服务测试
 *
 * 测试与后端 Thread API 的集成，验证数据映射和错误处理。
 *
 * 注意：createSession 函数已删除，会话只能通过主agent创建
 */

import MockAdapter from 'axios-mock-adapter'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { API_ENDPOINTS } from '@/services/api/../../constants/api'
import apiClient from '@/services/api/client'
import { getSessions, deleteSession, getMessages } from '@/services/api/session'
import type { Message } from '@/services/api/../../types/models'

// 创建axios mock适配器
const mockAxios = new MockAdapter(apiClient)

describe('会话和消息API服务', () => {
  beforeEach(() => {
    // 每个测试前重置mock
    mockAxios.reset()
    // 清除localStorage
    localStorage.clear()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('getSessions', () => {
    it('应该成功获取会话列表', async () => {
      // 准备后端 Thread API 响应数据
      const mockThreads = [
        {
          thread_id: 'session-1',
          current_state: 'idle',
          intent: '测试会话1',
          created_at: '2024-01-01T00:00:00Z',
          updated_at: '2024-01-01T00:00:00Z',
          agent_id: null,
        },
        {
          thread_id: 'session-2',
          current_state: 'running',
          intent: '测试会话2',
          created_at: '2024-01-02T00:00:00Z',
          updated_at: '2024-01-02T00:00:00Z',
          agent_id: 'agent-1',
        },
      ]

      // 模拟后端 Thread API 响应
      mockAxios.onGet(API_ENDPOINTS.THREADS.LIST).reply(200, {
        threads: mockThreads,
      })

      // 调用API
      const result = await getSessions()

      // 验证结果 - 应该映射为前端 Session 模型
      expect(result).toHaveLength(2)
      expect(result[0].id).toBe('session-1')
      expect(result[0].title).toBe('测试会话1')
      expect(result[1].id).toBe('session-2')
      expect(result[1].agentId).toBe('agent-1')
    })

    it('应该在网络错误时抛出异常', async () => {
      // 模拟网络错误
      mockAxios.onGet(API_ENDPOINTS.THREADS.LIST).networkError()

      // 验证抛出错误
      await expect(getSessions()).rejects.toThrow()
    })

    it('应该在服务器错误时支持重试', async () => {
      // 第一次请求失败，第二次成功
      mockAxios
        .onGet(API_ENDPOINTS.THREADS.LIST)
        .replyOnce(500)
        .onGet(API_ENDPOINTS.THREADS.LIST)
        .replyOnce(200, { threads: [] })

      // 调用API（启用重试）
      const result = await getSessions({ retry: true, maxRetries: 2 })

      // 验证结果
      expect(result).toEqual([])
    })
  })

  // 注意：createSession 函数已删除，会话只能通过主agent创建
  // 前端不应直接调用API创建会话
  // describe('createSession', () => { ... }) 已移除

  describe('deleteSession', () => {
    it('应该成功删除会话', async () => {
      const sessionId = 'session-to-delete'

      // 模拟API响应
      mockAxios.onDelete(API_ENDPOINTS.THREADS.DELETE(sessionId)).reply(204)

      // 调用API
      await expect(deleteSession(sessionId)).resolves.toBeUndefined()
    })

    it('应该在会话ID为空时抛出验证错误', async () => {
      // 验证空字符串
      await expect(deleteSession('')).rejects.toThrow('会话ID不能为空')

      // 验证空白字符串
      await expect(deleteSession('   ')).rejects.toThrow('会话ID不能为空')
    })

    it('应该在会话不存在时抛出异常', async () => {
      const sessionId = 'non-existent-session'

      // 模拟API错误
      mockAxios.onDelete(API_ENDPOINTS.THREADS.DELETE(sessionId)).reply(404, {
        code: 'NOT_FOUND',
        message: '会话不存在',
      })

      // 验证抛出错误
      await expect(deleteSession(sessionId)).rejects.toThrow()
    })
  })

  describe('getMessages', () => {
    it('应该成功获取会话消息列表', async () => {
      const sessionId = 'session-1'

      // 准备测试数据
      const mockMessages: Message[] = [
        {
          id: 'msg-1',
          sessionId: 'session-1',
          role: 'user',
          content: '你好',
          timestamp: '2024-01-01T00:00:00Z',
        },
        {
          id: 'msg-2',
          sessionId: 'session-1',
          role: 'assistant',
          content: '你好！有什么可以帮助你的吗？',
          timestamp: '2024-01-01T00:00:01Z',
        },
      ]

      // 模拟API响应（新格式：包含 messages、total、has_more）
      mockAxios.onGet(API_ENDPOINTS.MESSAGES.LIST(sessionId)).reply(200, {
        messages: mockMessages,
        total: 2,
        has_more: false,
      })

      // 调用API
      const result = await getMessages(sessionId)

      // 验证结果
      expect(result.messages).toHaveLength(2)
      expect(result.messages[0].role).toBe('user')
      expect(result.messages[1].role).toBe('assistant')
      expect(result.total).toBe(2)
      expect(result.has_more).toBe(false)
    })

    it('应该在会话ID为空时抛出验证错误', async () => {
      // 验证空字符串
      await expect(getMessages('')).rejects.toThrow('会话ID不能为空')
    })

    it('应该在会话不存在时抛出异常', async () => {
      const sessionId = 'non-existent-session'

      // 模拟API错误
      mockAxios.onGet(API_ENDPOINTS.MESSAGES.LIST(sessionId)).reply(404, {
        code: 'NOT_FOUND',
        message: '会话不存在',
      })

      // 验证抛出错误
      await expect(getMessages(sessionId)).rejects.toThrow()
    })
  })

  describe('重试机制', () => {
    it('getSessions应该在启用重试时重试失败的请求', async () => {
      let attemptCount = 0

      // 模拟前两次失败，第三次成功
      mockAxios.onGet(API_ENDPOINTS.THREADS.LIST).reply(() => {
        attemptCount++
        if (attemptCount < 3) {
          return [500, { error: '服务器错误' }]
        }
        return [200, { threads: [] }]
      })

      // 调用API（启用重试）
      const result = await getSessions({
        retry: true,
        maxRetries: 3,
        retryDelay: 10,
      })

      // 验证结果
      expect(result).toEqual([])
      expect(attemptCount).toBe(3)
    })
  })
})
