/**
 * 执行图API服务测试
 */

import MockAdapter from 'axios-mock-adapter'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { API_ENDPOINTS } from '@/services/api/../../constants/api'
import apiClient from '@/services/api/client'
import { getGraph } from '@/services/api/graph'
import type { GraphData } from '@/services/api/../../types/graph'
import type { ThreadDetailResponse } from '@/services/api/../../utils/mappers'

describe('执行图API服务', () => {
  let mock: MockAdapter

  beforeEach(() => {
    mock = new MockAdapter(apiClient)
  })

  afterEach(() => {
    mock.reset()
  })

  describe('getGraph', () => {
    const sessionId = 'session-123'

    // 后端返回的线程详情格式（包含 execution_graph）
    const mockThreadDetail: ThreadDetailResponse = {
      thread_id: sessionId,
      current_state: 'active',
      intent: '测试会话',
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
      execution_graph: {
        nodes: [
          {
            id: 'node-1',
            label: '任务节点1',
            status: 'completed',
            type: 'task',
            description: '这是一个任务节点',
          },
          {
            id: 'node-2',
            label: '工具节点1',
            status: 'running',
            type: 'tool',
          },
        ],
        edges: [
          {
            id: 'edge-1',
            source: 'node-1',
            target: 'node-2',
            label: '连接',
          },
        ],
      },
    }

    // 期望的前端 GraphData 格式（经过 mapThreadDetailToGraph 转换）

    const _unusedGraphData: GraphData = {
      nodes: [
        {
          id: 'node-1',
          type: 'task',
          data: {
            label: '任务节点1',
            status: 'completed',
            description: '这是一个任务节点',
          },
          position: { x: 0, y: 0 },
        },
        {
          id: 'node-2',
          type: 'tool',
          data: {
            label: '工具节点1',
            status: 'running',
          },
          position: { x: 0, y: 0 },
        },
      ],
      edges: [
        {
          id: 'edge-1',
          source: 'node-1',
          target: 'node-2',
          label: '连接',
        },
      ],
    }

    it('应该成功获取执行图数据', async () => {
      // 模拟API响应（线程详情端点）
      mock.onGet(API_ENDPOINTS.THREADS.GET(sessionId)).reply(200, mockThreadDetail)

      // 调用API
      const result = await getGraph(sessionId)

      // 验证结果（经过映射转换）
      expect(result.nodes).toHaveLength(2)
      expect(result.edges).toHaveLength(1)
      expect(result.nodes[0].id).toBe('node-1')
      expect(result.nodes[0].type).toBe('task')
      expect(result.nodes[0].data.status).toBe('completed')
    })

    it('应该在会话ID为空时抛出验证错误', async () => {
      await expect(getGraph('')).rejects.toThrow('会话ID不能为空')
      await expect(getGraph('   ')).rejects.toThrow('会话ID不能为空')
    })

    it('应该在API返回404时抛出错误', async () => {
      mock.onGet(API_ENDPOINTS.THREADS.GET(sessionId)).reply(404, {
        message: '会话不存在',
      })

      await expect(getGraph(sessionId)).rejects.toThrow()
    })

    it('应该在API返回500时抛出错误', async () => {
      mock.onGet(API_ENDPOINTS.THREADS.GET(sessionId)).reply(500, {
        message: '服务器错误',
      })

      await expect(getGraph(sessionId)).rejects.toThrow()
    })

    it('应该在网络错误时支持重试', async () => {
      let callCount = 0
      mock.onGet(API_ENDPOINTS.THREADS.GET(sessionId)).reply(() => {
        callCount++
        if (callCount < 3) {
          return [500, { message: '服务器错误' }]
        }
        return [200, mockThreadDetail]
      })

      const result = await getGraph(sessionId, { retry: true, maxRetries: 3 })

      expect(result.nodes).toHaveLength(2)
      expect(callCount).toBe(3)
    })

    it('应该正确处理空的执行图', async () => {
      const emptyThreadDetail: ThreadDetailResponse = {
        ...mockThreadDetail,
        execution_graph: {
          nodes: [],
          edges: [],
        },
      }

      mock.onGet(API_ENDPOINTS.THREADS.GET(sessionId)).reply(200, emptyThreadDetail)

      const result = await getGraph(sessionId)

      expect(result.nodes).toHaveLength(0)
      expect(result.edges).toHaveLength(0)
    })

    it('应该正确处理包含所有节点类型的执行图', async () => {
      const complexThreadDetail: ThreadDetailResponse = {
        ...mockThreadDetail,
        execution_graph: {
          nodes: [
            { id: 'task-1', label: '任务', status: 'completed', type: 'task' },
            { id: 'tool-1', label: '工具', status: 'running', type: 'tool' },
            {
              id: 'decision-1',
              label: '决策',
              status: 'pending',
              type: 'decision',
            },
          ],
          edges: [
            { id: 'e1', source: 'task-1', target: 'tool-1' },
            { id: 'e2', source: 'tool-1', target: 'decision-1' },
          ],
        },
      }

      mock.onGet(API_ENDPOINTS.THREADS.GET(sessionId)).reply(200, complexThreadDetail)

      const result = await getGraph(sessionId)

      expect(result.nodes).toHaveLength(3)
      expect(result.nodes.map((n) => n.type)).toEqual(['task', 'tool', 'decision'])
    })

    it('应该正确处理包含详细节点数据的执行图', async () => {
      const detailedThreadDetail: ThreadDetailResponse = {
        ...mockThreadDetail,
        execution_graph: {
          nodes: [
            {
              id: 'node-1',
              label: '详细任务',
              status: 'completed',
              type: 'task',
              description: '任务描述',
              input: { param1: 'value1' },
              output: { result: 'success' },
              logs: ['日志1', '日志2'],
            },
          ],
          edges: [],
        },
      }

      mock.onGet(API_ENDPOINTS.THREADS.GET(sessionId)).reply(200, detailedThreadDetail)

      const result = await getGraph(sessionId)

      expect(result.nodes[0].data.description).toBe('任务描述')
      expect(result.nodes[0].data.input).toEqual({ param1: 'value1' })
      expect(result.nodes[0].data.output).toEqual({ result: 'success' })
      expect(result.nodes[0].data.logs).toEqual(['日志1', '日志2'])
    })

    it('应该正确处理没有 execution_graph 的线程详情', async () => {
      const noGraphThreadDetail: ThreadDetailResponse = {
        thread_id: sessionId,
        current_state: 'active',
        intent: '测试会话',
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-01-01T00:00:00Z',
        // 没有 execution_graph 字段
      }

      mock.onGet(API_ENDPOINTS.THREADS.GET(sessionId)).reply(200, noGraphThreadDetail)

      const result = await getGraph(sessionId)

      expect(result.nodes).toHaveLength(0)
      expect(result.edges).toHaveLength(0)
    })
  })
})
