// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * renderIntent 分支缺口补测（branches 冲刺批九，承接原 dshRenderIntent 模块）：
 * rowsToStrings 三种行形态、diffPayload 双字段族回退、searchPayload/webPayload
 * 字段族逐级回退与全过滤、tablePayload 缺省形态、deriveGenericSummary（整体）、
 * deriveCardMeta diff 单文件/空路径分支、renderIntentToBlocks search/web 块、
 * bindings error 源 / args 源、apply* 入口的边界（无 toolName / 无 resultData）。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import {
  addRenderIntent,
  applyDataDrivenIntent,
  applyRenderIntent,
  deriveCardMeta,
  deriveGenericSummary,
  diffPayload,
  formPayload,
  inferRenderIntent,
  loadRenderIntents,
  readPayload,
  renderIntentToBlocks,
  searchPayload,
  tablePayload,
  terminalPayload,
  webPayload,
  type RenderContext,
} from '../renderIntent'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

const ctx = (args: Record<string, unknown>, result: Record<string, unknown>): RenderContext => ({
  args,
  result,
  error: null,
  duration_ms: 10,
})

const intent = (card: string, bindings?: Record<string, string>) => ({ card, bindings }) as never

describe('resolvePath / pickField — 路径解析容错', () => {
  beforeEach(() => loadRenderIntents([]))

  it('bindings 指向 error 源：root 为 null 取值 undefined，回落默认字段族（101/122/125）', () => {
    const p = terminalPayload(
      ctx({ command: 'ls' }, {}),
      intent('terminal', { command: 'error' }),
    )
    expect(p?.command).toBe('ls')
  })

  it('嵌套路径中间节点为 null：返回 undefined 不抛错', () => {
    const p = terminalPayload(
      ctx({ command: { deep: null } }, {}),
      intent('terminal', { command: 'args.command.deep.deeper' }),
    )
    expect(p).toBeNull()
  })
})

describe('readPayload — 缺省与脏行容错', () => {
  it('有 path 但无 lines/content：返回 null（175/179 缺省面）', () => {
    expect(readPayload(ctx({}, { path: 'only-path' }), intent('read'))).toBeNull()
  })

  it('lines 含非对象/缺字段行：过滤 + number/text 缺省兜底（182）', () => {
    const p = readPayload(
      ctx({}, { path: 'dirty.txt', lines: [{ text: 42 }, null, 'junk', { number: 3 }] }),
      intent('read'),
    )
    expect(p?.lines).toEqual([
      { number: 0, text: '42' },
      { number: 3, text: '' },
    ])
  })

  it('content 折行 + lang 透传 + totalLines 缺省取行数', () => {
    const p = readPayload(ctx({ file_path: 'f.py' }, { content: 'a\nb', lang: 'py' }), intent('read'))
    expect(p).toMatchObject({ totalLines: 2, lang: 'py' })
  })
})

describe('diffPayload — 双字段族回退矩阵', () => {
  it('diffs 条目 file_path/old_text/new_text 字段族回退 + 非对象条目过滤（194-198）', () => {
    const p = diffPayload(
      ctx({}, { diffs: [{ file_path: 'f.rs', old_text: 'o', new_text: 'n' }, 'junk', null] }),
      intent('diff'),
    )
    expect(p).toEqual({ diffs: [{ path: 'f.rs', oldText: 'o', newText: 'n' }] })
  })

  it('单文件对：old_content 有 new_content 无 → newText 空、oldText 保留（209/210）', () => {
    const p = diffPayload(ctx({ file_path: 'w' }, { old_content: 'o' }), intent('diff'))
    expect(p).toEqual({ diffs: [{ path: 'w', oldText: 'o', newText: '' }] })
  })

  it('单文件对：new_content 有 old_content 无 → oldText null（新建文件，209）', () => {
    const p = diffPayload(ctx({ file_path: 'w' }, { new_content: 'n' }), intent('diff'))
    expect(p).toEqual({ diffs: [{ path: 'w', oldText: null, newText: 'n' }] })
  })
})

