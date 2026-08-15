/** @feature FP-0.2.可观测性 渲染意图路由（DSH 适配） @ci frontend-test */
/**
 * render 意图路由层数据映射单测（task_dsh_plugin_adapter 任务 1d + 3b）。
 *
 * 覆盖：DSH card 词汇表 → 灵汐 ActivityDetailBlock 的纯映射、字段族默认
 * 路径解析（args.x / result.y）、bindings 覆盖、坏声明防御、级联回退
 * （无 render 声明 → applyRenderIntent 返回 null）。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import {
  addRenderIntent,
  applyRenderIntent,
  clearRenderIntents,
  diffPayload,
  getRenderIntent,
  loadRenderIntents,
  readPayload,
  renderIntentToBlocks,
  searchPayload,
  terminalPayload,
  webPayload,
  type RenderContext,
} from '../dshRenderIntent'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

const ctx = (args: Record<string, unknown>, result: Record<string, unknown>): RenderContext => ({
  args,
  result,
  error: null,
  duration_ms: 10,
})

const intent = (card: string, bindings?: Record<string, string>) => ({ card, bindings }) as never

describe('loadRenderIntents / 注册表', () => {
  beforeEach(() => clearRenderIntents())

  it('装载合法声明，忽略缺 card / 坏 card', () => {
    loadRenderIntents([
      { name: 'dsh_bash', render: { card: 'terminal' } },
      { name: 'no_render' },
      { name: 'bad_card', render: { card: 'hologram' } },
      { render: { card: 'diff' } }, // 缺 name
    ])
    expect(getRenderIntent('dsh_bash')?.card).toBe('terminal')
    expect(getRenderIntent('no_render')).toBeUndefined()
    expect(getRenderIntent('bad_card')).toBeUndefined()
  })

  it('bindings 与 title 透传', () => {
    loadRenderIntents([{ name: 'x', render: { card: 'read', bindings: { path: 'args.file' }, title: 'T' } }])
    const got = getRenderIntent('x')
    expect(got?.bindings?.path).toBe('args.file')
    expect(got?.title).toBe('T')
  })
})

describe('terminalPayload（DSH terminal 卡映射）', () => {
  it('DSH canonicalBashResult 字族', () => {
    const p = terminalPayload(
      ctx({ command: 'ls -la' }, { exitCode: 0, stdout: { text: 'a\nb', truncated: false }, stderr: { text: '', truncated: false } }),
      intent('terminal'),
    )
    expect(p).toEqual({ command: 'ls -la', cwd: undefined, output: 'a\nb', exitCode: 0, running: false })
  })

  it('灵汐 bash_execute 字族（output 单串 + exit_code）', () => {
    const p = terminalPayload(
      ctx({ command: 'echo hi', working_dir: '/tmp' }, { status: 'completed', output: 'hi', exit_code: 0 }),
      intent('terminal'),
    )
    expect(p).toMatchObject({ command: 'echo hi', cwd: '/tmp', output: 'hi', exitCode: 0 })
  })

  it('无 command（args 缺）→ null（回退级联）', () => {
    expect(terminalPayload(ctx({}, { output: 'x' }), intent('terminal'))).toBeNull()
  })

  it('bindings 覆盖默认字段族', () => {
    const p = terminalPayload(
      ctx({ cmd: 'run' }, { rc: 3 }),
      intent('terminal', { command: 'args.cmd', exitCode: 'result.rc' }),
    )
    expect(p).toMatchObject({ command: 'run', exitCode: 3 })
  })
})

describe('readPayload（DSH read 卡映射）', () => {
  it('DSH 结构化行（result.lines）直通', () => {
    const p = readPayload(
      ctx({ file_path: '/a/b.ts' }, { path: '/a/b.ts', lines: [{ number: 1, text: 'x' }], totalLines: 100 }),
      intent('read'),
    )
    expect(p).toEqual({ label: '/a/b.ts', lines: [{ number: 1, text: 'x' }], totalLines: 100, lang: undefined })
  })

  it('灵汐 content 字符串 + offset 折行号', () => {
    const p = readPayload(
      ctx({ path: 'f.py' }, { content: 'l1\nl2\nl3', offset: 11 }),
      intent('read'),
    )
    expect(p).toEqual({
      label: 'f.py',
      lines: [
        { number: 11, text: 'l1' },
        { number: 12, text: 'l2' },
        { number: 13, text: 'l3' },
      ],
      totalLines: 3,
      lang: undefined,
    })
  })

  it('无 path/lines → null', () => {
    expect(readPayload(ctx({}, { content: 'x' }), intent('read'))).toBeNull()
  })
})

describe('diffPayload（DSH diff 卡映射）', () => {
  it('DSH diffs 数组（oldText null = 新建文件）', () => {
    const p = diffPayload(
      ctx({}, { diffs: [{ path: 'new.ts', oldText: null, newText: 'a' }] }),
      intent('diff'),
    )
    expect(p).toEqual({ diffs: [{ path: 'new.ts', oldText: null, newText: 'a' }] })
  })

  it('灵汐 file_write 老新对（old_content/new_content）', () => {
    const p = diffPayload(
      ctx({ file_path: 'w.md' }, { old_content: 'x', new_content: 'y' }),
      intent('diff'),
    )
    expect(p).toEqual({ diffs: [{ path: 'w.md', oldText: 'x', newText: 'y' }] })
  })

  it('无 diff 数据 → null', () => {
    expect(diffPayload(ctx({}, {}), intent('diff'))).toBeNull()
  })
})

describe('searchPayload（DSH search 卡映射）', () => {
  it('matches 形态（分组行）', () => {
    const p = searchPayload(
      ctx({}, { files: [{ path: 'a.ts', matches: [{ lineNumber: 3, line: 'hit' }] }] }),
      intent('search'),
    )
    expect(p).toMatchObject({ kind: 'matches', total: 1 })
    expect((p as { files: unknown[] }).files).toHaveLength(1)
  })

  it('paths 形态（DSH glob）', () => {
    const p = searchPayload(ctx({}, { paths: ['a.ts', 'b.ts'] }), intent('search'))
    expect(p).toEqual({ kind: 'paths', paths: ['a.ts', 'b.ts'], truncated: false, total: 2 })
  })

  it('灵汐 results[].path 数组', () => {
    const p = searchPayload(ctx({}, { results: [{ path: 'r1' }, { path: 'r2' }, 'r3'] }), intent('search'))
    expect(p).toMatchObject({ kind: 'paths', paths: ['r1', 'r2', 'r3'] })
  })
})

describe('webPayload（DSH web 卡映射）', () => {
  it('search 形态（sources + answer）', () => {
    const p = webPayload(
      ctx({}, { answer: '42', sources: [{ url: 'https://a', title: 'A', snippet: 's' }] }),
      intent('web'),
    )
    expect(p).toEqual({
      kind: 'search',
      answer: '42',
      sources: [{ url: 'https://a', title: 'A', snippet: 's', publishedAt: undefined }],
      truncated: false,
    })
  })

  it('fetch 形态（url + statusCode）', () => {
    const p = webPayload(ctx({ url: 'https://x' }, { status_code: 200 }), intent('web'))
    expect(p).toEqual({ kind: 'fetch', url: 'https://x', statusCode: 200, truncated: false })
  })

  it('无 url/sources → null', () => {
    expect(webPayload(ctx({}, {}), intent('web'))).toBeNull()
  })
})

describe('renderIntentToBlocks / applyRenderIntent（级联集成）', () => {
  beforeEach(() => clearRenderIntents())

  it('产出 dsh:* 区块，dshProps 携带组件 props', () => {
    const blocks = renderIntentToBlocks(
      { card: 'terminal' },
      ctx({ command: 'pwd' }, { output: '/tmp', exit_code: 0 }),
    )
    expect(blocks).toHaveLength(1)
    expect(blocks[0].contentType).toBe('dsh:terminal')
    expect(blocks[0].dshProps).toMatchObject({ command: 'pwd', output: '/tmp' })
  })

  it('generic 不产区块（保持既有渲染）', () => {
    expect(renderIntentToBlocks({ card: 'generic' }, ctx({}, {}))).toEqual([])
  })

  it('applyRenderIntent：无声明/非 tool_call 返回 null（回退既有级联）', () => {
    const activity = { type: 'tool_call', toolName: 't' } as ActivityData
    const toolCall = { tool_args: {}, resultData: { x: 1 } } as MessageToolCall
    expect(applyRenderIntent(activity, toolCall)).toBeNull()
    addRenderIntent('t', { card: 'generic' })
    expect(applyRenderIntent(activity, toolCall)).toBeNull()
    expect(applyRenderIntent({ type: 'custom' } as ActivityData, toolCall)).toBeNull()
  })

  it('applyRenderIntent：声明 + 数据可映射 → details 被替换', () => {
    addRenderIntent('dsh_read', { card: 'read' })
    const activity = { type: 'tool_call', toolName: 'dsh_read', details: [{ label: 'old', content: 'x' }] } as ActivityData
    const toolCall = {
      tool_args: { file_path: 'a.ts' },
      resultData: { content: 'l1', offset: 1 },
    } as MessageToolCall
    const out = applyRenderIntent(activity, toolCall)
    expect(out).not.toBeNull()
    expect(out?.details).toHaveLength(1)
    expect(out?.details?.[0].contentType).toBe('dsh:read')
    // 原 details 被声明渲染替换
    expect(out?.details?.[0].label).toBe('a.ts')
  })

  it('applyRenderIntent：字段对不上（映射失败）→ null 回退', () => {
    addRenderIntent('dsh_read', { card: 'read' })
    const activity = { type: 'tool_call', toolName: 'dsh_read' } as ActivityData
    const toolCall = { tool_args: {}, resultData: { unrelated: true } } as MessageToolCall
    expect(applyRenderIntent(activity, toolCall)).toBeNull()
  })
})
