/** @feature FP-0.2.可观测性 ActivityCard 渲染意图（DSH 适配） @ci frontend-test */
/**
 * DSH 适配器前端生效链路测试（task_dsh_plugin_adapter 验证补充）。
 *
 * 模拟「网络下载插件」的完整生效路径：
 *   dsh_translate_manifest 翻译产物（renderers，ui-tool npm 包实测形态）
 *   → dshAdapter 服务注册（addRenderIntent）
 *   → 工具结果（MessageToolCall.resultData）按意图映射（applyRenderIntent）
 *   → ActivityCard 的 dsh:* 分支落到 vendor 组件 DOM。
 *
 * 数据形态取自真实 npm 包（@deepseek-ai/dsh-client-ui-tool@0.0.1-rc.1
 * lib/client.js 的 slots.register 键）与 DSH read/glob 工具的 canonical
 * 输出——fixture 与 translator 实测输出一致（见
 * plugins/shared/tools/tests/test_dsh_adapter.py 的 npm fixture）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { addRenderIntent, applyRenderIntent, clearRenderIntents } from '@/utils/dshRenderIntent'
import ActivityCard from '../ActivityCard'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

vi.mock('@/components/approval', () => ({
  TextDiffView: () => null,
}))

vi.mock('@/components/chat/markdown/MarkdownRenderer', () => ({
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

describe('DSH 下载插件生效链路：翻译产物 → render 意图 → vendor 组件渲染', () => {
  beforeEach(() => {
    clearRenderIntents()
  })
  afterEach(() => {
    clearRenderIntents()
  })

  it('read 卡：翻译产物注册后，工具结果渲染 vendor ReadBlock（行号 + 窗口计数）', () => {
    // 第 1 步：翻译产物注册（等价 dshAdapter.loadDshAdapterContributions 的
    // contributes.renderers 兜底通道；plugin.json render 声明通道则不经此步）
    addRenderer('dsh_read', 'read')

    // 第 2 步：意图映射（toolCardRegistry.enhance 的 S3.5 层同函数）
    const enhanced = applyRenderIntent(makeActivity('dsh_read'), makeToolCall(readResultData))
    expect(enhanced).not.toBeNull()
    expect(enhanced?.details?.[0].contentType).toBe('dsh:read')

    // 第 3 步：ActivityCard 渲染（dsh:* 分支 → vendor 组件 DOM 锚点）
    render(<ActivityCard activity={enhanced as ActivityData} defaultExpanded />)
    expect(screen.getByText('显示 2 / 120 行')).toBeInTheDocument()
    expect(screen.getByText(/export default x/)).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('search 卡：glob 结果渲染 vendor SearchBlock（路径平铺 + 计数）', () => {
    addRenderer('dsh_read', 'search') // 复用同名工具换卡验证注册表可覆盖
    const enhanced = applyRenderIntent(
      makeActivity('dsh_read'),
      makeToolCall(globResultData),
    )
    expect(enhanced?.details?.[0].contentType).toBe('dsh:search')
    render(<ActivityCard activity={enhanced as ActivityData} defaultExpanded />)
    expect(screen.getByText('a.ts')).toBeInTheDocument()
    expect(screen.getByText('2 个路径')).toBeInTheDocument()
  })

  it('未注册意图的工具回落现有级联（无 dsh 块产生）', () => {
    const out = applyRenderIntent(makeActivity('unknown_tool'), makeToolCall(readResultData))
    expect(out).toBeNull()
  })

  it('失败卡片同样走 dsh 分支渲染（status=failed 不影响意图路由）', () => {
    addRenderer('dsh_read', 'read')
    const enhanced = applyRenderIntent(makeActivity('dsh_read'), makeToolCall(readResultData))
    const failed = { ...(enhanced as ActivityData), status: 'failed' as const, error: 'boom' }
    render(<ActivityCard activity={failed} />)
    // 默认折叠，展开后 dsh:read 块可见
    expect(screen.queryByText('显示 2 / 120 行')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('dsh_read 工具调用'))
    expect(screen.getByText('显示 2 / 120 行')).toBeInTheDocument()
  })
})

/** 翻译产物单条注册（等价 dshAdapter 服务对 contributes.renderers 的处理）。 */
function addRenderer(tool: string, card: 'read' | 'search'): void {
  addRenderIntent(tool, { card })
}
