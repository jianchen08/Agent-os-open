// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * AgentManagerPage 覆盖补测：列表加载失败 / 搜索 / 展开详情 / 编辑入口
 *
 * 既有测试（AgentManagerPage.typeLabels.test.tsx）只覆盖 typeLabels 声明下发，
 * 本文件补组件其余交互面：
 * - 列表加载失败：Error 与非 Error 两种拒绝形态各自的可视文案
 * - 搜索输入：空结果时标题切「没有找到匹配的智能体」（与「暂无智能体」区分）
 * - 刷新按钮：重跑列表请求，请求次数随点击增长
 * - 卡片点击：展开详情 / 再点收起
 * - 编辑按钮：不冒泡触发卡片展开，只打开编辑模态框；模态框关闭后不再渲染
 * - 详情区字段渲染：tool_names / max_iterations / timeout / tags
 *
 * 测试策略：mock 外部边界（agents API 网络层 + apiClient），组件真实渲染，
 * 编辑模态框走真实 AgentConfigModal→FormWidget 链（表单数据由 apiClient 桩供给）。
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as agentsApi from '@/services/api/agents'
import { renderWithProviders } from '@/test/renderWithProviders'
import { AgentManagerPage } from '../AgentManagerPage'
import type { AgentResponse } from '@/services/api/agents'
import type * as agentsApiMod from '@/services/api/agents'

const getAgentsMock = vi.mocked(agentsApi.getAgents)

vi.mock('@/services/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof agentsApiMod>()),
  getAgents: vi.fn(),
}))

// FormWidget 消费 useSessionsQuery（会话隔离形态展示），本文件静态 mock 空列表
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))

// apiClient 网络层打桩（外部依赖：HTTP）——编辑模态框的 schema / config 读取
const apiGet = vi.fn()
const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign((...args: unknown[]) => apiRequest(...args), {
    get: (...args: unknown[]) => apiGet(...args),
  }),
}))

const SCHEMA_URI = '/ext/agent_manager/agents/schema'
const CONFIG_URI = '/ext/agent_manager/agents/agent-1/config'

/** 编辑模态框的字段声明（最小集：够表单渲染 + 保存链） */
const AGENT_FIELDS = [
  { name: 'config_id', type: 'string', label: '配置ID', required: true },
  { name: 'name', type: 'string', label: '名称', required: true },
  { name: 'timeout_seconds', type: 'number', label: '超时秒' },
]

const AGENT_YAML = `config_id: agent-1
name: Agent1
timeout_seconds: 600
`

/** 一条字段齐全的 agent（展开详情四类字段全覆盖） */
const FULL_AGENT: AgentResponse = {
  id: 'agent-1',
  name: 'Agent1',
  description: '主控智能体',
  agent_type: 'main',
  status: 'active',
  model: 'glm-5.3',
  level: 'L1',
  system_prompt: '你是主控',
  tool_names: ['file_read', 'file_write'],
  max_iterations: 120,
  timeout: 900,
  tags: ['core', 'ops'],
  created_at: '2026-01-01T00:00:00.000Z',
}

/** 一条仅必填字段的 agent（详情区可选字段全缺，走空值分支） */
const MINIMAL_AGENT: AgentResponse = {
  id: 'agent-2',
  name: 'Agent2',
  agent_type: 'atomic',
  status: 'inactive',
  model: 'glm-5.3-flash',
  created_at: '2026-01-02T00:00:00.000Z',
}

function mockList(items: AgentResponse[], total = items.length) {
  getAgentsMock.mockResolvedValue({
    items,
    total,
    page: 1,
    page_size: 100,
  })
}

function renderPage() {
  return renderWithProviders(<AgentManagerPage typeLabels={{ main: '主控' }} />)
}

/** 等待列表就绪（用卡片出现替代 loading 等待） */
async function findCard(name: string): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name })
  return heading.closest('[role="listitem"]') as HTMLElement
}

