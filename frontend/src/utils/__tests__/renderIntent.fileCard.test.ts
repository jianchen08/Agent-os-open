/** @feature: FP-0.2.四 前端Schema(R174 产物文件卡 render 契约) | @ci: frontend-test */
/**
 * 功能测试：file_card 渲染词汇表（R174：文件类产物卡片化，架构裁决 =
 * 插件 render 契约承载、前端只做通用渲染器，零路径正则/零工具硬编码）。
 *
 * 形态契约（对齐成熟 AI 编码产品的文件卡）：
 * - 折叠态：单行 chip = 文件名（主位）+ 完整路径摘要 + 大小 + +X -Y + 打开入口；
 * - 展开态·新建文件（无基线 old_content）：全文预览；
 * - 展开态·修改已有文件：改前/改后 diff 视图；
 * - 在途（result 无写后内容）：file_card 产不出块 → 级联回后续声明分支。
 */
import { describe, expect, it } from 'vitest'
import {
  applyRenderIntent,
  fileCardPayload,
  loadRenderIntents,
  renderIntentToBlocks,
  buildRenderContext,
  deriveCardMeta,
} from '@/utils/renderIntent'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

function makeActivity(toolName: string): ActivityData {
  return { type: 'tool_call', toolName, title: toolName, status: 'completed' } as ActivityData
}

function makeToolCall(
  tool: string,
  args: Record<string, unknown>,
  result?: Record<string, unknown>,
): MessageToolCall {
  return {
    id: 'tc1',
    tool,
    tool_args: args,
    result,
    containerTaskId: 'task-42',
    status: 'completed',
  } as unknown as MessageToolCall
}

/** R174 形态：file_write 完成态（新建文件） */
const NEW_FILE_RESULT = {
  file: '/w/.ai_workspaces/sessions/t1/calculator.html',
  added: 57,
  removed: 0,
  size: 2389,
  old_content: '',
  new_content: '<html>...</html>',
}

describe('fileCardPayload — 字段族与排除面', () => {
  it('提取路径/大小/增删行/写前写后全文（result.file + result.size 字段族）', () => {
    const payload = fileCardPayload(
      buildRenderContext(makeToolCall('file_write', { path: 'a.html' }, NEW_FILE_RESULT)),
      { card: 'file_card' },
    )
    expect(payload).not.toBeNull()
    expect(payload?.path).toBe('/w/.ai_workspaces/sessions/t1/calculator.html')
    expect(payload?.size).toBe(2389)
    expect(payload?.added).toBe(57)
    expect(payload?.removed).toBe(0)
    expect(payload?.oldText).toBe('')
    expect(payload?.newText).toBe('<html>...</html>')
  })

  it('无文件路径 → null（级联回后续声明分支）', () => {
    const payload = fileCardPayload(buildRenderContext(makeToolCall('x', {}, { a: 1 })), {
      card: 'file_card',
    })
    expect(payload).toBeNull()
  })

  it('http(s) 路径不是文件卡（web 域排除）', () => {
    const payload = fileCardPayload(
      buildRenderContext(makeToolCall('x', { path: 'a' }, { file: 'https://a.com/b.html' })),
      { card: 'file_card' },
    )
    expect(payload).toBeNull()
  })

  it('bindings 覆盖 size 字段族', () => {
    const payload = fileCardPayload(
      buildRenderContext(makeToolCall('x', { path: 'a' }, { file: 'a', bytes: 7 })),
      { card: 'file_card', bindings: { size: 'result.bytes' } },
    )
    expect(payload?.size).toBe(7)
  })
})

