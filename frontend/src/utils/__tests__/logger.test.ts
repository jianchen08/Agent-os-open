// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * logger 统一日志服务测试
 *
 * 覆盖：级别门控、前缀格式化、printf 占位符（%s/%d/%j）、
 * Error/AxiosError 序列化、循环引用降级、模块缓存、enable/disable、
 * 子模块、clearCache。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { LogLevel, logger } from '@/utils/logger'

describe('logger 统一日志服务', () => {
  let consoleError: ReturnType<typeof vi.spyOn>
  let consoleLog: ReturnType<typeof vi.spyOn>
  let consoleWarn: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    consoleLog = vi.spyOn(console, 'log').mockImplementation(() => {})
    consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    logger.setEnabled(true)
    logger.setLevel(LogLevel.DEBUG) // 默认非生产：DEBUG 可见
    logger.setDebugInProduction(false)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('setLevel 门控：WARN 级别下 info/debug 不输出，warn/error 输出', () => {
    logger.setLevel(LogLevel.WARN)
    const log = logger.module('Test')
    log.debug('debug 不该出现')
    log.info('info 不该出现')
    log.warn('警告出现')
    log.error('错误出现')
    expect(consoleLog).not.toHaveBeenCalled()
    expect(consoleWarn).toHaveBeenCalledTimes(1)
    expect(consoleError).toHaveBeenCalledTimes(1)
    expect(logger.getLevel()).toBe(LogLevel.WARN)
  })

  it('setEnabled(false) 全部静默', () => {
    logger.setEnabled(false)
    const log = logger.module('Test')
    log.error('不应输出')
    expect(consoleError).not.toHaveBeenCalled()
  })

  it('格式化前缀含时间戳/模块名/级别标识', () => {
    const log = logger.module('MyMod')
    log.info('hello')
    const call = consoleLog.mock.calls[0]
    const prefix = String(call[0])
    expect(prefix).toContain('[MyMod]')
    expect(prefix).toContain('INFO')
    expect(prefix).toMatch(/\[\d{2}:\d{2}:\d{2}\]/)
  })

  it('printf 占位符 %s/%d/%j 替换；多余参数追加', () => {
    const log = logger.module('Fmt')
    log.info('name=%s count=%d data=%j', 'agent', 3, { a: 1 })
    const call = consoleLog.mock.calls[0]
    const msg = String(call[1])
    expect(msg).toContain('name=agent count=3 data={"a":1}')

    log.info('extra', 'x', 'y')
    const msg2 = String(consoleLog.mock.calls[1][1])
    expect(msg2).toBe('extra x y')
  })

  it('%d 非数字参数 → Number() 转换或 0', () => {
    const log = logger.module('Fmt')
    log.info('n=%d', '42')
    expect(String(consoleLog.mock.calls[0][1])).toBe('n=42')
    log.info('n=%d', 'abc')
    expect(String(consoleLog.mock.calls[1][1])).toBe('n=0')
  })

  it('Error 参数序列化为 <Error {...}> 含 message/name/stack 摘要', () => {
    const log = logger.module('Err')
    log.error('失败: %s', new Error('boom'))
    const msg = String(consoleError.mock.calls[0][1])
    expect(msg).toContain('<Error')
    expect(msg).toContain('boom')
    expect(msg).toContain('Error')
  })

  it('Axios 形态错误（isAxiosError）序列化为 <AxiosError ...> 含 status/url/method', () => {
    const log = logger.module('Axios')
    const axiosLike = {
      message: 'Request failed',
      name: 'AxiosError',
      isAxiosError: true,
      code: 'ERR_BAD_REQUEST',
      response: { status: 404 },
      config: { url: '/api/v1/x', method: 'get' },
      stack: 'line1\nline2\nline3\nline4',
    }
    log.warn('请求失败: %s', axiosLike)
    const msg = String(consoleWarn.mock.calls[0][1])
    expect(msg).toContain('<AxiosError')
    expect(msg).toContain('404')
    expect(msg).toContain('/api/v1/x')
    expect(msg).toContain('get')
  })

  it('循环引用对象 → 降级为 [Circular]，不抛错', () => {
    const log = logger.module('Circ')
    const obj: any = { name: 'root' }
    obj.self = obj
    expect(() => log.info('circ=%j', obj)).not.toThrow()
    const msg = String(consoleLog.mock.calls[0][1])
    expect(msg).toContain('[Circular]')
  })

  it('module 缓存：同名模块返回同一实例；subModule 生成子模块名', () => {
    const m1 = logger.module('Same')
    const m2 = logger.module('Same')
    expect(m1).toBe(m2)
    m1.subModule('sub1').info('x')
    const prefix = String(consoleLog.mock.calls[0][0])
    expect(prefix).toContain('[Same:sub1]')
  })

  it('未使用占位符时原样输出（含对象参数 safeStringify）', () => {
    const log = logger.module('Plain')
    log.info('plain message')
    expect(String(consoleLog.mock.calls[0][1])).toBe('plain message')
    log.info('with obj', { k: 'v' })
    expect(String(consoleLog.mock.calls[1][1])).toBe('with obj {"k":"v"}')
  })
})
