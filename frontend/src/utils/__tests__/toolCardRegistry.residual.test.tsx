/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * toolCardRegistry 残余分支补测（簇3）
 *
 * 覆盖既有 *.test.ts / .declared / .renderMeta / .diff 未触达的分支：
 * - 全局回调缺省兜底：未注册文件打开回调时的 warn 兜底、未注册图片预览回调时的
 *   协议白名单兜底（安全相对路径放行 / 危险协议拒绝）
 * - 非 tool_call 类型 activity 的早退（声明级联不介入）
 * - L0 推断（无任何声明时按数据类型分块）：URL→link、图片路径→image、
 *   文件路径键→file、partialOutput→log、长文本→code
 * - looksLikePath 的两类命中形态（路径分隔符族 / 扩展名族）
 * - safeParseResult 非字符串非对象入参（number/boolean）→ null
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - toolCardRegistry.tsx 的 `safeParseResult` 第二个 catch（Python dict 解析再失败）
 *   在两段 try 后仍落到 `return null`，属解析失败的正常出口，非防御死代码；
 *   本文件以非字符串入参覆盖 `return null` 出口，两段 catch 由既有测试覆盖。
 * - `looksLikePath` 无独立导出，仅经 inferDefaultDetails → pushInferredBlocks 触达，
 *   故以 L0 推断用例间接驱动（断行为：块类型与 path 值）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearChatCardDeclarations, loadChatCardDeclarations } from '@/utils/chatCardInterpreter'
import { loadOutputSchemas } from '@/utils/outputSchemaView'
import { loadRenderIntents } from '@/utils/renderIntent'
import {
  enhanceActivityWithToolConfig,
  getGlobalImagePreviewCallback,
  getGlobalOpenFileCallback,
  registerGlobalImagePreviewCallback,
  safeParseResult,
} from '@/utils/toolCardRegistry'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

