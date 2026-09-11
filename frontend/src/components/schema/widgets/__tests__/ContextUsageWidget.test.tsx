/**
 * @feature FP-0.2.可观测性 上下文用量指示器数据消费契约 | @ci frontend-test
 *
 * ContextUsageWidget 单测：输入框上下文用量指示器的数据消费契约
 *
 * 数据源 = 共享 pipelineStates query 缓存（管道 state 唯一真值）：
 * - 行齐全 → 徽标+圆环+已用/上限，窗口取行内 context_window（不依赖模型注册表）
 * - 行缺 context_window → 回退模型注册表按键查询
 * - 无匹配行 → 不渲染（不显「模型无效」误报）
 * - 无匹配行 + 静态兜底 modelName（槽位默认件宿主传入）→ 显示兜底模型名，
 *   无圆环（state 无窗口真值不发明），行内 llm_model 存在时以行内真值优先
 */
import { screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
const getModelsMock = vi.fn()
vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => getModelsMock(...args),
}))
const fetchStatesMock = vi.fn()
vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/api/pipelines')>()),
  fetchPipelineStates: (...args: unknown[]) => fetchStatesMock(...args),
}))
import { useAgentTabStore } from '@/stores/agentTabStore'
import { renderWithProviders } from '@/test/renderWithProviders'
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
  fetchStatesMock.mockReset()
  getModelsMock.mockReset()
  getModelsMock.mockResolvedValue({ models: {} })
})

afterEach(() => {
  cleanup()
  useAgentTabStore.setState({ tabs: [], activeTabId: null })
})

describe('ContextUsageWidget', () => {
  it('state 行齐全：模型名+圆环+已用/上限，窗口取行内 context_window（注册表无此模型也渲染）', async () => {
    fetchStatesMock.mockResolvedValue([
      stateRow('p1', {
        llm_model: 'MiniMax-M3',
        context_window: 1_000_000,
        'track.llm_usage': { last_input_tokens: 63260, last_output_tokens: 539 },
      }),
    ])
    useAgentTabStore.setState({ tabs: [makeTab('p1')], activeTabId: 'sub-p1' })

    renderWithProviders(<ContextUsageWidget />)
    await waitFor(() => expect(screen.getByTestId('context-usage-indicator').textContent).toContain('MiniMax-M3'))
    // 圆环出现 = 分母（窗口）来自 state.context_window——注册表 mock 为空表
    // （查任何键都 0），仍能渲染即证明窗口取自行内真值
    expect(screen.getByTestId('context-usage-ring')).toBeInTheDocument()
    expect(screen.getByTestId('context-usage-indicator').textContent).toContain('63.3k / 1.0M')
  })

  it('state 行缺 context_window：回退模型注册表查询', async () => {
    fetchStatesMock.mockResolvedValue([
      stateRow('p2', {
        llm_model: 'MiniMax-M3',
        'track.llm_usage': { last_input_tokens: 1000 },
      }),
    ])
    getModelsMock.mockResolvedValue({ models: { 'MiniMax-M3': { context_window: 128_000 } } })
    useAgentTabStore.setState({ tabs: [makeTab('p2')], activeTabId: 'sub-p2' })

    renderWithProviders(<ContextUsageWidget />)
    await waitFor(() => expect(screen.getByTestId('context-usage-ring')).toBeInTheDocument())
    expect(screen.getByTestId('context-usage-indicator').textContent).toContain('1.0k / 128k')
  })

  it('无匹配行且无兜底模型名：不渲染（既无徽标也无「模型无效」误报）', async () => {
    fetchStatesMock.mockResolvedValue([])
    useAgentTabStore.setState({ tabs: [makeTab('p3')], activeTabId: 'sub-p3' })

    renderWithProviders(<ContextUsageWidget />)
    await waitFor(() => expect(fetchStatesMock).toHaveBeenCalled())
    expect(screen.queryByTestId('context-usage-indicator')).not.toBeInTheDocument()
    expect(screen.queryByTestId('context-usage-invalid')).not.toBeInTheDocument()
  })

  it('无匹配行 + 静态兜底 modelName：显示兜底模型名；注册表也无窗口真值时无圆环', async () => {
    fetchStatesMock.mockResolvedValue([])
    useAgentTabStore.setState({ tabs: [makeTab('p4')], activeTabId: 'sub-p4' })

    renderWithProviders(<ContextUsageWidget modelName="deepseek-v3" />)
    await waitFor(() => expect(screen.getByTestId('context-usage-indicator')).toBeInTheDocument())
    expect(screen.getByTestId('context-usage-indicator').textContent).toContain('deepseek-v3')
    // state 行缺失 → 无 context_window 真值 → 不渲染圆环与已用/上限
    expect(screen.queryByTestId('context-usage-ring')).not.toBeInTheDocument()
  })

  it('行内 llm_model 优先于静态兜底模型名（state 真值 > 宿主猜测）', async () => {
    fetchStatesMock.mockResolvedValue([
      stateRow('p5', {
        llm_model: 'MiniMax-M3',
        context_window: 1_000_000,
        'track.llm_usage': { last_input_tokens: 10 },
      }),
    ])
    useAgentTabStore.setState({ tabs: [makeTab('p5')], activeTabId: 'sub-p5' })

    renderWithProviders(<ContextUsageWidget modelName="deepseek-v3" />)
    await waitFor(() => expect(screen.getByTestId('context-usage-indicator')).toBeInTheDocument())
    expect(screen.getByTestId('context-usage-indicator').textContent).toContain('MiniMax-M3')
    expect(screen.getByTestId('context-usage-indicator').textContent).not.toContain('deepseek-v3')
  })
})
