/**
 * ContextUsageWidget 单测：输入框上下文用量指示器的数据消费契约
 *
 * 模型名/上下文窗口/用量三类真值全部出自管道 state 行（/api/v1/pipelines/state）：
 * - 行齐全 → 徽标+圆环+已用/上限，窗口取行内 context_window（不依赖模型注册表）
 * - 行缺 context_window → 回退模型注册表按键查询
 * - 无匹配行 → 不渲染（不显「模型无效」误报）
 */
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => apiGet(...args) },
}))

const getModelsMock = vi.fn()
vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => getModelsMock(...args),
}))

import { useAgentTabStore } from '@/stores/agentTabStore'
import { ContextUsageWidget } from '../ContextUsageWidget'

function makeTab(pipelineRunId: string) {
  return {
    id: `sub-${pipelineRunId}`,
    agentId: 'task-object-id-not-agent-config',
    agentName: '子任务',
    agentLevel: 2 as const,
    taskId: pipelineRunId,
    parentRecordId: pipelineRunId,
    pipelineRunId,
    path: ['主管道', '子任务'],
    status: 'completed' as const,
    hasUnread: false,
    canClose: true,
    messages: [],
  }
}

function stateRow(pipelineId: string, state: Record<string, unknown>) {
  return { pipeline_id: pipelineId, source: 'memory', state }
}

beforeEach(() => {
  apiGet.mockReset()
  getModelsMock.mockReset()
  getModelsMock.mockResolvedValue({ models: {} })
})

afterEach(() => {
  cleanup()
  useAgentTabStore.setState({ tabs: [], activeTabId: null })
})

describe('ContextUsageWidget', () => {
  it('state 行齐全：模型名+圆环+已用/上限，窗口取行内 context_window（注册表无此模型也渲染）', async () => {
    apiGet.mockResolvedValue({
      data: {
        items: [
          stateRow('p1', {
            llm_model: 'MiniMax-M3',
            context_window: 1_000_000,
            'track.llm_usage': { last_input_tokens: 63260, last_output_tokens: 539 },
          }),
        ],
      },
    })
    useAgentTabStore.setState({ tabs: [makeTab('p1')], activeTabId: 'sub-p1' })

    render(<ContextUsageWidget />)
    await waitFor(() => expect(screen.getByTestId('context-usage-indicator').textContent).toContain('MiniMax-M3'))
    // 圆环出现 = 分母（窗口）来自 state.context_window——注册表 mock 为空表
    // （查任何键都 0），仍能渲染即证明窗口取自行内真值
    expect(screen.getByTestId('context-usage-ring')).toBeInTheDocument()
    expect(screen.getByTestId('context-usage-indicator').textContent).toContain('63.3k / 1.0M')
  })

  it('state 行缺 context_window：回退模型注册表查询', async () => {
    apiGet.mockResolvedValue({
      data: {
        items: [
          stateRow('p2', {
            llm_model: 'MiniMax-M3',
            'track.llm_usage': { last_input_tokens: 1000 },
          }),
        ],
      },
    })
    getModelsMock.mockResolvedValue({ models: { 'MiniMax-M3': { context_window: 128_000 } } })
    useAgentTabStore.setState({ tabs: [makeTab('p2')], activeTabId: 'sub-p2' })

    render(<ContextUsageWidget />)
    await waitFor(() => expect(screen.getByTestId('context-usage-ring')).toBeInTheDocument())
    expect(screen.getByTestId('context-usage-indicator').textContent).toContain('1.0k / 128k')
  })

  it('无匹配行：不渲染（既无徽标也无「模型无效」误报）', async () => {
    apiGet.mockResolvedValue({ data: { items: [] } })
    useAgentTabStore.setState({ tabs: [makeTab('p3')], activeTabId: 'sub-p3' })

    render(<ContextUsageWidget />)
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(screen.queryByTestId('context-usage-indicator')).not.toBeInTheDocument()
    expect(screen.queryByTestId('context-usage-invalid')).not.toBeInTheDocument()
  })
})