describe('searchPayload — 字段族回退与全过滤', () => {
  it('files 条目 path/file/filename 逐级回退、matches 非数组置空、line_number/text 兜底（225-229）', () => {
    const p = searchPayload(
      ctx({}, {
        files: [
          { file: 'f1' },
          { filename: 'f2', matches: 'nope' },
          { path: 'p3', matches: [{ line_number: 7, text: 'tx' }] },
          { path: 'p4', matches: [{ line: 'bare' }] },
          { matches: [{ lineNumber: 2, line: 'L' }] },
          'junk',
        ],
      }),
      intent('search'),
    )
    expect(p).toMatchObject({ kind: 'matches', total: 3 })
    expect((p as { files: unknown[] }).files).toEqual([
      { path: 'f1', matches: [] },
      { path: 'f2', matches: [] },
      { path: 'p3', matches: [{ lineNumber: 7, line: 'tx' }] },
      { path: 'p4', matches: [{ lineNumber: 0, line: 'bare' }] },
      { path: '', matches: [{ lineNumber: 2, line: 'L' }] },
    ])
  })

  it('files 全无效：落到 paths/results 检查并返回 null（229 缺省面 + 级联）', () => {
    expect(searchPayload(ctx({}, { files: ['x'] }), intent('search'))).toBeNull()
  })

  it('results 条目均无 path：过滤后为空返回 null（245/247）', () => {
    expect(searchPayload(ctx({}, { results: [{ nope: 1 }, 42] }), intent('search'))).toBeNull()
  })
})

describe('webPayload — sources 回退族与 fetch 缺省状态码', () => {
  it('source 条目 link/name/content/date 逐级回退、无 url 条目被过滤（261-267）', () => {
    const p = webPayload(
      ctx({}, {
        sources: [
          { link: 'l1', name: 'n1', content: 'c1', date: 'd1' },
          { url: '' },
          'junk',
        ],
      }),
      intent('web'),
    )
    expect(p).toEqual({
      kind: 'search',
      answer: undefined,
      sources: [{ url: 'l1', title: 'n1', snippet: 'c1', publishedAt: 'd1' }],
      truncated: false,
    })
  })

  it('sources 全被过滤：落到 fetch 分支并返回 null（267 缺省面）', () => {
    expect(webPayload(ctx({}, { sources: [{ url: '' }] }), intent('web'))).toBeNull()
  })

  it('source 无 url/link 键：url 落空串后被过滤（261 空串面）', () => {
    expect(webPayload(ctx({}, { sources: [{ other: 1 }] }), intent('web'))).toBeNull()
  })

  it('fetch 形态无状态码：statusCode 缺省 200（274）', () => {
    expect(webPayload(ctx({ url: 'https://a' }, {}), intent('web'))).toEqual({
      kind: 'fetch', url: 'https://a', statusCode: 200, truncated: false,
    })
  })
})

describe('rowsToStrings / tablePayload — 行形态与缺省', () => {
  it('对象行 → 值列；null 单元 → 空串；标量行 → 整体 null（305-311）', () => {
    const objRows = tablePayload(ctx({}, { d: [{ a: 1, b: 'x' }, { a: null, b: 'y' }] }), intent('table'))
    expect(objRows?.rows).toEqual([['1', 'x'], ['', 'y']])

    expect(tablePayload(ctx({}, { d: ['scalar'] }), intent('table'))).toBeNull()
  })

  it('d 非数组 / d 空数组：返回 null（302/352 缺省面）', () => {
    expect(tablePayload(ctx({}, { d: 'nope' }), intent('table'))).toBeNull()
    expect(tablePayload(ctx({}, { d: [] }), intent('table'))).toBeNull()
  })

  it('表头非全字符串：形态 A 放弃并级联到形态 B（331）', () => {
    const p = tablePayload(ctx({}, { x_h: [1, 2], x_d: [['a']], d: [['z']] }), intent('table'))
    expect(p?.columns).toEqual(['列1'])
    expect(p?.rows).toEqual([['z']])
  })

  it('表头合法但行不可折叠：形态 A 放弃返回 null（333）', () => {
    expect(tablePayload(ctx({}, { y_h: ['a'], y_d: ['scalar'] }), intent('table'))).toBeNull()
  })

  it('items 对象缺列：undefined 单元落空串（344）', () => {
    const p = tablePayload(ctx({}, { items: [{ a: 1, b: undefined }, { a: 2, b: 'x' }] }), intent('table'))
    expect(p?.columns).toEqual(['a', 'b'])
    expect(p?.rows).toEqual([['1', ''], ['2', 'x']])
  })

  it('result 非对象：返回 null（325 缺省面）', () => {
    expect(tablePayload({ args: {}, result: null }, intent('table'))).toBeNull()
  })
})