afterEach(() => {
  clearChatCardDeclarations()
  loadRenderIntents([])
  loadOutputSchemas([])
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function makeActivity(toolName: string, type: ActivityData['type'] = 'tool_call'): ActivityData {
  return { type, toolName, title: toolName, status: 'completed' } as ActivityData
}

function makeToolCall(over: Partial<MessageToolCall> = {}): MessageToolCall {
  return {
    id: 'tc1',
    tool: 'plain_tool',
    tool_args: {},
    status: 'completed',
    ...over,
  } as unknown as MessageToolCall
}

describe('全局回调缺省兜底', () => {
  it('未注册文件打开回调 → 兜底只 warn，不抛错（宿主可在启动前渲染卡片）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    // 本测试文件未调用 registerGlobalOpenFileCallback，模块内仍为 null
    expect(() => getGlobalOpenFileCallback()('src/a.py', 'task-9')).not.toThrow()
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('未注册文件打开回调')
  })
})

describe('图片预览回调注册语义', () => {
  it('注册自定义回调优先于兜底；传 null 恢复兜底行为', () => {
    const custom = vi.fn()
    registerGlobalImagePreviewCallback(custom)
    getGlobalImagePreviewCallback()('https://cdn.example.com/a.png')
    expect(custom).toHaveBeenCalledWith('https://cdn.example.com/a.png')

    // 传 null 撤销注册 → 回到兜底路径（安全 src 走 window.open）
    const open = vi.fn()
    vi.stubGlobal('open', open)
    registerGlobalImagePreviewCallback(null)
    getGlobalImagePreviewCallback()('https://cdn.example.com/b.png')
    expect(open).toHaveBeenCalledWith('https://cdn.example.com/b.png', '_blank', 'noopener,noreferrer')
    expect(custom).toHaveBeenCalledTimes(1)
  })
})

describe('chat_card 声明：filePathSource 注入打开入口', () => {
  it('filePathSource 求值非空 → 注入 filePath 与 onOpenFile（宿主回调优先，透传 record taskId）', () => {
    loadRenderIntents([])
    loadOutputSchemas([])
    loadChatCardDeclarations([
      { name: 'declare_residual_tool', ui: { chat_card: { filePathSource: 'args.target' } } },
    ])
    const hostOpen = vi.fn()

    const activity = enhanceActivityWithToolConfig(
      makeActivity('declare_residual_tool'),
      makeToolCall({
        tool: 'declare_residual_tool',
        tool_args: { target: 'out/report.md' },
        containerTaskId: 'task-77',
      }),
      { onOpenFile: hostOpen },
    )

    expect(activity.filePath).toBe('out/report.md')
    expect(activity.onOpenFile).toBeTypeOf('function')
    activity.onOpenFile?.('out/report.md')
    // options.onOpenFile 优先生效，record 的 containerTaskId 一并透传
    expect(hostOpen).toHaveBeenCalledWith('out/report.md', 'task-77')
  })

  it('filePathSource 求值为空/缺失 → 不注入打开入口（按钮不出现）', () => {
    loadRenderIntents([])
    loadOutputSchemas([])
    loadChatCardDeclarations([
      { name: 'declare_residual_tool', ui: { chat_card: { filePathSource: 'args.target' } } },
    ])

    const activity = enhanceActivityWithToolConfig(
      makeActivity('declare_residual_tool'),
      makeToolCall({ tool: 'declare_residual_tool', tool_args: {} }),
    )

    expect(activity.filePath).toBeUndefined()
    expect(activity.onOpenFile).toBeUndefined()
  })
})

describe('工具契约结构化视图（output_schema，无显式声明时）', () => {
  const schema = {
    type: 'object',
    properties: {
      text: { type: 'string', title: '文本' },
      score: { type: 'number', title: '分数' },
    },
    required: ['text'],
  }

  it('有 output_schema 且结果合规 → 契约块 + L0 详情，标题人性化，无违约标警', () => {
    loadRenderIntents([])
    loadChatCardDeclarations([])
    loadOutputSchemas([{ name: 'contract_residual_tool', output_schema: schema }])

    const out = enhanceActivityWithToolConfig(
      makeActivity('contract_residual_tool'),
      makeToolCall({
        tool: 'contract_residual_tool',
        tool_args: { q: 'x' },
        resultData: { text: 'hello', score: 1 },
      }),
    )

    expect(out.title).toBe('Contract Residual Tool')
    expect(out.details?.[0].contentType).toBe('form')
    expect(out.error).toBeUndefined()
  })

  it('结果违反契约（缺必填/类型不符）→ error 标注违约明细（fail-closed 前端镜像）', () => {
    loadRenderIntents([])
    loadChatCardDeclarations([])
    loadOutputSchemas([{ name: 'contract_residual_tool', output_schema: schema }])

    const out = enhanceActivityWithToolConfig(
      makeActivity('contract_residual_tool'),
      makeToolCall({
        tool: 'contract_residual_tool',
        tool_args: {},
        resultData: { score: 'not-a-number' },
      }),
    )

    expect(out.details?.[0].contentType).toBe('form')
    expect(out.error).toContain('output_schema 契约校验（前端镜像）')
    expect(out.error).toContain('text')
  })

  it('无 output_schema 声明的工具不走契约分支（回落数据路由/L0）', () => {
    loadRenderIntents([])
    loadChatCardDeclarations([])
    loadOutputSchemas([])

    const out = enhanceActivityWithToolConfig(
      makeActivity('contract_residual_tool'),
      makeToolCall({ tool: 'contract_residual_tool', tool_args: { a: 'v' } }),
    )
    expect(out.error).toBeUndefined()
    expect(out.details).toBeDefined()
  })
})

describe('图片预览回调兜底（open_url 同协议白名单）', () => {
  it('安全 src（http/https、应用内相对路径）→ 新标签打开', () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    const preview = getGlobalImagePreviewCallback()

    preview('https://cdn.example.com/a.png')
    preview('/workspace/files/a.png')

    expect(open).toHaveBeenNthCalledWith(1, 'https://cdn.example.com/a.png', '_blank', 'noopener,noreferrer')
    expect(open).toHaveBeenNthCalledWith(2, '/workspace/files/a.png', '_blank', 'noopener,noreferrer')
  })

  it.each([
    ['javascript:alert(1)'],
    ['data:text/html;base64,PHNjcmlwdD4='],
    ['file:///C:/Windows/System32'],
  ])('危险协议 %s → 不打开（插件输出为不可信面）', (src) => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    getGlobalImagePreviewCallback()(src)
    expect(open).not.toHaveBeenCalled()
  })
})

describe('声明级联早退', () => {
  it.each(['message', 'thinking', 'system'] as const)(
    '非 tool_call 类型（%s）→ 原样返回，不做声明/推断增强',
    (type) => {
      const activity = makeActivity('file_read', type)
      const out = enhanceActivityWithToolConfig(activity, makeToolCall())
      expect(out).toBe(activity)
      expect(out.title).toBe('file_read')
      expect(out.details).toBeUndefined()
    },
  )

  it('tool_call 但无 toolName → 原样返回', () => {
    const activity = { type: 'tool_call', title: 'x', status: 'completed' } as ActivityData
    const out = enhanceActivityWithToolConfig(activity, makeToolCall())
    expect(out).toBe(activity)
  })
})

