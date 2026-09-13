/** @feature FP-0.2.可观测性 ActivityCard 渲染意图路由 @ci frontend-test */
/**
 * render 意图生效链路测试：声明（或翻译产物）→ 意图注册 → 工具结果映射
 * → ActivityCard 落到原生卡片块。
 *
 * 数据形态取自真实 DSH 工具输出（read 的 {path,offset,lines,totalLines} 与
 * glob 的 {root,paths}）——适配器把外部插件包翻译成 render 声明后，走的是
 * 与灵汐自研工具完全相同的一条通道：卡片词汇表 → 原生块，前端不为任何
 * 插件写专属组件。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { addRenderIntent, applyRenderIntent, loadRenderIntents } from '@/utils/renderIntent'
import ActivityCard from '../ActivityCard'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

vi.mock('@/components/approval', () => ({
  TextDiffView: () => null,
}))

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))

vi.mock('@/utils/toolCardRegistry', () => ({
  getGlobalOpenFileCallback: () => () => {},
}))

/** dsh_read 工具的真实结果（DSH read canonical 输出，e2e 实测形态）。 */
const readResultData = {
  path: 'src/app.ts',
  offset: 1,
  lines: [
    { number: 1, text: 'import { x } from "./x"' },
    { number: 2, text: 'export default x' },
  ],
  totalLines: 120,
}

/** dsh_glob 工具的真实结果（DSH glob canonical 输出）。 */
const globResultData = {
  root: '.',
  paths: ['a.ts', 'b/c.ts'],
}

function makeToolCall(resultData: Record<string, unknown>): MessageToolCall {
  return {
    id: 'tc-1',
    tool_name: 'dsh_read',
    tool_args: { file_path: 'src/app.ts' },
    status: 'done',
    resultData,
  } as unknown as MessageToolCall
}

function makeActivity(toolName: string): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-1',
    title: `${toolName} 工具调用`,
    toolName,
    status: 'completed',
  }
}

describe('render 意图生效链路：声明 → 意图注册 → 原生卡片块渲染', () => {
  beforeEach(() => {
    loadRenderIntents([])
  })
  afterEach(() => {
    loadRenderIntents([])
  })

  it('read 卡：注册后工具结果渲染行号视图（含窗口计数）', () => {
    // 第 1 步：翻译产物注册（等价 dshAdapter.loadDshAdapterContributions 的
    // contributes.renderers 兜底通道；plugin.json render 声明通道则不经此步）
    addRenderer('dsh_read', 'read')

    // 第 2 步：意图映射（toolCardRegistry.enhance 的声明路由层同函数）
    const enhanced = applyRenderIntent(makeActivity('dsh_read'), makeToolCall(readResultData))
    expect(enhanced).not.toBeNull()
    expect(enhanced?.details?.[0].contentType).toBe('read')

    // 第 3 步：ActivityCard 渲染（原生卡片块 DOM 锚点）
    render(<ActivityCard activity={enhanced as ActivityData} defaultExpanded />)
    expect(screen.getByText('显示 2 / 120 行')).toBeInTheDocument()
    expect(screen.getByText(/export default x/)).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('search 卡：glob 结果渲染路径平铺 + 计数', () => {
    addRenderer('dsh_read', 'search') // 复用同名工具换卡验证注册表可覆盖
    const enhanced = applyRenderIntent(
      makeActivity('dsh_read'),
      makeToolCall(globResultData),
    )
    expect(enhanced?.details?.[0].contentType).toBe('search')
    render(<ActivityCard activity={enhanced as ActivityData} defaultExpanded />)
    expect(screen.getByText('a.ts')).toBeInTheDocument()
    expect(screen.getByText('2 个路径')).toBeInTheDocument()
  })

  it('未注册意图的工具回落现有级联（无卡片块产生）', () => {
    const out = applyRenderIntent(makeActivity('unknown_tool'), makeToolCall(readResultData))
    expect(out).toBeNull()
  })

  it('失败卡片同样走意图路由渲染（status=failed 不影响渲染形态）', () => {
    addRenderer('dsh_read', 'read')
    const enhanced = applyRenderIntent(makeActivity('dsh_read'), makeToolCall(readResultData))
    const failed = { ...(enhanced as ActivityData), status: 'failed' as const, error: 'boom' }
    render(<ActivityCard activity={failed} />)
    // 默认折叠，展开后内容块可见
    expect(screen.queryByText('显示 2 / 120 行')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('dsh_read 工具调用'))
    expect(screen.getByText('显示 2 / 120 行')).toBeInTheDocument()
  })
})

/** 翻译产物单条注册（等价 dshAdapter 服务对 contributes.renderers 的处理）。 */
function addRenderer(tool: string, card: 'read' | 'search'): void {
  addRenderIntent(tool, { card })
}