describe('formPayload — bindings 双源与空值跳过', () => {
  it('bindings 含 args.* 源：从 args 取值（410）', () => {
    const p = formPayload(
      ctx({ cmd: 'ls' }, { ignored: 'x' }),
      intent('form', { command: 'args.cmd' }),
    )
    expect(p?.kvItems).toEqual([{ key: '命令', value: 'ls' }])
  })

  it('binding 路径取不到值：该条目跳过（397）', () => {
    const p = formPayload(
      ctx({}, { status: 'ok' }),
      intent('form', { missing: 'result.not_there', status: 'result.status' }),
    )
    expect(p?.kvItems).toEqual([{ key: '状态', value: 'ok' }])
  })

  it('无 bindings：null 值条目跳过（419/420）、args 侧 root 缺失只收 result（417）', () => {
    const p = formPayload({ args: null, result: { task_id: 't', junk: null, ok: 'y' } }, intent('form') as never)
    expect(p?.kvItems).toEqual([
      { key: '任务ID', value: 't' },
      { key: 'ok', value: 'y' },
    ])
  })
})

describe('deriveGenericSummary — 通用对象摘要（整体缺口）', () => {
  it('按字段族取首个非空字符串，折叠空白为单行', () => {
    expect(deriveGenericSummary(ctx({ path: '  a\n\t b ' }, {}))).toBe('a b')
    expect(deriveGenericSummary(ctx({ path: '   ', query: 'q1' }, {}))).toBe('q1')
  })

  it('args 侧全空后落到 result.task_id；非字符串值跳过', () => {
    expect(deriveGenericSummary(ctx({ path: '', file_path: 42 }, { task_id: 't-9' }))).toBe('t-9')
  })

  it('超长截断到 200 字符并加省略号（SUMMARY_MAX_LEN 边界）', () => {
    const long = 'x'.repeat(250)
    const s = deriveGenericSummary(ctx({ path: long }, {}))
    expect(s?.startsWith('x'.repeat(200))).toBe(true)
    expect(s?.endsWith('…')).toBe(true)
    expect(s).toHaveLength(201)
  })

  it('无任何可摘要字段：返回 undefined', () => {
    expect(deriveGenericSummary(ctx({ command: 'ls' }, { status: 'ok' }))).toBeUndefined()
  })
})

describe('deriveCardMeta — diff 单文件与空路径分支（529-532）', () => {
  const meta = (card: string, args: Record<string, unknown>, result: Record<string, unknown>, bindings?: Record<string, string>) =>
    deriveCardMeta(ctx(args, result), intent(card, bindings))

  it('diffs 条目仅 file_path：摘要与打开入口落在 file_path（524 回退 + 530）', () => {
    expect(meta('diff', {}, { diffs: [{ file_path: 'fp.rs' }] })).toEqual({
      summary: 'fp.rs',
      filePath: 'fp.rs',
    })
  })

  it('diffs 全部无路径：空元信息（532）', () => {
    expect(meta('diff', {}, { diffs: [42, { oldText: 'x' }] })).toEqual({})
  })

  it('read 无路径、terminal/web/search 关键字段缺失：均返回空元信息（516/538/542/546）', () => {
    expect(meta('read', {}, {})).toEqual({})
    expect(meta('file', {}, {})).toEqual({})
    expect(meta('image', {}, {})).toEqual({})
    expect(meta('terminal', {}, {})).toEqual({})
    expect(meta('web', {}, {})).toEqual({})
    expect(meta('search', {}, {})).toEqual({})
  })
})

