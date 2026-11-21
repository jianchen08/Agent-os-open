/** @feature: FP-0.2.登录失败分类 登录失败三分类横幅 | @ci: frontend-test */
/**
 * 登录失败三分类与横幅文案（S5-A②/S5-D①）：
 * - 判定只认稳定契约（拦截器附着的机器码/HTTP 状态串/axios 错误码），不做文案匹配
 * - 429 限速锁=终态（带 Retry-After 秒数）；凭据错=后端原文；网络失败=无法连接内核+目标基址
 * - 同一行为 ≥2 组有区分度输入（机器码形态/状态码回退形态/无响应形态）
 */
import { describe, expect, it } from 'vitest'
import { classifyLoginFailure, loginFailureBanner } from '../loginFailure'

/** 拦截器 reject 形状：Object.assign(new Error(msg), apiError) */
function rejected(message: string, extra: Record<string, unknown>): Error {
  return Object.assign(new Error(message), extra)
}

describe('classifyLoginFailure', () => {
  it('429 限速锁：机器码 TOO_MANY_REQUESTS 命中，携带 Retry-After 秒数', () => {
    const failure = classifyLoginFailure(
      rejected('登录失败次数过多，请 900 秒后重试', {
        code: 'TOO_MANY_REQUESTS',
        retry_after_secs: 900,
      }),
    )
    expect(failure.kind).toBe('rate_limited')
    expect(failure.retryAfterSecs).toBe(900)
  })

  it('429 限速锁：无信封时状态码字符串 "429" 回退命中', () => {
    expect(classifyLoginFailure(rejected('x', { code: '429' })).kind).toBe('rate_limited')
  })

  it('凭据错（登录端点凭据不匹配族）：BAD_REQUEST/UNAUTHORIZED 及状态串回退', () => {
    expect(
      classifyLoginFailure(rejected('用户名或密码错误', { code: 'BAD_REQUEST' })).kind,
    ).toBe('credential')
    expect(classifyLoginFailure(rejected('x', { code: '401' })).kind).toBe('credential')
    expect(classifyLoginFailure(rejected('x', { code: '400' })).kind).toBe('credential')
  })

  it('网络失败（无响应）：axios 错误码与 "Network Error" 原文两形态', () => {
    expect(classifyLoginFailure(rejected('Network Error', { code: 'ERR_NETWORK' })).kind).toBe(
      'network',
    )
    expect(classifyLoginFailure(rejected('Network Error', {})).kind).toBe('network')
    expect(classifyLoginFailure(rejected('x', { code: 'ECONNABORTED' })).kind).toBe('network')
  })

  it('网络失败细分：超时族（ECONNABORTED/ETIMEDOUT）timedOut=true，连不上 timedOut=false', () => {
    // 超时=请求已发出但内核未在窗口内应答（两错误码同族）
    expect(classifyLoginFailure(rejected('timeout of 120000ms exceeded', { code: 'ECONNABORTED' }))).toEqual({
      kind: 'network',
      timedOut: true,
    })
    expect(classifyLoginFailure(rejected('x', { code: 'ETIMEDOUT' }))).toEqual({
      kind: 'network',
      timedOut: true,
    })
    // 连不上=根本没建立连接（axios 把 ECONNREFUSED 等包装为 ERR_NETWORK）
    expect(classifyLoginFailure(rejected('Network Error', { code: 'ERR_NETWORK' }))).toEqual({
      kind: 'network',
      timedOut: false,
    })
    expect(classifyLoginFailure(rejected('Network Error', {}))).toEqual({
      kind: 'network',
      timedOut: false,
    })
  })

  it('其余错误（含普通 Error/非对象）：unknown 不误判', () => {
    expect(classifyLoginFailure(new Error('connection refused')).kind).toBe('unknown')
    expect(classifyLoginFailure('boom').kind).toBe('unknown')
    expect(classifyLoginFailure(rejected('x', { code: 'INTERNAL_ERROR' })).kind).toBe('unknown')
  })
})

describe('loginFailureBanner', () => {
  it('429：锁定文案含秒数与重启逃生口，不诱导继续输入', () => {
    const text = loginFailureBanner({ kind: 'rate_limited', retryAfterSecs: 900 }, '')
    expect(text).toBe('尝试过多已锁定，请等待 900 秒或重启应用')
  })

  it('429：Retry-After 缺失时降级为无秒数锁定文案', () => {
    expect(loginFailureBanner({ kind: 'rate_limited' }, '')).toBe(
      '尝试过多已锁定，请等待锁定期结束或重启应用',
    )
  })

  it('网络失败：无法连接内核 + 附当前目标基址（本机/远程可辨别，S5-D①）', () => {
    const text = loginFailureBanner({ kind: 'network' }, 'http://10.0.2.2:9')
    expect(text).toBe('无法连接内核（目标：http://10.0.2.2:9），请确认服务已启动或检查服务器地址')
  })

  it('网络失败超时分支：内核响应超时 + 「再点一次登录」行动指引（不误导查服务）', () => {
    const text = loginFailureBanner({ kind: 'network', timedOut: true }, 'http://10.0.2.2:9')
    expect(text).toBe('内核响应超时（目标：http://10.0.2.2:9）（启动中或繁忙），请再点一次登录')
  })

  it('网络失败超时分支：无基址（同源）不输出空目标段', () => {
    expect(loginFailureBanner({ kind: 'network', timedOut: true }, '')).toBe(
      '内核响应超时（启动中或繁忙），请再点一次登录',
    )
  })

  it('网络失败：无基址（同源）时不输出空目标段', () => {
    expect(loginFailureBanner({ kind: 'network' }, '')).toBe(
      '无法连接内核，请确认服务已启动或检查服务器地址',
    )
  })

  it('凭据错：透传后端原文；缺失回退固定文案', () => {
    expect(loginFailureBanner({ kind: 'credential' }, '', '用户名或密码错误')).toBe(
      '用户名或密码错误',
    )
    expect(loginFailureBanner({ kind: 'credential' }, '')).toBe('用户名或密码错误')
  })

  it('unknown：保持原文兜底', () => {
    expect(loginFailureBanner({ kind: 'unknown' }, '', 'connection refused')).toBe(
      'connection refused',
    )
    expect(loginFailureBanner({ kind: 'unknown' }, '')).toBe('登录失败')
  })
})
