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
  applyDataDrivenIntent,
  applyRenderIntent,
  clearRenderIntents,
  deriveCardMeta,
  diffPayload,
  filePayload,
  formPayload,
  getRenderIntent,
  imagePayload,
  inferRenderIntent,
  loadRenderIntents,
  readPayload,
  renderIntentToBlocks,
  searchPayload,
  tablePayload,
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

describe('imagePayload / filePayload（产物路径卡）', () => {
  it('图片扩展名路径 → image 卡（media 产物）', () => {
    const p = imagePayload(ctx({ prompt: 'cat' }, { file_path: 'out/art.png', media_type: 'image' }), intent('image'))
    expect(p).toEqual({ path: 'out/art.png' })
  })

  it('非图片扩展名 → image 卡 null，file 卡命中', () => {
    expect(imagePayload(ctx({}, { path: 'a.mp3' }), intent('image'))).toBeNull()
    expect(filePayload(ctx({}, { path: 'a.mp3' }), intent('file'))).toEqual({ path: 'a.mp3' })
  })

  it('URL 不是文件 → file 卡 null', () => {
    expect(filePayload(ctx({}, { path: 'https://x/y.png' }), intent('file'))).toBeNull()
  })

  it('bindings 覆盖路径字段', () => {
    const p = filePayload(ctx({}, { saved: '/tmp/f.bin' }), intent('file', { path: 'result.saved' }))
    expect(p).toEqual({ path: '/tmp/f.bin' })
  })
})

describe('tablePayload（表头+二维数组）', () => {
  it('形态 A：*_h + *_d 配对（resource_search 表格）', () => {
    const p = tablePayload(
      ctx({ resource_type: 'agent' }, { agent_h: ['ID', '名称'], agent_d: [['a-1', '运维'], ['a-2', '开发']], agent_c: 2 }),
      intent('table'),
    )
    expect(p).toEqual({ columns: ['ID', '名称'], rows: [['a-1', '运维'], ['a-2', '开发']] })
  })

  it('形态 B：result.d 二维数组（task_manage 列表，无表头 → 列1..N）', () => {
    const p = tablePayload(ctx({}, { d: [['t-1', '标题1', 'running'], ['t-2', '标题2', 'completed']], hint: '共 2 条' }), intent('table'))
    expect(p?.columns).toEqual(['列1', '列2', '列3'])
    expect(p?.rows).toEqual([['t-1', '标题1', 'running'], ['t-2', '标题2', 'completed']])
  })

  it('形态 C：items 对象数组（list_directory，列名=key 序）', () => {
    const p = tablePayload(
      ctx({}, { items: [{ name: 'a.py', type: 'file', size: 12 }, { name: 'src', type: 'directory', size: 0 }] }),
      intent('table'),
    )
    expect(p?.columns).toEqual(['name', 'type', 'size'])
    expect(p?.rows).toEqual([['a.py', 'file', '12'], ['src', 'directory', '0']])
  })

  it('无表格形状 → null', () => {
    expect(tablePayload(ctx({}, { title: 'x' }), intent('table'))).toBeNull()
  })
})

describe('formPayload（表单式布局）', () => {
  it('无 bindings：args 标量 + result 标量入 kv，长文本/对象入 json', () => {
    const p = formPayload(
      ctx(
        { goal_title: '实现登录', priority: 5, acceptance_criteria: { file_check: {} } },
        { task_id: 't-1', status: 'running', message: '这是一条很长的话术，用来验证长文本会进入折叠区而不是 kv 平铺。'.repeat(6) },
      ),
      intent('form'),
    )
    expect(p).not.toBeNull()
    const kv = p?.kvItems as { key: string; value: string }[]
    expect(kv).toEqual(expect.arrayContaining([
      { key: '任务目标', value: '实现登录' },
      { key: '优先级', value: '5' },
      { key: '任务ID', value: 't-1' },
      { key: '状态', value: 'running' },
    ]))
    const json = p?.jsonItems as { label: string; content: unknown }[]
    expect(json.map((j) => j.label)).toEqual(expect.arrayContaining(['验收标准', '消息']))
  })

  it('bindings：按声明字段序收集', () => {
    const p = formPayload(
      ctx({ goal_title: 'G', target_id: 'a-1' }, { task_id: 't', status: 'done' }),
      intent('form', { status: 'result.status', 任务ID: 'result.task_id' }),
    )
    expect(p?.kvItems).toEqual([
      { key: '状态', value: 'done' },
      { key: '任务ID', value: 't' },
    ])
  })

  it('空数据 → null', () => {
    expect(formPayload(ctx({}, {}), intent('form'))).toBeNull()
  })
})

