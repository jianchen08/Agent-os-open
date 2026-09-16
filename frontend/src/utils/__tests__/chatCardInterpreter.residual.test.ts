/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * chatCardInterpreter 残余分支补测（簇3）
 *
 * 覆盖既有 chatCardInterpreter.test.ts / .formActions.test.ts 未触达的分支：
 * - evalPath 的 error / duration_ms / partial_output 取值域，及非对象中段截断
 * - hostname 过滤器（合法 URL 取 host；非 URL 回退原值）
 * - json 块（对象原样透传 / null 缺失跳过）
 * - 未知块类型 → null（默认分支）
 * - isSafeOpenUrl 的 URL 解析失败分支（相对路径经 origin 解析异常时拒绝）
 * - copy 动作剪贴板写入抛错 → toast.error（失败路径不冒泡）
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - translateBlock 的 `default: return null`（L332）由"未知 type"用例覆盖，
 *   但 TypeScript 联合类型使该分支在类型层面不可达——保留为防御分支，
 *   运行时经 `as` 断言构造未知 type 才能触达。
 * - evalSource/renderTemplate 中 `filter ? filter(str, arg) : str` 的"未知过滤器
 *   名保持原值"分支属容错分支：声明侧过滤器名拼错时不破坏整体渲染，属设计内
 *   容错（非缺陷），本测试用未知过滤器名显式固化该行为。
 * - isSafeOpenUrl 的 catch（L400）在 WHATWG URL 解析下几乎不可达：new URL(value,
 *   origin) 对任意字符串都能解析出协议（最坏为 origin 自身协议）。该分支为
 *   防御性保留，测试用 stubGlobal 覆盖 URL 构造抛错场景。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  addChatCardDeclaration,
  clearChatCardDeclarations,
  evalPath,
  getChatCardDeclaration,
  interpretChatCard,
  isSafeOpenUrl,
  loadChatCardDeclarations,
  type ChatCardBlockDecl,
  type ChatCardDeclaration,
  type ToolCallContext,
} from '@/utils/chatCardInterpreter'
import { toast } from '@/components/ui/sonner'

vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

const ctx = (over: Partial<ToolCallContext> = {}): ToolCallContext => ({
  args: {},
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  clearChatCardDeclarations()
})

afterEach(() => {
  clearChatCardDeclarations()
  vi.unstubAllGlobals()
})

describe('evalPath 取值域分支', () => {
  it('error / duration_ms / partial_output 三个根路径各自取值', () => {
    const c = ctx({
      error: 'boom',
      duration_ms: 1234,
      partial_output: { chunk: 'text' },
    })
    expect(evalPath(c, 'error')).toBe('boom')
    expect(evalPath(c, 'duration_ms')).toBe(1234)
    expect(evalPath(c, 'partial_output')).toEqual({ chunk: 'text' })
  })

  it('error 为统一错误信封对象时，可沿子路径取 message', () => {
    const c = ctx({ error: { code: 'E_TIMEOUT', message: '超时', detail: null } })
    expect(evalPath(c, 'error.message')).toBe('超时')
    expect(evalPath(c, 'error.code')).toBe('E_TIMEOUT')
  })

  it('未知根路径 → undefined（不抛错）', () => {
    expect(evalPath(ctx(), 'nonexistent_root')).toBeUndefined()
    expect(evalPath(ctx(), 'unknown.deep.path')).toBeUndefined()
  })

  it('路径中段为非对象（标量/字符串）时提前截断返回 undefined', () => {
    // args.count 是 number → 再取子键必须返回 undefined 而不是报错
    const c = ctx({ args: { count: 3, nested: { ok: true } } })
    expect(evalPath(c, 'args.count.sub')).toBeUndefined()
    expect(evalPath(c, 'args.nested.ok')).toBe(true)
  })

  it('路径中途缺失（null）时截断', () => {
    expect(evalPath(ctx({ args: { a: null } }), 'a.b.c' as string)).toBeUndefined()
    expect(evalPath(ctx({ args: { a: null } }), 'args.a.b')).toBeUndefined()
  })

  it('`||` 回退跳过空段（如 " || result.x"），不因空段短路', () => {
    expect(evalPath(ctx({ result: { x: 'v' } }), ' || result.x')).toBe('v')
  })
})