describe('renderIntentToBlocks(file_card) — 展开形态：新建预览 vs 修改 diff', () => {
  it('新建文件（old_content 空串，无基线）→ 全文预览 code 块，默认展开', () => {
    const blocks = renderIntentToBlocks(
      { card: 'file_card' },
      buildRenderContext(makeToolCall('file_write', { path: 'a.html' }, NEW_FILE_RESULT)),
    )
    expect(blocks).toHaveLength(1)
    expect(blocks[0].contentType).toBe('code')
    expect(blocks[0].content).toBe('<html>...</html>')
    expect(blocks[0].defaultExpanded).toBe(true)
    expect(blocks[0].label).toBe('calculator.html')
  })

  it('修改已有文件（old_content 非空）→ 改前/改后 diff 块', () => {
    const blocks = renderIntentToBlocks(
      { card: 'file_card' },
      buildRenderContext(
        makeToolCall(
          'file_write',
          { path: 'docs/a.md' },
          { file: 'docs/a.md', old_content: '旧段落', new_content: '新段落' },
        ),
      ),
    )
    expect(blocks).toHaveLength(1)
    expect(blocks[0].contentType).toBe('diff')
    expect(blocks[0].diffOld).toBe('旧段落')
    expect(blocks[0].diffNew).toBe('新段落')
  })

  it('result 无写后内容（在途）→ 无块（级联回后续分支）', () => {
    const blocks = renderIntentToBlocks(
      { card: 'file_card' },
      buildRenderContext(makeToolCall('file_write', { path: 'a.html' }, {})),
    )
    expect(blocks).toHaveLength(0)
    const activity = applyRenderIntent(
      makeActivity('file_write'),
      makeToolCall('file_write', { path: 'a.html' }, {}),
    )
    expect(activity).toBeNull()
  })
})

describe('deriveCardMeta(file_card) — 折叠 chip 元信息', () => {
  it('标题=文件名、摘要=完整路径、大小与增删行数齐备', () => {
    const meta = deriveCardMeta(
      buildRenderContext(makeToolCall('file_write', { path: 'a.html' }, NEW_FILE_RESULT)),
      { card: 'file_card' },
    )
    expect(meta.title).toBe('calculator.html')
    expect(meta.summary).toBe('/w/.ai_workspaces/sessions/t1/calculator.html')
    expect(meta.filePath).toBe('/w/.ai_workspaces/sessions/t1/calculator.html')
    expect(meta.size).toBe(2389)
    expect(meta.diffStat).toEqual({ added: 57, removed: 0 })
  })

  it('result 缺 size/added/removed 时不硬造（undefined 缺省面）', () => {
    const meta = deriveCardMeta(
      buildRenderContext(
        makeToolCall('file_write', { path: 'a.md' }, { file: 'a.md', new_content: 'x' }),
      ),
      { card: 'file_card' },
    )
    expect(meta.size).toBeUndefined()
    expect(meta.diffStat).toBeUndefined()
  })
})

describe('enhanceActivityWithToolConfig — file_card 端到端（折叠 chip 形态）', () => {
  it('声明 file_card → 文件名主位 + 路径摘要 + 打开入口 + 大小/增删徽标 + 文件图标', () => {
    loadRenderIntents([{ name: 'file_write', render: { card: 'file_card' } }])
    const out = enhanceActivityWithToolConfig(
      makeActivity('file_write'),
      makeToolCall('file_write', { path: 'a.html' }, NEW_FILE_RESULT),
    )
    expect(out.title).toBe('calculator.html')
    expect(out.subtitle).toBe('/w/.ai_workspaces/sessions/t1/calculator.html')
    expect(out.filePath).toBe('/w/.ai_workspaces/sessions/t1/calculator.html')
    expect(out.onOpenFile).toBeTypeOf('function')
    expect(out.size).toBe(2389)
    expect(out.diffStat).toEqual({ added: 57, removed: 0 })
    expect(out.customIcon).toBeTruthy()
    expect(out.details?.[0]?.contentType).toBe('code')
  })

  it('声明仍是旧词汇 diff 时行为不变（词汇扩展不回改既有卡）', () => {
    loadRenderIntents([{ name: 'legacy_writer', render: { card: 'diff' } }])
    const out = enhanceActivityWithToolConfig(
      makeActivity('legacy_writer'),
      makeToolCall('legacy_writer', { path: 'a.md' }, { old_content: 'a', new_content: 'b' }),
    )
    expect(out.details?.[0]?.contentType).toBe('diff')
    expect(out.title).toBe('Legacy Writer')
    expect(out.size).toBeUndefined()
  })
})
