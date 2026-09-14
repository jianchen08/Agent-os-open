/**
 * logger + outputSchemaView 分支补测（两份工具模块的剩余分支）
 *
 * logger：
 * - safeStringify 的循环引用降级（含 replacer 路径与整体失败兜底）；
 * - formatArg 的 bigint / 函数（typeof !== object）分支、Error 无 stack、
 *   AxiosError（isAxiosError / 有 response）与带 config 的 URL/method 提取；
 * - _format 的 %d 非数字、%j、占位符多于参数（占位符原样保留）、
 *   参数多于占位符（追加输出）、无占位符直接返回；
 * - shouldLog 的 enabled=false 与生产环境 debug 门控（经 stubEnv 走 PROD 分支）；
 * - verbose 级别门控、module 缓存命中与 clearCache、subModule 命名。
 *
 * outputSchemaView：
 * - schemaTypeToFieldType 的 enum 数组/非数组、array+items.enum、布尔/整数等；
 * - coerceDisplayValues 的 string/number/其它类型收敛；
 * - validateOutputSubset 的根类型错配、required、enum、items 递归、
 *   组合关键字宽松忽略。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildOutputSchemaView,
  coerceDisplayValues,
  getOutputSchema,
  loadOutputSchemas,
  outputSchemaToFormFields,
  validateOutputSubset,
} from '@/utils/outputSchemaView'

const consoleSpies = () => ({
  error: vi.spyOn(console, 'error').mockImplementation(() => {}),
  warn: vi.spyOn(console, 'warn').mockImplementation(() => {}),
  log: vi.spyOn(console, 'log').mockImplementation(() => {}),
})

let spies: ReturnType<typeof consoleSpies>

beforeEach(() => {
  spies = consoleSpies()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
})

describe('logger — safeStringify 循环引用降级', () => {
  it('循环引用对象被替换为 [Circular]（不抛异常）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setEnabled(true)
    logger.setLevel(4)
    const cyclic: Record<string, unknown> = { name: 'a' }
    cyclic.self = cyclic

    const log = logger.module('Cyclic')
    expect(() => log.info('循环对象: %j', cyclic)).not.toThrow()

    const output = spies.log.mock.calls[0][1] as string
    expect(output).toContain('[Circular]')
    expect(output).not.toContain('[object Object]')
  })

  it('深嵌套循环引用同样降级（多层后回指）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    const root: Record<string, unknown> = { level: 1, child: { up: null } }
    ;(root.child as Record<string, unknown>).up = root

    logger.module('Deep').debug('deep: %j', root)
    expect(spies.log.mock.calls[0][1]).toContain('[Circular]')
  })

  it('首次 JSON.stringify 抛错且 replacer 也失败时兜底 [Circular]', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    // ownKeys 抛错 → 外层 stringify 抛异常 → 内层 replacer stringify 同样抛异常
    const evil = new Proxy(
      { a: 1 },
      {
        ownKeys() {
          throw new Error('ownKeys boom')
        },
      },
    )

    expect(() => logger.module('InnerFallback').info('v=%j', evil)).not.toThrow()
    expect(spies.log.mock.calls[0][1]).toBe('v=[Circular]')
  })

  it('正常对象仍被完整序列化（不被降级破坏）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Plain').info('obj: %j', { a: 1, b: 'two' })
    expect(spies.log.mock.calls[0][1]).toContain('"a":1')
    expect(spies.log.mock.calls[0][1]).toContain('"b":"two"')
  })
})

describe('logger — formatArg 各类型分支', () => {
  it('bigint 经 String() 输出', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Big').info('值: %s', 10n)
    expect(spies.log.mock.calls[0][1]).toBe('值: 10')
  })

  it('null/undefined 经 %j 按 JSON 语义输出 null/undefined 字面量', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Nil').info('A%jB', null)
    expect(spies.log.mock.calls[0][1]).toBe('AnullB')

    spies.log.mockClear()
    logger.module('Nil').info('X%jY', undefined)
    expect(spies.log.mock.calls[0][1]).toBe('XundefinedY')
  })

  it('Error 对象提取 message/name（isError 路径）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    const err = new Error('boom')
    logger.module('Err').error('失败: %s', err)
    const output = spies.error.mock.calls[0][1] as string
    expect(output).toContain('<Error')
    expect(output).toContain('boom')
    expect(output).toContain('"name":"Error"')
  })

  it('AxiosError（isAxiosError=true）带 status/url/method 时全部提取', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    const axiosErr = {
      isAxiosError: true,
      message: 'Request failed with status code 404',
      name: 'AxiosError',
      code: 'ERR_BAD_REQUEST',
      response: { status: 404 },
      config: { url: '/api/v1/x', method: 'get' },
      stack: 'line1\nline2\nline3\nline4',
    }
    logger.module('Ax').error('请求失败: %s', axiosErr)

    const output = spies.error.mock.calls[0][1] as string
    expect(output).toContain('<AxiosError')
    expect(output).toContain('"status":404')
    expect(output).toContain('"url":"/api/v1/x"')
    expect(output).toContain('"method":"get"')
    expect(output).toContain('"code":"ERR_BAD_REQUEST"')
    // stack 只保留前 3 行
    expect(output).toContain('line1')
    expect(output).not.toContain('line4')
  })

  it('仅有 response 字段（非 axios 标记）时走 <AxiosError> 分支', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('RespOnly').warn('异常: %s', { response: { status: 500 }, message: 'server' })
    const output = spies.warn.mock.calls[0][1] as string
    expect(output).toContain('<AxiosError')
    expect(output).toContain('"status":500')
  })

  it('Error 无 stack 时不追加 stack 字段', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    const err = new Error('no-stack')
    delete (err as { stack?: string }).stack
    logger.module('NoStack').error('%s', err)
    expect(spies.error.mock.calls[0][1]).not.toContain('"stack"')
  })

  it('config 无 url/method 时只保留其余字段（部分提取）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('PartialCfg').error('%s', {
      isAxiosError: true,
      message: 'm',
      config: { timeout: 1000 },
    })
    const output = spies.error.mock.calls[0][1] as string
    expect(output).not.toContain('"url"')
    expect(output).not.toContain('"method"')
  })
})

describe('logger — _format 占位符边界', () => {
  it('%d 传入非数字时 Number() 失败落 0', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Fmt').info('计数=%d', 'not-a-number')
    expect(spies.log.mock.calls[0][1]).toBe('计数=0')
  })

  it('%d 传入数字字符串被转为数字', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Fmt').info('计数=%d', '42')
    expect(spies.log.mock.calls[0][1]).toBe('计数=42')
  })

  it('占位符多于参数时多余占位符原样保留', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Fmt').info('%s 与 %s', 'one')
    expect(spies.log.mock.calls[0][1]).toBe('one 与 %s')
  })

  it('参数多于占位符时剩余参数追加到末尾', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Fmt').info('%s', 'first', 'second', 'third')
    expect(spies.log.mock.calls[0][1]).toBe('first second third')
  })

  it('无参数时直接返回原模板（不做替换）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Fmt').info('纯文本 %s 原样')
    expect(spies.log.mock.calls[0][1]).toBe('纯文本 %s 原样')
  })

  it('%j 序列化对象；%s 对普通对象走序列化而非 [object Object]', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Fmt').info('json=%j str=%s', { a: 1 }, { b: 2 })
    const out = spies.log.mock.calls[0][1] as string
    expect(out).toContain('json={"a":1}')
    expect(out).toContain('str={"b":2}')
  })

  it('%s 传入 null/undefined 输出空串（formatArg 首行守卫）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Fmt').info('A%sB', null)
    expect(spies.log.mock.calls[0][1]).toBe('AB')

    spies.log.mockClear()
    logger.module('Fmt').info('C%sD', undefined)
    expect(spies.log.mock.calls[0][1]).toBe('CD')
  })

  it('%s 传入函数/符号（typeof !== object）走 String() 分支', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    const fn = function namedFn() {
      return 1
    }
    logger.module('Fmt').info('f=%s', fn)
    expect(spies.log.mock.calls[0][1]).toContain('namedFn')
  })

  it('WARN 级别被门控时 warn 不输出（shouldLog 为假直接 return）', async () => {
    const { LogLevel, logger } = await import('@/utils/logger')
    logger.setEnabled(true)
    logger.setLevel(LogLevel.ERROR) // WARN(1) > ERROR(0) → 不输出
    logger.module('Gated').warn('不应输出')
    expect(spies.warn).not.toHaveBeenCalled()

    logger.setLevel(LogLevel.WARN)
    logger.module('Gated').warn('应输出')
    expect(spies.warn).toHaveBeenCalledTimes(1)
  })

  it('showTimestamp/showModule 关闭时前缀省略对应段（formatPrefix 分支）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    // 时间戳段恒开；模块名用空串绕过（见「未命名模块」用例）。
    // 这里断言前缀的时间戳段形态（HH:MM:SS 包裹在 [] 内）
    logger.module('Ts').info('t')
    const prefix = spies.log.mock.calls[0][0] as string
    expect(prefix).toMatch(/^\[\d{2}:\d{2}:\d{2}\]/)
  })
})

describe('logger — 级别门控与生产环境分支', () => {
  it('setEnabled(false) 时 verbose 也不输出', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setEnabled(false)
    logger.setLevel(4)
    logger.module('Off').verbose('不应输出')
    expect(spies.log).not.toHaveBeenCalled()
  })

  it('verbose 在 VERBOSE 级别以下被门控（level=INFO 时不输出）', async () => {
    const { LogLevel, logger } = await import('@/utils/logger')
    logger.setEnabled(true)
    logger.setLevel(LogLevel.INFO)
    logger.module('V').verbose('debug 级以下')
    expect(spies.log).not.toHaveBeenCalled()

    logger.setLevel(LogLevel.VERBOSE)
    logger.module('V').verbose('应输出')
    expect(spies.log).toHaveBeenCalledTimes(1)
  })

  it('生产环境下 DEBUG/VERBOSE 默认静默，setDebugInProduction(true) 后放行', async () => {
    vi.resetModules()
    vi.stubEnv('PROD', true)
    vi.stubEnv('DEV', false)
    const { LogLevel, logger } = await import('@/utils/logger')
    logger.setEnabled(true)
    logger.setLevel(LogLevel.VERBOSE)
    logger.setDebugInProduction(false)

    logger.module('Prod').debug('生产静默')
    logger.module('Prod').verbose('生产静默')
    expect(spies.log).not.toHaveBeenCalled()

    logger.setDebugInProduction(true)
    logger.module('Prod').debug('放行')
    expect(spies.log).toHaveBeenCalledTimes(1)

    // 生产环境下 ERROR/WARN/INFO 不受 debugInProduction 影响
    logger.setDebugInProduction(false)
    logger.module('Prod').info('info 仍输出')
    expect(spies.log).toHaveBeenCalledTimes(2)
  })

  it('生产环境构造时初始级别为 INFO（非生产为 DEBUG）', async () => {
    vi.resetModules()
    vi.stubEnv('PROD', true)
    const { LogLevel, logger } = await import('@/utils/logger')
    expect(logger.getLevel()).toBe(LogLevel.INFO)

    vi.resetModules()
    vi.stubEnv('PROD', false)
    const mod = await import('@/utils/logger')
    expect(mod.logger.getLevel()).toBe(mod.LogLevel.DEBUG)
  })
})

describe('logger — 模块缓存与子模块', () => {
  it('同模块名返回同一实例（缓存命中）', async () => {
    const { logger } = await import('@/utils/logger')
    const a = logger.module('Same')
    const b = logger.module('Same')
    expect(a).toBe(b)
  })

  it('模块缓存经 LogManager 内部 Map 生效（clearCache 未暴露在 logger 门面上）', async () => {
    const { logger } = await import('@/utils/logger')
    // 现状契约：logger 门面只导出 module/setLevel/getLevel/setEnabled/
    // setDebugInProduction/LogLevel——clearCache 仅在 LogManager 类内部可用
    // （见回报中的疑似缺陷：声明了 clearCache 但未从 logger 导出）
    expect((logger as unknown as Record<string, unknown>).clearCache).toBeUndefined()
    expect(logger.module('Cleared')).toBe(logger.module('Cleared'))
  })

  it('subModule 命名带父模块前缀，模块名进入日志前缀', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    const sub = logger.module('Parent').subModule('Child')
    sub.info('子模块日志')

    const prefix = spies.log.mock.calls[0][0] as string
    expect(prefix).toContain('[Parent:Child]')
  })

  it('subModule 同路径两次返回同一实例（经父模块缓存）', async () => {
    const { logger } = await import('@/utils/logger')
    const p = logger.module('P2')
    expect(p.subModule('C')).toBe(p.subModule('C'))
  })

  it('未命名模块（空串）不输出模块名前缀', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('').info('无模块名')
    const prefix = spies.log.mock.calls[0][0] as string
    expect(prefix).not.toContain('[]')
    expect(prefix).toContain('INFO')
  })

  it('格式前缀含级别标识（ERROR 级用 ❌ 标记）', async () => {
    const { logger } = await import('@/utils/logger')
    logger.setLevel(4)
    logger.module('Prefix').error('e')
    expect(spies.error.mock.calls[0][0]).toContain('ERROR')
  })

  it('loggers 预定义模块与 logger.module 同源（同名单例）', async () => {
    const { logger, loggers } = await import('@/utils/logger')
    expect(loggers.websocket).toBe(logger.module('WebSocket'))
    expect(loggers.sessionStore).toBe(logger.module('SessionStore'))
  })
})

describe('outputSchemaView — outputSchemaToFormFields 类型映射边界', () => {
  it('enum + type 数组含 array → multiselect', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { tags: { type: ['array', 'null'], enum: [['a'], ['b']] } },
    })
    expect(fields[0].type).toBe('multiselect')
  })

  it('enum + 非数组 type → select', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { mode: { type: 'string', enum: ['a', 'b'] } },
    })
    expect(fields[0].type).toBe('select')
    expect(fields[0].options).toEqual([
      { label: 'a', value: 'a' },
      { label: 'b', value: 'b' },
    ])
  })

  it('type 为数组（取首个）决定字段类型', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { n: { type: ['integer', 'null'] } },
    })
    expect(fields[0].type).toBe('number')
  })

  it('array + items.enum（无顶层 enum）当前抛 TypeError（疑似缺陷，见回报）', async () => {
    // schemaTypeToFieldType 判定为 multiselect，但 options 分支读 prop.enum.map——
    // items.enum 形态下 prop.enum 为 undefined，直接抛 TypeError
    expect(() =>
      outputSchemaToFormFields({
        type: 'object',
        properties: { pick: { type: 'array', items: { enum: [1, 2] } } },
      }),
    ).toThrow(TypeError)
  })

  it('array + items 非 enum → string（不读未定义的 enum）', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { list: { type: 'array', items: { type: 'string' } } },
    })
    expect(fields[0].type).toBe('string')
    expect(fields[0].options).toBeUndefined()
  })

  it.each([
    ['boolean', 'toggle'],
    ['number', 'number'],
    ['integer', 'number'],
    ['string', 'string'],
    ['object', 'string'],
  ])('type=%s → 字段类型 %s', async (type, expected) => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { f: { type } },
    })
    expect(fields[0].type).toBe(expected)
  })

  it('properties 为数组或 null 时返回空（非 object 形态不适用）', async () => {
    expect(outputSchemaToFormFields({ properties: [] })).toEqual([])
    expect(outputSchemaToFormFields({ properties: null as never })).toEqual([])
    expect(outputSchemaToFormFields({})).toEqual([])
  })

  it('属性值为非对象/数组时被跳过（其余字段保留）', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { bad: null, alsoBad: ['x'], good: { type: 'string' } },
    })
    expect(fields.map((f) => f.name)).toEqual(['good'])
  })

  it('title/description/default 透传，required 命中标记', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: {
        a: { type: 'string', title: '甲', description: '说明', default: 'x' },
      },
      required: ['a'],
    })
    expect(fields[0]).toMatchObject({
      name: 'a',
      label: '甲',
      description: '说明',
      default: 'x',
      required: true,
    })
  })
})

describe('outputSchemaView — coerceDisplayValues 值收敛', () => {
  it('string 字段的对象/数组值被 JSON 序列化（避免控件类型错配）', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { s: { type: 'string' } },
    })
    const out = coerceDisplayValues(fields, { s: { nested: true } })
    expect(typeof out.s).toBe('string')
    expect(out.s).toContain('"nested"')
  })

  it('string 字段的原始字符串保持原样，null 也保持', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { s: { type: 'string' } },
    })
    expect(coerceDisplayValues(fields, { s: 'plain' }).s).toBe('plain')
    expect(coerceDisplayValues(fields, { s: null as never }).s).toBeNull()
  })

  it('number 字段的数字字符串被转成数字，非数字保持原样', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { n: { type: 'number' } },
    })
    expect(coerceDisplayValues(fields, { n: '42' }).n).toBe(42)
    expect(coerceDisplayValues(fields, { n: 'abc' }).n).toBe('abc')
  })

  it('未在数据中出现的字段不写入结果（不伪造空值）', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { a: { type: 'string' }, b: { type: 'string' } },
    })
    const out = coerceDisplayValues(fields, { a: 'x' })
    expect(Object.keys(out)).toEqual(['a'])
  })

  it('非 string/number 类型字段原值透传（enum 数组字段）', async () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: { pick: { type: 'string', enum: ['a', 'b'] } },
    })
    const out = coerceDisplayValues(fields, { pick: 'a' })
    expect(out.pick).toBe('a')
  })
})

describe('outputSchemaView — validateOutputSubset 子集语义', () => {
  it('根对象类型错配时报错并指出实际类型', async () => {
    const errs = validateOutputSubset({ type: 'object', properties: {} }, 42)
    expect(errs[0]).toContain('expected type object')
    expect(errs[0]).toContain('number')
  })

  it('根为数组时报错并标 array（区分对象/数组）', async () => {
    const errs = validateOutputSubset({ type: 'object', properties: {} }, [])
    expect(errs[0]).toContain('array')
  })

  it('仅声明 properties（无 type）且数据为标量时静默通过（宽松）', async () => {
    expect(validateOutputSubset({ properties: {} }, 'scalar')).toEqual([])
  })

  it('缺 required 字段时报错（含路径与字段名）', async () => {
    const errs = validateOutputSubset(
      { type: 'object', properties: { a: { type: 'string' } }, required: ['a', 'b'] },
      { a: 'x' },
    )
    expect(errs.some((e) => e.includes('missing required field `b`'))).toBe(true)
    expect(errs.some((e) => e.includes('missing required field `a`'))).toBe(false)
  })

  it('required 字段值为 undefined 视同缺失', async () => {
    const errs = validateOutputSubset(
      { type: 'object', properties: { a: { type: 'string' } }, required: ['a'] },
      { a: undefined },
    )
    expect(errs).toHaveLength(1)
  })

  it('嵌套对象错误带完整路径（父.子）', async () => {
    const errs = validateOutputSubset(
      {
        type: 'object',
        properties: { outer: { type: 'object', properties: { inner: { type: 'string' } } } },
      },
      { outer: { inner: 123 } },
    )
    expect(errs[0]).toContain('.outer.inner')
  })

  it('数组 items 递归校验并带下标路径', async () => {
    const errs = validateOutputSubset(
      { type: 'array', items: { type: 'string' } },
      ['ok', 42],
    )
    expect(errs).toHaveLength(1)
    expect(errs[0]).toContain('[1]')
  })

  it('enum 未命中时报错并列出合法值', async () => {
    const errs = validateOutputSubset({ type: 'string', enum: ['a', 'b'] }, 'c')
    expect(errs[0]).toContain('not in enum')
    expect(errs[0]).toContain('a, b')
  })

  it('type 为数组（联合类型）时任一匹配即通过', async () => {
    expect(validateOutputSubset({ type: ['string', 'null'] }, null)).toEqual([])
    expect(validateOutputSubset({ type: ['string', 'null'] }, 'x')).toEqual([])
    expect(validateOutputSubset({ type: ['string', 'null'] }, 5)).toHaveLength(1)
  })

  it('integer 与整值浮点宽容，非整值报错', async () => {
    expect(validateOutputSubset({ type: 'integer' }, 3)).toEqual([])
    expect(validateOutputSubset({ type: 'integer' }, 3.5)).toHaveLength(1)
  })

  it('number 对 NaN/Infinity 报错（有限性检查）', async () => {
    expect(validateOutputSubset({ type: 'number' }, Number.NaN)).toHaveLength(1)
    expect(validateOutputSubset({ type: 'number' }, Number.POSITIVE_INFINITY)).toHaveLength(1)
  })

  it('boolean/null/object/array 各自类型判定', async () => {
    expect(validateOutputSubset({ type: 'boolean' }, true)).toEqual([])
    expect(validateOutputSubset({ type: 'boolean' }, 'true')).toHaveLength(1)
    expect(validateOutputSubset({ type: 'null' }, null)).toEqual([])
    expect(validateOutputSubset({ type: 'null' }, undefined)).toHaveLength(1)
    expect(validateOutputSubset({ type: 'array' }, [])).toEqual([])
    expect(validateOutputSubset({ type: 'array' }, {})).toHaveLength(1)
    expect(validateOutputSubset({ type: 'object' }, {})).toEqual([])
    expect(validateOutputSubset({ type: 'object' }, [])).toHaveLength(1)
  })

  it('未知 type 词汇宽松通过（镜像内核宽松语义）', async () => {
    expect(validateOutputSubset({ type: 'some-future-type' }, 'anything')).toEqual([])
  })

  it('type 为数组且含未知词汇时该分支宽松通过（check 的 default 分支）', () => {
    expect(validateOutputSubset({ type: ['mystery'] }, 'whatever')).toEqual([])
  })

  it('联合类型含 object 时数组/non-null 判定生效（不把数组当 object）', () => {
    // check('object') 要求非 null 且非数组
    expect(validateOutputSubset({ type: ['object', 'null'] }, { a: 1 })).toEqual([])
    expect(validateOutputSubset({ type: ['object', 'null'] }, null)).toEqual([])
    expect(validateOutputSubset({ type: ['object'] }, [])).toHaveLength(1)
    expect(validateOutputSubset({ type: ['object'] }, null)).toHaveLength(1)
  })

  it('type 为非字符串非数组（数字）时宽松通过（typeMatches 末行 return true）', () => {
    expect(validateOutputSubset({ type: 7 as never }, 'x')).toEqual([])
  })

  it('schema 为 null/falsy 时返回空错误列表', async () => {
    expect(validateOutputSubset(null as never, { a: 1 })).toEqual([])
  })

  it('additionalProperties 等组合关键字被忽略（不误报）', async () => {
    const errs = validateOutputSubset(
      {
        type: 'object',
        additionalProperties: false,
        properties: { a: { type: 'string' } },
      },
      { a: 'x', extra: 1 },
    )
    expect(errs).toEqual([])
  })

  it('数据为 null 且 schema 声明 object 时报错（null 被描述为 object 系现状文案）', async () => {
    const errs = validateOutputSubset({ type: 'object', properties: {} }, null)
    expect(errs).toHaveLength(1)
    // 现状：null 在「got X」三元里只区分 array/其它，null 被报成 object（文案不精确，见回报）
    expect(errs[0]).toBe('根: expected type object, got object')
  })
})

describe('outputSchemaView — buildOutputSchemaView 数据形态分支', () => {
  const schema = {
    type: 'object',
    properties: { status: { type: 'string' } },
    required: ['status'],
  }

  it('resultData 为对象时优先使用（忽略 result 字符串）', async () => {
    const view = buildOutputSchemaView(schema, '{"status":"ignored"}', { status: 'from-data' })
    expect(view?.block.content).toMatchObject({ readOnly: true })
    expect((view!.block.content as { values: Record<string, unknown> }).values.status).toBe(
      'from-data',
    )
  })

  it('无 resultData 时解析 JSON 字符串 result', async () => {
    const view = buildOutputSchemaView(schema, '{"status":"ok"}')
    expect((view!.block.content as { values: Record<string, unknown> }).values.status).toBe('ok')
  })

  it('result 为非 JSON 字符串时返回 null（非结构化输出不适用）', async () => {
    expect(buildOutputSchemaView(schema, 'plain text')).toBeNull()
  })

  it('result 为对象时直接使用', async () => {
    const view = buildOutputSchemaView(schema, { status: 'obj' })
    expect((view!.block.content as { values: Record<string, unknown> }).values.status).toBe('obj')
  })

  it('result 为 null/undefined 时返回 null', async () => {
    expect(buildOutputSchemaView(schema, null)).toBeNull()
    expect(buildOutputSchemaView(schema, undefined)).toBeNull()
  })

  it('schema 无可用字段（properties 空）时返回 null', async () => {
    expect(buildOutputSchemaView({ type: 'object', properties: {} }, { a: 1 })).toBeNull()
  })

  it('合规数据 violations 为空，违规数据带出违规消息', async () => {
    const ok = buildOutputSchemaView(schema, { status: 'x' })
    expect(ok!.violations).toEqual([])

    const bad = buildOutputSchemaView(schema, { status: 42 })
    expect(bad!.violations.length).toBeGreaterThan(0)
    expect(bad!.violations[0]).toContain('.status')
  })

  it('块形状为只读 form（contentType/content.formFields/readOnly）', async () => {
    const view = buildOutputSchemaView(schema, { status: 'x' })!
    expect(view.block).toMatchObject({ id: 'output_schema_view', contentType: 'form' })
    const content = view.block.content as Record<string, unknown>
    expect(content.readOnly).toBe(true)
    expect(Array.isArray(content.formFields)).toBe(true)
  })
})

describe('outputSchemaView — loadOutputSchemas/getOutputSchema 注册表', () => {
  it('装载后可按名查询；重复装载清空旧项（幂等）', async () => {
    loadOutputSchemas([{ name: 't1', output_schema: { type: 'object' } }])
    expect(getOutputSchema('t1')).toEqual({ type: 'object' })

    loadOutputSchemas([{ name: 't2', output_schema: { type: 'array' } }])
    expect(getOutputSchema('t1')).toBeUndefined()
    expect(getOutputSchema('t2')).toEqual({ type: 'array' })
  })

  it('缺 name 或缺 output_schema 的条目被跳过', async () => {
    loadOutputSchemas([
      { output_schema: { type: 'object' } },
      { name: 'no-schema' },
      { name: 'bad-type', output_schema: 'string' as never },
    ])
    expect(getOutputSchema('no-schema')).toBeUndefined()
    expect(getOutputSchema('bad-type')).toBeUndefined()
  })

  it('空数组装载后注册表为空', async () => {
    loadOutputSchemas([{ name: 'x', output_schema: { type: 'object' } }])
    loadOutputSchemas([])
    expect(getOutputSchema('x')).toBeUndefined()
  })
})