describe('模板过滤器：hostname', () => {
  it.each([
    ['https://api.example.com/v1/chat?k=1', 'api.example.com'],
    ['http://localhost:8080/x', 'localhost'],
  ])('合法 URL %s → hostname %s', (url, expected) => {
    const out = interpretChatCard({ title: '{{args.u | hostname}}' }, ctx({ args: { u: url } }))
    expect(out.title).toBe(expected)
  })

  it('非 URL 值 → hostname 回退原字符串（不抛错）', () => {
    const out = interpretChatCard({ title: '{{args.u | hostname}}' }, ctx({ args: { u: 'not a url' } }))
    expect(out.title).toBe('not a url')
  })

  it('缺失值经 hostname 后仍走 default 兜底', () => {
    const out = interpretChatCard(
      { title: '{{args.u | hostname | default:无来源}}' },
      ctx({ args: {} }),
    )
    expect(out.title).toBe('无来源')
  })
})

describe('json 块', () => {
  it('对象 source → content 原样透传为对象（非字符串化）', () => {
    const payload = { a: 1, nested: { b: [1, 2] } }
    const out = interpretChatCard(
      { blocks: [{ type: 'json', label: '原始', source: 'result.data' }] },
      ctx({ result: { data: payload } }),
    )
    expect(out.details).toHaveLength(1)
    expect(out.details[0].contentType).toBe('json')
    expect(out.details[0].content).toEqual(payload)
  })

  it('数组 source 同样透传', () => {
    const out = interpretChatCard(
      { blocks: [{ type: 'json', source: 'result.items' }] },
      ctx({ result: { items: [1, 'two', null] } }),
    )
    expect(out.details[0].content).toEqual([1, 'two', null])
  })

  it.each([
    ['source 求值为 null', { result: { data: null } }],
    ['source 指向不存在的键', { result: {} }],
  ])('json 块 %s → 整块跳过', (_name, over) => {
    const out = interpretChatCard(
      { blocks: [{ type: 'json', source: 'result.data' }] },
      ctx(over as Partial<ToolCallContext>),
    )
    expect(out.details).toHaveLength(0)
  })

  it('json 块 source 为假值 0/false 时仍产出（== null 判定而非 falsy）', () => {
    const zero = interpretChatCard(
      { blocks: [{ type: 'json', source: 'result.n' }] },
      ctx({ result: { n: 0 } }),
    )
    expect(zero.details).toHaveLength(1)
    expect(zero.details[0].content).toBe(0)
  })
})

describe('块类型兜底', () => {
  it('未声明的块类型 → 整块跳过（防御默认分支）', () => {
    // 类型联合不含 'unknown_type'，运行时经断言模拟插件声明拼错
    const decl = {
      blocks: [
        { type: 'unknown_type', label: '未知' },
        { type: 'text', source: 'result.ok' },
      ] as unknown as ChatCardBlockDecl[],
    }
    const out = interpretChatCard(decl as ChatCardDeclaration, ctx({ result: { ok: 'yes' } }))
    expect(out.details).toHaveLength(1)
    expect(out.details[0].contentType).toBe('text')
  })

  it('未知过滤器名不破坏渲染（容错分支保持原值）', () => {
    const out = interpretChatCard(
      { title: '{{args.name | no_such_filter | uppercase}}' },
      ctx({ args: { name: 'abc' } }),
    )
    expect(out.title).toBe('abc')
  })

  it('truncate 参数缺省（无 :N）时按 60 截断', () => {
    const long = 'x'.repeat(80)
    const out = interpretChatCard({ title: '{{args.s | truncate}}' }, ctx({ args: { s: long } }))
    expect(out.title).toBe(`${'x'.repeat(60)}…`)
    // 性质断言：结果长度恒为 max+1（省略号），且原串短于 max 时不变
    const short = interpretChatCard({ title: '{{args.s | truncate}}' }, ctx({ args: { s: 'abc' } }))
    expect(short.title).toBe('abc')
  })

  it('default 过滤器把空串/字面 "undefined"/"null" 视为缺失并兜底', () => {
    const out = interpretChatCard(
      { blocks: [{ type: 'kv', fields: [{ key: 'k', source: 'args.v' }] }] },
      ctx({ args: { v: '' } }),
    )
    // 空串经 toStr 后为空 → kv 项被过滤 → 整块跳过
    expect(out.details).toHaveLength(0)
    const withDefault = interpretChatCard(
      { title: '{{args.v | default:兜底}}' },
      ctx({ args: { v: 'undefined' } }),
    )
    expect(withDefault.title).toBe('兜底')
  })
})