beforeEach(() => {
  vi.resetAllMocks()
  apiGet.mockImplementation((url: string) => {
    if (url === SCHEMA_URI) return Promise.resolve({ data: { fields: AGENT_FIELDS } })
    if (url === CONFIG_URI)
      return Promise.resolve({ data: { config_id: 'agent-1', yaml: AGENT_YAML, etag: 'e1' } })
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
  apiRequest.mockResolvedValue({ data: {} })
})

describe('AgentManagerPage — 列表加载失败', () => {
  it('Error 拒绝 → 展示该错误消息并给重试入口', async () => {
    getAgentsMock.mockRejectedValue(new Error('后端不可达'))
    renderPage()

    expect(await screen.findByText('后端不可达')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    // 失败态不渲染列表容器（错误态优先于空态）
    expect(screen.queryByRole('list', { name: '智能体列表' })).not.toBeInTheDocument()
  })

  it('失败态不伪装空态：不出现「共 0 个智能体」计数，也不出现空态文案（OBS-R258-1）', async () => {
    getAgentsMock.mockRejectedValue(new Error('后端不可达'))
    renderPage()

    await screen.findByText('后端不可达')
    expect(screen.queryByText(/共 0 个智能体/)).not.toBeInTheDocument()
    expect(screen.queryByText('暂无智能体')).not.toBeInTheDocument()
  })

  it('非 Error 拒绝 → 回退通用文案（不展示 [object Object] 类脏文本）', async () => {
    getAgentsMock.mockRejectedValue({ code: 'E_IO' })
    renderPage()

    expect(await screen.findByText('获取 Agent 列表失败')).toBeInTheDocument()
  })

  it('失败后点重试 → 重新请求并渲染成功结果', async () => {
    getAgentsMock.mockRejectedValueOnce(new Error('后端不可达'))
    mockList([FULL_AGENT])
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '重试' }))

    expect(await screen.findByRole('heading', { name: 'Agent1' })).toBeInTheDocument()
    expect(screen.queryByText('后端不可达')).not.toBeInTheDocument()
  })
})

describe('AgentManagerPage — 搜索与刷新', () => {
  it('搜索关键字透传后端；空结果标题切「没有找到匹配的智能体」且不给配置目录提示', async () => {
    mockList([FULL_AGENT])
    renderPage()
    await findCard('Agent1')

    // 第二组输入：换成命中不了的关键字（区分「有结果」与「无结果」两态）
    mockList([], 0)
    fireEvent.change(screen.getByLabelText('搜索智能体'), { target: { value: 'zzz' } })

    expect(await screen.findByText('没有找到匹配的智能体')).toBeInTheDocument()
    expect(screen.queryByText('暂无智能体')).not.toBeInTheDocument()
    expect(screen.queryByText(/config\/agents\//)).not.toBeInTheDocument()
    expect(getAgentsMock).toHaveBeenLastCalledWith({ search: 'zzz', pageSize: 100 })
  })

  it('无关键字且列表为空 → 「暂无智能体」并提示配置目录', async () => {
    mockList([], 0)
    renderPage()

    expect(await screen.findByText('暂无智能体')).toBeInTheDocument()
    expect(screen.getByText('请在 config/agents/ 目录下添加 Agent 配置文件')).toBeInTheDocument()
  })

  it('搜索清空后回到无关键字请求（search 不残留）', async () => {
    mockList([])
    renderPage()
    const search = screen.getByLabelText('搜索智能体')

    fireEvent.change(search, { target: { value: 'abc' } })
    await waitFor(() =>
      expect(getAgentsMock).toHaveBeenLastCalledWith({ search: 'abc', pageSize: 100 }),
    )

    fireEvent.change(search, { target: { value: '' } })
    await waitFor(() =>
      expect(getAgentsMock).toHaveBeenLastCalledWith({ search: undefined, pageSize: 100 }),
    )
  })

  it('刷新按钮 → 再跑一次列表请求（请求次数随点击增长）', async () => {
    mockList([FULL_AGENT])
    renderPage()
    await findCard('Agent1')
    const callsAfterLoad = getAgentsMock.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '刷新智能体列表' }))

    await waitFor(() => expect(getAgentsMock.mock.calls.length).toBe(callsAfterLoad + 1))
  })

  it('总数来自响应而非页长（total 与 items 长度解耦）', async () => {
    mockList([FULL_AGENT], 42)
    renderPage()

    expect(await screen.findByText('共 42 个智能体')).toBeInTheDocument()
  })
})