describe('renderIntentToBlocks — search/web 块（整体缺口）与各卡缺省面', () => {
  it('search paths 形态：默认 label、total 取 paths 长度', () => {
    const blocks = renderIntentToBlocks({ card: 'search' }, ctx({}, { paths: ['a', 'b'] }))
    expect(blocks[0].contentType).toBe('search')
    expect(blocks[0].label).toBe('搜索结果')
    expect(blocks[0].search).toEqual({ kind: 'paths', paths: ['a', 'b'], truncated: false, total: 2 })
  })

  it('search matches 形态：title 覆盖 label', () => {
    const blocks = renderIntentToBlocks(
      { card: 'search', title: '全局搜索' },
      ctx({}, { files: [{ path: 'f', matches: [{ lineNumber: 1, line: 'x' }] }] }),
    )
    expect(blocks[0].label).toBe('全局搜索')
    expect(blocks[0].search).toMatchObject({ kind: 'matches', total: 1 })
  })

  it('web fetch 形态：statusCode 透传', () => {
    const blocks = renderIntentToBlocks({ card: 'web' }, ctx({}, { url: 'https://x', status_code: 201 }))
    expect(blocks[0].label).toBe('网页抓取')
    expect(blocks[0].web).toEqual({ kind: 'fetch', url: 'https://x', statusCode: 201, truncated: false })
  })

  it('web search 形态：answer 可选段缺省不出现', () => {
    const blocks = renderIntentToBlocks({ card: 'web' }, ctx({}, { sources: [{ url: 'u' }] }))
    expect(blocks[0].label).toBe('联网搜索')
    expect(blocks[0].web).toEqual({ kind: 'search', sources: [{ url: 'u', title: undefined, snippet: undefined, publishedAt: undefined }], truncated: false })
    expect('answer' in (blocks[0].web as Record<string, unknown>)).toBe(false)
  })

  it('file 卡无 title：默认 label「文件」；image 卡默认「图片」', () => {
    const file = renderIntentToBlocks({ card: 'file' }, ctx({}, { path: 'a.txt' }))
    expect(file[0].label).toBe('文件')
    const image = renderIntentToBlocks({ card: 'image' }, ctx({}, { file_path: 'a.png' }))
    expect(image[0].label).toBe('图片')
  })

  it('form 卡无 title：默认 label「详情」', () => {
    const blocks = renderIntentToBlocks({ card: 'form' }, ctx({ task_id: 't' }, {}))
    expect(blocks[0].label).toBe('详情')
  })

  it('diff 卡：title 覆盖、空 path 兜底「差异对比」、oldText null → 空串（616/619）', () => {
    const titled = renderIntentToBlocks(
      { card: 'diff', title: 'T' },
      ctx({ file_path: 'w' }, { old_content: 'o', new_content: 'n' }),
    )
    expect(titled[0].label).toBe('T')

    const noPath = renderIntentToBlocks({ card: 'diff' }, ctx({}, { new_content: 'n' }))
    expect(noPath[0].label).toBe('差异对比')
    expect(noPath[0].diffOld).toBe('')
    expect(noPath[0].diffNew).toBe('n')
  })

  it('read 卡：label 取 path、totalLines 折行数、lang 可选段（627/635）', () => {
    const blocks = renderIntentToBlocks({ card: 'read' }, ctx({ file_path: 'f.ts' }, { content: 'a\nb\nc' }))
    expect(blocks[0].label).toBe('f.ts')
    expect(blocks[0].read).toEqual({
      lines: [
        { number: 1, text: 'a' },
        { number: 2, text: 'b' },
        { number: 3, text: 'c' },
      ],
      totalLines: 3,
    })
  })

  it('terminal 卡：cwd/exitCode 可选段按声明出现（645/647 缺省面）', () => {
    const bare = renderIntentToBlocks({ card: 'terminal' }, ctx({ command: 'ls' }, {}))
    expect(bare[0].label).toBe('ls')
    expect(bare[0].terminal).toEqual({ command: 'ls', output: '', running: false })
    expect('exitCode' in (bare[0].terminal as Record<string, unknown>)).toBe(false)
    expect('cwd' in (bare[0].terminal as Record<string, unknown>)).toBe(false)

    const full = renderIntentToBlocks(
      { card: 'terminal' },
      ctx({ command: 'pwd', working_dir: '/tmp' }, { output: '/tmp', exit_code: 0 }),
    )
    expect(full[0].terminal).toMatchObject({ cwd: '/tmp', exitCode: 0 })
  })
})

describe('apply* 入口边界（741/754/755）', () => {
  beforeEach(() => loadRenderIntents([]))

  it('applyRenderIntent：tool_call 但无 toolName → null', () => {
    expect(applyRenderIntent({ type: 'tool_call' } as ActivityData, { tool_args: {} } as MessageToolCall)).toBeNull()
  })

  it('buildRenderContext：无 resultData 时回退 result 字段（754）', () => {
    addRenderIntent('w', { card: 'web' })
    const activity = { type: 'tool_call', toolName: 'w', details: [] } as ActivityData
    const toolCall = { tool_args: {}, result: { url: 'https://r' } } as MessageToolCall
    const out = applyRenderIntent(activity, toolCall)
    expect(out?.details?.[0].contentType).toBe('web')
  })

  it('applyDataDrivenIntent：形状命中 search（数据路由覆盖 search 块）', () => {
    const activity = { type: 'tool_call', toolName: 'g', details: [] } as ActivityData
    const toolCall = { tool_args: { query: 'q' }, resultData: { paths: ['a'] } } as MessageToolCall
    const out = applyDataDrivenIntent(activity, toolCall)
    expect(out?.details?.[0].contentType).toBe('search')
  })

  it('inferRenderIntent：result 为 null 且 args 无特征时安全返回 undefined', () => {
    expect(inferRenderIntent({ args: {}, result: null })).toBeUndefined()
  })
})
