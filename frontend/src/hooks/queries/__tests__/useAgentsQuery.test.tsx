// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * useAgentsQuery — agents 查询映射/去重与失败上报测试
 *
 * 契约：
 * - 后端 snake_case 列表 → 前端 Agent 映射；id 缺省回退 config_id
 * - 按 config_id 去重（内核遍历 config/agents/** 不去重，同 config_id 只留第一条），
 *   避免 SessionEditModal 渲染 option 时 key 重复；缺 configId 时按 id 去重，
 *   两者皆缺的条目保留（不因无法定键而丢数据）
 * - 请求失败：reportError 上报（组件/操作上下文）+ 错误继续上抛（query 进 error 态）
 *
 * 服务端状态唯一真值源 = queryClient 缓存（queryKeys.agents），经 readAgents 公共读面断言。
 */
import { QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

const getAgentsMock = vi.hoisted(() => vi.fn())
const reportErrorMock = vi.hoisted(() => vi.fn())

vi.mock('@/services/api/agents', () => ({ getAgents: getAgentsMock }))
vi.mock('@/services/errorReporting', () => ({
  ErrorType: { SERVER: 'server', NETWORK: 'network', CLIENT: 'client' },
  reportError: reportErrorMock,
}))

import { readAgents, useAgentsQuery } from '../useAgentsQuery'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

/** 后端 AgentResponse 形态（snake_case） */
function rawAgent(overrides: Record<string, unknown>) {
  return {
    id: '',
    name: '',
    description: '',
    agent_type: 'atomic',
    status: 'active',
    model: 'm',
    created_at: '2026-09-01T00:00:00Z',
    ...overrides,
  }
}

async function fetchList(items: unknown[]) {
  getAgentsMock.mockResolvedValue({ items, total: items.length, page: 1, page_size: 20 })
  const { result } = renderHook(() => useAgentsQuery(), { wrapper })
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  return readAgents()
}

beforeEach(() => {
  vi.resetAllMocks()
  queryClient.clear()
})

describe('mapAndDedupeAgents — 映射与去重（经查询缓存可观察结果）', () => {
  it('同 config_id 只保留第一条（顺序靠前者胜出）', async () => {
    const agents = await fetchList([
      rawAgent({ id: 'a1', config_id: 'agentos', name: '主 Agent' }),
      rawAgent({ id: 'a2', config_id: 'agentos', name: '同 config 的第二份 YAML' }),
      rawAgent({ id: 'b1', config_id: 'reviewer', name: '评审' }),
    ])

    expect(agents).toHaveLength(2)
    expect(agents[0].name).toBe('主 Agent')
    expect(agents[1].name).toBe('评审')
    // 性质断言：输出的 configId 集合无重复
    const ids = agents.map((a) => a.configId)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('缺 config_id → id 兜底做去重键；id 也缺 → 条目保留（不因无法定键丢数据）', async () => {
    const agents = await fetchList([
      rawAgent({ id: 'x1', name: '无 configId 甲' }),
      rawAgent({ id: 'x1', name: '无 configId 重复 id' }),
      rawAgent({ id: '', config_id: '', name: '两键皆缺' }),
    ])

    expect(agents.map((a) => a.name)).toEqual(['无 configId 甲', '两键皆缺'])
    // 两键皆缺的条目 id 落空串，但仍在结果集里
    expect(agents[1].id).toBe('')
  })

  it('id 缺省时映射回退 config_id（Agent.id 取 configId）', async () => {
    const agents = await fetchList([
      rawAgent({ id: '', config_id: 'fallback-cfg', name: '回退' }),
    ])

    expect(agents).toHaveLength(1)
    expect(agents[0].id).toBe('fallback-cfg')
  })
})

describe('fetchAgentsMapped — 请求失败上报并上抛', () => {
  it('Error 失败：reportError 带消息与定位上下文，query 进 error 态', async () => {
    getAgentsMock.mockRejectedValue(new Error('后端 500'))
    const { result } = renderHook(() => useAgentsQuery(), { wrapper })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(reportErrorMock).toHaveBeenCalledWith(
      '后端 500',
      expect.objectContaining({
        type: 'server',
        componentName: 'useAgentsQuery',
        operation: 'fetchAgents',
      }),
    )
    // 失败不得写缓存（readAgents 保持空数组）
    expect(readAgents()).toEqual([])
  })

  it('非 Error 抛出（普通对象）：回退通用文案上报', async () => {
    getAgentsMock.mockRejectedValue({ status: 503 })
    const { result } = renderHook(() => useAgentsQuery(), { wrapper })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(reportErrorMock).toHaveBeenCalledWith(
      '获取 Agent 列表失败',
      expect.objectContaining({ operation: 'fetchAgents' }),
    )
  })

  it('成功后缓存可取（queryKeys.agents 真值源）', async () => {
    const agents = await fetchList([rawAgent({ id: 'ok', config_id: 'ok', name: '可用' })])

    expect(agents[0].name).toBe('可用')
    expect(queryClient.getQueryData(queryKeys.agents)).toEqual(agents)
    expect(reportErrorMock).not.toHaveBeenCalled()
  })
})