describe('AgentManagerPage — 展开详情', () => {
  it('点击卡片展开详情：工具 / 最大迭代 / 超时 / 标签 / 等级 / 提示词齐备', async () => {
    mockList([FULL_AGENT])
    renderPage()
    const card = await findCard('Agent1')

    expect(within(card).queryByText('绑定工具：')).not.toBeInTheDocument()
    fireEvent.click(card)

    expect(within(card).getByText('绑定工具：')).toBeInTheDocument()
    expect(within(card).getByText('file_read')).toBeInTheDocument()
    expect(within(card).getByText('file_write')).toBeInTheDocument()
    expect(within(card).getByText('最大迭代：')).toBeInTheDocument()
    expect(within(card).getByText('120')).toBeInTheDocument()
    expect(within(card).getByText('超时：')).toBeInTheDocument()
    expect(within(card).getByText('900s')).toBeInTheDocument()
    expect(within(card).getByText('core')).toBeInTheDocument()
    expect(within(card).getByText('ops')).toBeInTheDocument()
    expect(within(card).getByText('L1')).toBeInTheDocument()
    expect(within(card).getByText('你是主控')).toBeInTheDocument()
  })

  it('再点卡片收起详情（切换语义，非只开不合）', async () => {
    mockList([FULL_AGENT])
    renderPage()
    const card = await findCard('Agent1')

    fireEvent.click(card)
    expect(within(card).getByText('绑定工具：')).toBeInTheDocument()

    fireEvent.click(card)
    expect(within(card).queryByText('绑定工具：')).not.toBeInTheDocument()
  })

  it('可选字段缺失的 agent：详情区不出现工具/迭代/超时/标签行，仅提示词块按需', async () => {
    mockList([MINIMAL_AGENT])
    renderPage()
    const card = await findCard('Agent2')

    fireEvent.click(card)

    expect(within(card).queryByText('绑定工具：')).not.toBeInTheDocument()
    expect(within(card).queryByText('最大迭代：')).not.toBeInTheDocument()
    expect(within(card).queryByText('超时：')).not.toBeInTheDocument()
    expect(within(card).queryByText('系统提示词：')).not.toBeInTheDocument()
    // 描述缺省回退文案
    expect(within(card).getByText('暂无描述')).toBeInTheDocument()
  })

  it('展开态单卡：展开第二张卡不影响第一张（expandedId 是单值）', async () => {
    mockList([FULL_AGENT, MINIMAL_AGENT])
    renderPage()
    const first = await findCard('Agent1')
    const second = await findCard('Agent2')

    fireEvent.click(first)
    expect(within(first).getByText('绑定工具：')).toBeInTheDocument()

    fireEvent.click(second)
    // 同一时刻只允许一张卡展开：第一张收起，第二张展开
    expect(within(first).queryByText('绑定工具：')).not.toBeInTheDocument()
    expect(within(second).getByText('暂无描述')).toBeInTheDocument()
  })

  it('工具/标签为空数组时不渲染空的分组区块', async () => {
    mockList([{ ...FULL_AGENT, tool_names: [], tags: [] }])
    renderPage()
    const card = await findCard('Agent1')

    fireEvent.click(card)

    expect(within(card).queryByText('绑定工具：')).not.toBeInTheDocument()
    // 空 tags 数组同样不产出空行：详情区只剩迭代/超时/提示词
    expect(within(card).getByText('最大迭代：')).toBeInTheDocument()
  })
})

describe('AgentManagerPage — 编辑入口', () => {
  it('点编辑按钮打开编辑模态框（不进卡片展开态，stopPropagation 生效）', async () => {
    mockList([FULL_AGENT])
    renderPage()
    const card = await findCard('Agent1')

    fireEvent.click(screen.getByRole('button', { name: '编辑 Agent1' }))

    expect(await screen.findByText('编辑配置 — Agent1')).toBeInTheDocument()
    // 编辑点击不得冒泡成卡片展开
    expect(within(card).queryByText('绑定工具：')).not.toBeInTheDocument()
  })

  it('关闭编辑模态框 → 模态框及其标题卸载（editingAgent 复位）', async () => {
    mockList([FULL_AGENT])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '编辑 Agent1' }))
    await screen.findByText('编辑配置 — Agent1')

    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    await waitFor(() =>
      expect(screen.queryByText('编辑配置 — Agent1')).not.toBeInTheDocument(),
    )
  })

  it('两张卡各自编辑互不串号（第二张卡打开的是第二张 agent）', async () => {
    mockList([FULL_AGENT, MINIMAL_AGENT])
    renderPage()
    await findCard('Agent2')

    fireEvent.click(screen.getByRole('button', { name: '编辑 Agent2' }))

    expect(await screen.findByText('编辑配置 — Agent2')).toBeInTheDocument()
    expect(screen.queryByText('编辑配置 — Agent1')).not.toBeInTheDocument()
  })
})
