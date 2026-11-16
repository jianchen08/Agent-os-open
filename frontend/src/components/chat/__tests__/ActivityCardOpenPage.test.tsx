/** @feature: FP-0.2.工具卡开页 | @ci: frontend-test */
/**
 * 工具卡 open_page 跳转测试（render.open_page 声明 → 按钮 → 回调带 itemId）
 *
 * 声明直读真实 plugins/shared/mode_personal/plugin.json（与生产 /api/v1/schema
 * 同源），防止 fixture 与插件声明镜像漂移。覆盖：
 * 1. 有声明：卡片出现「打开今日总览」入口；点击 → 全局回调收到 (pageId, itemId)
 * 2. 结果无实体 id：点击仍开页（itemId undefined，只开页不定位=合法形态）
 * 3. 无声明：零渲染（无入口、标题不可点）
 * 4. 泛化性自检：另一工具声明 open_page 同样生效（不绑工具名硬编码）
 * 5. 归一化与提取边界：非法 open_page 丢弃；extractPanelItemId 各形态
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '@/components/chat/ActivityCard'
import { toolCallToActivity } from '@/utils/activityConverter'
import { loadRenderIntents } from '@/utils/renderIntent'
import {
  enhanceActivityWithToolConfig,
  extractPanelItemId,
  registerGlobalOpenPanelCallback,
} from '@/utils/toolCardRegistry'
import type { MessageToolCall } from '@/types/models'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))

// ── 声明装载：直读真实 plugin.json（生产 /api/v1/schema 的源头）──
const PLUGIN_JSON_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../../plugins/shared/mode_personal/plugin.json',
)
const personalManifest = JSON.parse(readFileSync(PLUGIN_JSON_PATH, 'utf-8')) as {
  capabilities: { tools: Array<Record<string, unknown>> }
}
const pimDecl = personalManifest.capabilities.tools.find((t) => t.name === 'pim')!

/** 泛化性自检用：另一工具声明（同协议不同页） */
const otherDecl = {
  name: 'other_tool',
  render: { card: 'form', title: '别的工具', open_page: { page: 'other_page', label: '打开别的页' } },
}

function makeToolCall(overrides: Partial<MessageToolCall>): MessageToolCall {
  return {
    call_id: 'tc-1',
    tool_name: 'pim',
    tool_args: { action: 'capture' },
    status: 'completed',
    duration_ms: 8,
    ...overrides,
  } as MessageToolCall
}

/** 渲染一条经真实增强管线的工具卡（converter → enhance → ActivityCard） */
function renderToolCard(toolCall: MessageToolCall) {
  const activity = enhanceActivityWithToolConfig(toolCallToActivity(toolCall), toolCall)
  return render(<ActivityCard activity={activity} defaultExpanded />)
}

const CAPTURE_RESULT = {
  status: 'ok',
  action: 'capture',
  op_id: 'op-9',
  result: { todo: { id: 'todo-42', title: '交周报' } },
}

describe('工具卡 open_page 跳转（render.open_page 声明驱动）', () => {
  let openPanel: ReturnType<typeof vi.fn>

  beforeEach(() => {
    openPanel = vi.fn()
    registerGlobalOpenPanelCallback(openPanel)
    loadRenderIntents([pimDecl as never])
  })

  afterEach(() => {
    loadRenderIntents([])
    vi.restoreAllMocks()
  })

  it('有声明：卡片出现入口，点击 → 回调收到 (pim_today, todo-42)', () => {
    renderToolCard(makeToolCall({ resultData: CAPTURE_RESULT }))

    const entry = screen.getByRole('button', { name: /打开今日总览/ })
    fireEvent.click(entry)

    expect(openPanel).toHaveBeenCalledTimes(1)
    expect(openPanel).toHaveBeenCalledWith('pim_today', 'todo-42')
  })

  it('结果无实体 id：点击仍开页（itemId undefined，只开页不定位）', () => {
    renderToolCard(makeToolCall({ resultData: { status: 'ok', action: 'query' } }))

    fireEvent.click(screen.getByRole('button', { name: /打开今日总览/ }))

    expect(openPanel).toHaveBeenCalledWith('pim_today', undefined)
  })

  it('无声明：零渲染（无入口按钮、标题不可点）', () => {
    // 换一个未声明 open_page 的工具（同 render.card 形态）
    loadRenderIntents([{ name: 'plain_tool', render: { card: 'form' } } as never])
    renderToolCard(makeToolCall({ tool_name: 'plain_tool', resultData: CAPTURE_RESULT }))

    expect(screen.queryByRole('button', { name: /打开今日总览/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /打开页面/ })).toBeNull()
  })

  it('泛化性自检：另一工具声明 open_page 同样生效（不绑工具名）', () => {
    loadRenderIntents([pimDecl as never, otherDecl as never])
    renderToolCard(
      makeToolCall({ tool_name: 'other_tool', resultData: { record: { id: 'rec-7' } } }),
    )

    fireEvent.click(screen.getByRole('button', { name: /打开别的页/ }))

    expect(openPanel).toHaveBeenCalledWith('other_page', 'rec-7')
  })
})

describe('open_page 归一化与条目 id 提取边界', () => {
  afterEach(() => {
    loadRenderIntents([])
  })

  it('非法 open_page（page 缺失/空串/非对象）整字段丢弃，不影响 card 路由', () => {
    loadRenderIntents([
      { name: 'bad1', render: { card: 'form', open_page: { label: '无 page' } } } as never,
      { name: 'bad2', render: { card: 'form', open_page: { page: '  ' } } } as never,
      { name: 'bad3', render: { card: 'form', open_page: 'not-object' } } as never,
    ])
    const openPanel = vi.fn()
    registerGlobalOpenPanelCallback(openPanel)
    for (const name of ['bad1', 'bad2', 'bad3']) {
      renderToolCard(makeToolCall({ tool_name: name, resultData: CAPTURE_RESULT }))
      expect(screen.queryByRole('button', { name: /打开/ })).toBeNull()
    }
    expect(openPanel).not.toHaveBeenCalled()
  })

  it('extractPanelItemId：resultData 优先、路径族（todo/record/item/id）、字符串 JSON 回退', () => {
    // resultData.todo.id 优先于 result 文本
    expect(
      extractPanelItemId(
        makeToolCall({ resultData: CAPTURE_RESULT, result: JSON.stringify({ id: 'fallback' }) }),
      ),
    ).toBe('todo-42')
    // record.id / item.id / 顶层 id
    expect(extractPanelItemId(makeToolCall({ resultData: { record: { id: 'r1' } } }))).toBe('r1')
    expect(extractPanelItemId(makeToolCall({ resultData: { item: { id: 'i1' } } }))).toBe('i1')
    expect(extractPanelItemId(makeToolCall({ resultData: { id: 'top1' } }))).toBe('top1')
    // resultData 缺失 → result 字符串 JSON
    expect(
      extractPanelItemId(makeToolCall({ result: JSON.stringify({ todo: { id: 's1' } }) })),
    ).toBe('s1')
    // 无可提取（空对象/坏 JSON/纯文本）→ undefined
    expect(extractPanelItemId(makeToolCall({ resultData: {}, result: 'plain' }))).toBeUndefined()
  })
})