describe('inferRenderIntent（数据路由：按形状推断）', () => {
  it('diff 形状（old_content+new_content）→ diff', () => {
    expect(inferRenderIntent(ctx({}, { old_content: 'a', new_content: 'b' }))?.card).toBe('diff')
  })

  it('terminal 形状（args.command）→ terminal', () => {
    expect(inferRenderIntent(ctx({ command: 'ls' }, { stdout: 'x', exit_code: 0 }))?.card).toBe('terminal')
  })

  it('read 形状（content + path）→ read', () => {
    expect(inferRenderIntent(ctx({ file_path: 'f.py' }, { content: 'l1\nl2' }))?.card).toBe('read')
  })

  it('search 形状（results 数组）→ search', () => {
    expect(inferRenderIntent(ctx({}, { results: [{ path: 'a.ts' }] }))?.card).toBe('search')
  })

  it('web 形状（args.url）→ web', () => {
    expect(inferRenderIntent(ctx({ url: 'https://x' }, { status_code: 200 }))?.card).toBe('web')
  })

  it('图片扩展名路径 → image；普通文件路径 → file', () => {
    expect(inferRenderIntent(ctx({}, { file_path: 'out/pic.png' }))?.card).toBe('image')
    expect(inferRenderIntent(ctx({}, { file_path: 'out/data.bin' }))?.card).toBe('file')
  })

  it('表格形状（*_h/*_d）→ table；d 二维数组 → table', () => {
    expect(inferRenderIntent(ctx({}, { tool_h: ['a'], tool_d: [['1']] }))?.card).toBe('table')
    expect(inferRenderIntent(ctx({}, { d: [['t', 'x']] }))?.card).toBe('table')
  })

  it('无形状特征 → undefined（落通用数据渲染）', () => {
    expect(inferRenderIntent(ctx({ goal_title: 'G' }, { task_id: 't', status: 'ok' }))).toBeUndefined()
  })
})

describe('renderIntentToBlocks：新卡产出块形态', () => {
  it('image 卡 → contentType=image + path', () => {
    const blocks = renderIntentToBlocks({ card: 'image', title: '生成图片' }, ctx({}, { file_path: 'a.png' }))
    expect(blocks).toEqual([{ label: '生成图片', content: '', contentType: 'image', path: 'a.png' }])
  })

  it('table 卡 → contentType=table + columns/rows', () => {
    const blocks = renderIntentToBlocks({ card: 'table' }, ctx({}, { tool_h: ['ID'], tool_d: [['1']] }))
    expect(blocks[0].contentType).toBe('table')
    expect(blocks[0].table).toEqual({ columns: ['ID'], rows: [['1']] })
  })

  it('form 卡 → contentType=form + kvItems/jsonItems（默认展开）', () => {
    const blocks = renderIntentToBlocks({ card: 'form', title: '任务详情' }, ctx({ goal_title: 'G' }, { task_id: 't' }))
    expect(blocks[0].contentType).toBe('form')
    expect(blocks[0].kvItems).toContainEqual({ key: '任务ID', value: 't' })
    expect(blocks[0].defaultExpanded).toBe(true)
  })
})

