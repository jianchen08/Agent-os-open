/**
 * AgentManagerPage typeLabels 契约测试（agent_type 展示标签声明下发）
 *
 * 核验「前端零域词表」：agent_type 标签来自页声明 props.typeLabels
 * （agent_manager plugin.json /agents 页），未知类型回退原值——
 * 插件改词表无需动前端，未声明时显示原始 agent_type。
 */

import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AgentManagerPage } from '../AgentManagerPage'
import * as agentsApi from '@/services/api/agents'

const getAgentsMock = vi.mocked(agentsApi.getAgents)

function mockItems(types: string[]) {
  getAgentsMock.mockResolvedValue({
    items: types.map((t, i) => ({
      id: `agent-${i}`,
      name: `Agent${i}`,
      agent_type: t,
      status: 'active' as const,
      model: 'glm-5.3',
    })),
    total: types.length,
  })
}

vi.mock('@/services/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/api/agents')>()),
  getAgents: vi.fn(),
}))

describe('AgentManagerPage — typeLabels 声明下发', () => {
  it('声明命中的类型显示声明标签', async () => {
    mockItems(['main', 'atomic'])
    render(<AgentManagerPage typeLabels={{ main: '主控', atomic: '原子' }} />)
    await waitFor(() => expect(screen.getByText('主控')).toBeInTheDocument())
    expect(screen.getByText('原子')).toBeInTheDocument()
  })

  it('未命中的类型回退原值（不猜词表）', async () => {
    mockItems(['orchestrator'])
    render(<AgentManagerPage typeLabels={{ main: '主控' }} />)
    await waitFor(() => expect(screen.getByText('orchestrator')).toBeInTheDocument())
  })

  it('未下发 typeLabels（无声明 props）→ 全部原值', async () => {
    mockItems(['main', 'sub'])
    render(<AgentManagerPage />)
    await waitFor(() => expect(screen.getByText('main')).toBeInTheDocument())
    expect(screen.getByText('sub')).toBeInTheDocument()
  })
})