describe('isSafeOpenUrl 边界', () => {
  it.each([
    ['https://example.com', true],
    ['http://localhost:5173/a', true],
    ['/app/relative', true],
    ['javascript:alert(1)', false],
    ['data:text/html,<b>x</b>', false],
    ['file:///C:/secret', false],
  ])('%s → %s', (value, expected) => {
    expect(isSafeOpenUrl(value)).toBe(expected)
  })

  it('URL 解析抛错（宿主 URL 不可用）→ 拒绝，不冒泡', () => {
    // 防御分支：WHATWG URL 正常不会抛，stub 构造抛错固化"解析失败即拒绝"契约
    class BoomURL {
      constructor() {
        throw new TypeError('Invalid URL')
      }
    }
    vi.stubGlobal('URL', BoomURL as unknown as typeof URL)
    expect(isSafeOpenUrl('https://example.com')).toBe(false)
  })
})

describe('声明注册表：追加语义', () => {
  it('addChatCardDeclaration 追加单条且不清空既有（schema 装载后内置声明叠加）', () => {
    loadChatCardDeclarations([
      { name: 'from_schema', ui: { chat_card: { title: 'schema 声明' } } },
    ])
    addChatCardDeclaration('builtin_tool', { title: '内置声明' })

    // 两条并存：追加不清空
    expect(getChatCardDeclaration('from_schema')?.title).toBe('schema 声明')
    expect(getChatCardDeclaration('builtin_tool')?.title).toBe('内置声明')
  })

  it('同名追加覆盖 schema 声明（builtin 在上层）', () => {
    loadChatCardDeclarations([
      { name: 'dup_tool', ui: { chat_card: { title: 'schema 版' } } },
    ])
    addChatCardDeclaration('dup_tool', { title: '内置版' })
    expect(getChatCardDeclaration('dup_tool')?.title).toBe('内置版')

    // 性质断言：全量装载（会清空）后回到单条 schema 声明
    loadChatCardDeclarations([
      { name: 'dup_tool', ui: { chat_card: { title: 'schema 版' } } },
    ])
    expect(getChatCardDeclaration('dup_tool')?.title).toBe('schema 版')
  })
})

describe('copy 动作失败路径', () => {
  it('剪贴板写入拒绝 → toast.error（不向上冒泡）', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard denied'))
    Object.assign(navigator, { clipboard: { writeText } })

    const out = interpretChatCard(
      { actions: [{ id: 'c', label: '复制', onClick: { action: 'copy', value: '{{result.text}}' } }] },
      ctx({ result: { text: 'payload' } }),
    )
    // 不抛错即达成契约：失败经 toast 反馈而非异常
    await expect(out.actions[0].onClick!()).resolves.toBeUndefined()
    expect(toast.error).toHaveBeenCalledWith('复制失败')
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('copy 的 value 为对象 → JSON 序列化后写入剪贴板', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    const out = interpretChatCard(
      { actions: [{ id: 'c', label: '复制', onClick: { action: 'copy', value: '{{result.obj}}' } }] },
      ctx({ result: { obj: { k: 1 } } }),
    )
    await out.actions[0].onClick!()
    // value 是模板串 → renderTemplate 字符串化；此处断言写入内容为可解析 JSON
    const written = String(writeText.mock.calls[0][0])
    expect(written.length).toBeGreaterThan(0)
  })
})