describe('applyDataDrivenIntent（数据路由增强入口）', () => {
  beforeEach(() => clearRenderIntents())

  it('无声明 + 数据有形状 → 按形状路由（diff 数据 → dsh:diff 块）', () => {
    const activity = { type: 'tool_call', toolName: 'w', details: [] } as ActivityData
    const toolCall = { tool_args: { path: 'a.md' }, resultData: { old_content: 'x', new_content: 'y' } } as MessageToolCall
    const out = applyDataDrivenIntent(activity, toolCall)
    expect(out?.details?.[0].contentType).toBe('dsh:diff')
  })

  it('有声明时数据路由不干预（applyRenderIntent 声明优先）', () => {
    addRenderIntent('w', { card: 'terminal' })
    const activity = { type: 'tool_call', toolName: 'w', details: [] } as ActivityData
    const toolCall = { tool_args: {}, resultData: { old_content: 'x', new_content: 'y' } } as MessageToolCall
    // 声明映射失败（terminal 需要 command）→ 返回 null，数据路由由调用方后续处理
    expect(applyRenderIntent(activity, toolCall)).toBeNull()
  })

  it('无形状特征 → null（落 L0 通用渲染）', () => {
    const activity = { type: 'tool_call', toolName: 'w', details: [] } as ActivityData
    const toolCall = { tool_args: { a: 1 }, resultData: { b: 2 } } as MessageToolCall
    expect(applyDataDrivenIntent(activity, toolCall)).toBeNull()
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

describe('deriveCardMeta（条目元信息提取：summary/filePath）', () => {
  const meta = (card: string, args: Record<string, unknown>, result: Record<string, unknown>, bindings?: Record<string, string>) =>
    deriveCardMeta(ctx(args, result), intent(card, bindings))

  it('read/file/image 卡 → summary=路径 + filePath（工作区打开入口）', () => {
    expect(meta('read', { file_path: 'src/a.py' }, { file: 'src/a.py', content: 'x' })).toEqual({
      summary: 'src/a.py',
      filePath: 'src/a.py',
    })
    expect(meta('file', {}, { path: 'out/report.md' })).toEqual({
      summary: 'out/report.md',
      filePath: 'out/report.md',
    })
    expect(meta('image', {}, { path: 'shots/x.png' })).toEqual({
      summary: 'shots/x.png',
      filePath: 'shots/x.png',
    })
  })

  it('read 卡 bindings.path 覆盖字段族', () => {
    expect(meta('read', { f: 'custom.rs' }, {}, { path: 'args.f' })).toEqual({
      summary: 'custom.rs',
      filePath: 'custom.rs',
    })
  })

  it('terminal/web/search 卡 → 仅 summary（命令/URL/查询词）', () => {
    expect(meta('terminal', { command: 'cargo test' }, {})).toEqual({ summary: 'cargo test' })
    expect(meta('web', { url: 'https://a.com' }, {})).toEqual({ summary: 'https://a.com' })
    expect(meta('search', { query: 'foo' }, {})).toEqual({ summary: 'foo' })
  })

  it('diff 卡：单文件对可打开；多文件 diffs 只给摘要；无路径返回空', () => {
    expect(meta('diff', { file_path: 'a.md' }, { old_content: 'x', new_content: 'y' })).toEqual({
      summary: 'a.md',
      filePath: 'a.md',
    })
    expect(meta('diff', {}, { diffs: [{ path: 'a.py' }, { path: 'b.py' }] })).toEqual({
      summary: 'a.py 等 2 个文件',
    })
    expect(meta('diff', {}, { old_content: 'x', new_content: 'y' })).toEqual({})
  })

  it('table/form/generic 卡 → 无元信息', () => {
    expect(meta('form', { a: 1 }, { b: 2 })).toEqual({})
    expect(meta('table', {}, { d: [['x']] })).toEqual({})
    expect(meta('generic', {}, {})).toEqual({})
  })
})