describe('L0 推断：按数据类型分块', () => {
  /** 无任何声明 + 数据形状不匹配既有 render 意图 → 落 L0 */
  function l0(over: Partial<MessageToolCall>) {
    loadRenderIntents([])
    loadChatCardDeclarations([])
    loadOutputSchemas([])
    return enhanceActivityWithToolConfig(
      makeActivity('custom_infer_tool'),
      makeToolCall(over),
    )
  }

  it('URL 字符串 → link 块（url 原样）；图片路径 → image 块；文件路径键 → file 块', () => {
    const out = l0({
      tool_args: { page_url: 'https://example.com/docs', shot: '/tmp/shot.png', target_file: '/tmp/out.txt' },
    })
    const blocks = out.details ?? []
    const link = blocks.find((b) => b.contentType === 'link')
    expect(link?.url).toBe('https://example.com/docs')
    const image = blocks.find((b) => b.contentType === 'image')
    expect(image?.path).toBe('/tmp/shot.png')
    const file = blocks.find((b) => b.contentType === 'file')
    expect(file?.path).toBe('/tmp/out.txt')
  })

  it.each([
    ['https://a.example/x', '/tmp/a.png', '/tmp/a.py'],
    ['http://127.0.0.1:8000/y', '/var/log/shot.PNG', 'relative/nested/b.py'],
  ])('两类 looksLikePath 形态均命中（分隔符族 %s / 扩展名族 %s）', (url, img, filePath) => {
    const out = l0({
      tool_args: { page_url: url, shot: img, target_file: filePath },
    })
    const blocks = out.details ?? []
    expect(blocks.find((b) => b.contentType === 'link')?.url).toBe(url)
    expect(blocks.find((b) => b.contentType === 'image')?.path).toBe(img)
    expect(blocks.find((b) => b.contentType === 'file')?.path).toBe(filePath)
  })

  it('长文本（>120 或含换行）→ 可折叠 code 块；短标量 → 收进 kv 块', () => {
    const longText = 'x'.repeat(130)
    const out = l0({ tool_args: { note: longText, multi: 'a\nb', short: 'ok', count: 2 } })
    const blocks = out.details ?? []

    const code = blocks.filter((b) => b.contentType === 'code')
    expect(code.map((b) => b.label).sort()).toEqual(['multi', 'note'])
    for (const b of code) {
      expect(b.collapsible).toBe(true)
      expect(b.defaultExpanded).toBe(false)
    }
    // 短值聚合成单条 kv 块，置于块列表最前
    const kv = blocks.find((b) => b.contentType === 'kv')
    expect(kv?.kvItems).toEqual([
      { key: 'short', value: 'ok' },
      { key: 'count', value: '2' },
    ])
    expect(blocks[0]).toBe(kv)
  })

  it('对象/数组值 → json 折叠块', () => {
    const out = l0({ tool_args: { opts: { a: 1 }, list: [1, 2] } })
    const json = (out.details ?? []).filter((b) => b.contentType === 'json')
    expect(json.map((b) => b.label).sort()).toEqual(['list', 'opts'])
    for (const b of json) {
      expect(b.collapsible).toBe(true)
      expect(b.content).toEqual(b.label === 'list' ? [1, 2] : { a: 1 })
    }
  })

  it('partialOutput 非空 → log 块（label=执行输出，content 按换行拼接）', () => {
    const out = l0({ partialOutput: ['line1', 'line2'] })
    const log = (out.details ?? []).find((b) => b.contentType === 'log')
    expect(log?.label).toBe('执行输出')
    expect(log?.content).toBe('line1\nline2')
    expect(log?.collapsible).toBe(false)
  })

  it('partialOutput 为空数组 → 不出 log 块', () => {
    const out = l0({ partialOutput: [] })
    expect((out.details ?? []).some((b) => b.contentType === 'log')).toBe(false)
  })

  it('无法解析为 dict 的纯文本结果 → 短文本 text 块 / 长文本 code 块', () => {
    const short = l0({ result: 'plain output' })
    const shortBlock = (short.details ?? []).find((b) => b.id === 'result-text')
    expect(shortBlock?.contentType).toBe('text')
    expect(shortBlock?.collapsible).toBe(false)

    const long = l0({ result: 'z'.repeat(150) })
    const longBlock = (long.details ?? []).find((b) => b.id === 'result-text')
    expect(longBlock?.contentType).toBe('code')
    expect(longBlock?.collapsible).toBe(true)
  })

  it('标题人性化：中文映射优先，未映射工具名下划线转空格并首字母大写', () => {
    expect(enhanceActivityWithToolConfig(makeActivity('file_read'), makeToolCall()).title).toBe('读取文件')
    const custom = enhanceActivityWithToolConfig(
      makeActivity('my_custom_tool'),
      makeToolCall(),
    )
    expect(custom.title).toBe('My Custom Tool')
  })
})

describe('safeParseResult 入参类型边界', () => {
  it.each([
    ['number', 42],
    ['boolean', true],
    ['bigint', 10n],
  ])('非字符串非对象入参（%s）→ null', (_name, input) => {
    expect(safeParseResult(input)).toBeNull()
  })

  it('空/空白字符串 → null；合法 JSON / Python dict → 对象（性质：解析结果可再取值）', () => {
    expect(safeParseResult('')).toBeNull()
    expect(safeParseResult('   ')).toBeNull()
    expect(safeParseResult('{"a": 1}')).toMatchObject({ a: 1 })
    const py = safeParseResult("{'a': True, 'b': None}")
    expect(py).toMatchObject({ a: true, b: null })
  })
})
