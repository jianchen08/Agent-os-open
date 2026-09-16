// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * token 续期退避链 + 数据形状归一 覆盖缺口补测
 *
 * 断行为：公开 API 的输入 → 输出 / 状态副作用 / 告警，不断言私有字段。
 *
 * 覆盖契约：
 * - tokenLifecycle 主动续期瞬时失败：指数退避 30s → 60s → 120s（间隔逐次翻倍，
 *   非固定间隔）；连续失败超上限（10 次）停止重试并 console.error；
 * - tokenLifecycle refresh 复用 in-flight：并发两次 refresh 只打一次后端（幂等）；
 * - dataWidget 归一化：{items,total} 与 {results,total} 两种信封（区分输入）
 *   都解出 rows；series 非法输入 → 空数据集 / 合法输入数值化；
 *   未声明 shape → 原样透传（default 分支不做推测性归一）。
 *
 * 注：themeService.fetchDynamicThemes 的构建期主题导入（源码 814-825）未在此覆盖——
 * 该 import.meta.glob 在 vitest 与生产构建中均解析为空集（已实证，见测试报告），
 * 属源码缺口而非测试缺口，按约定只上报不修源码。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
const mockApiRefreshToken = vi.fn()
vi.mock('@/services/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  refreshToken: (...args: unknown[]) => mockApiRefreshToken(...args),
  getCurrentUser: vi.fn(),
  logout: vi.fn(),
  changePassword: vi.fn(),
}))

import {
  getAccessToken,
  getRefreshTokenValue,
  setTokens,
  clearTokens,
  refresh,
  startAutoRefresh,
  stopAutoRefresh,
} from '@/services/auth/tokenLifecycle'
import { normalizeRows, normalizeSeries, normalizeDataPayload } from '@/services/schema/dataWidget'

const BASE_TIME = new Date('2026-01-01T00:00:00Z').getTime()
const TTL_S = 100 // 100s TTL → 首次续期调度在 50s

describe('tokenLifecycle 主动续期指数退避与上限', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    clearTokens()
    mockApiRefreshToken.mockReset()
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    stopAutoRefresh()
    vi.useRealTimers()
  })

  it('瞬时失败按 30s → 60s → 120s 指数退避重试（间隔逐次翻倍，非固定间隔）', async () => {
    setTokens('at-1', 'rt-1', TTL_S)
    startAutoRefresh()
    mockApiRefreshToken.mockRejectedValue(new Error('Network Error'))

    // 以「距起点 + 已推进」的绝对时间断言，避免相对步进累积漂移
    let now = 0
    const at = async (t: number) => {
      await vi.advanceTimersByTimeAsync(t - now)
      now = t
    }

    // 首次续期点 50s 失败 → 第 1 次
    await at(50_000)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)

    // 首次退避 30s：79s 时仍未重试，81s 时第 2 次
    await at(79_000)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)
    await at(81_000)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(2)

    // 第二次退避 60s：139s 时仍 2 次，141s 时第 3 次（间隔翻倍）
    await at(139_000)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(2)
    await at(141_000)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(3)

    // 第三次退避 120s：259s 时仍 3 次，261s 时第 4 次
    await at(259_000)
    expect(mockApiRefreshToken.mock.calls.length).toBe(3)
    await at(261_000)
    expect(mockApiRefreshToken.mock.calls.length).toBe(4)
  })

  it('连续瞬时失败超过上限（10 次）→ 停止重试并 console.error 上报', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    setTokens('at-1', 'rt-1', TTL_S)
    startAutoRefresh()
    mockApiRefreshToken.mockRejectedValue(new Error('Network Error'))

    // 首次 50s + 退避链：30+60+120+240+300×N（300s 处封顶 5min）足够跑到上限
    // 逐段推进（每段后断言调用次数单调不减，避免依赖精确累加和）
    const steps = [
      50_000, 30_000, 60_000, 120_000, 240_000,
      300_000, 300_000, 300_000, 300_000, 300_000, 300_000, 300_000, 300_000,
    ]
    let prev = 0
    for (const s of steps) {
      await vi.advanceTimersByTimeAsync(s)
      const now = mockApiRefreshToken.mock.calls.length
      expect(now).toBeGreaterThanOrEqual(prev)
      prev = now
      if (now > 10) break
    }

    // 达到上限（连续失败 > 10）后链停止
    expect(mockApiRefreshToken.mock.calls.length).toBeGreaterThanOrEqual(10)
    const atStop = mockApiRefreshToken.mock.calls.length
    await vi.advanceTimersByTimeAsync(60 * 60 * 1000)
    expect(mockApiRefreshToken.mock.calls.length).toBe(atStop)
    expect(errorSpy).toHaveBeenCalledWith(
      expect.stringContaining('token 主动续期连续失败'),
      expect.any(Number),
    )
    errorSpy.mockRestore()
  })

  it('并发两次 refresh：复用 in-flight，只打一次后端（幂等）', async () => {
    setTokens('at-1', 'rt-1', TTL_S)
    mockApiRefreshToken.mockResolvedValue({
      access_token: 'at-concurrent',
      refresh_token: 'rt-concurrent',
      expires_in: TTL_S,
    })

    const [a, b] = await Promise.all([refresh(), refresh()])

    expect(a).toBeUndefined()
    expect(b).toBeUndefined()
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)
    expect(getAccessToken()).toBe('at-concurrent')
  })
})

describe('tokenLifecycle 存储故障容错', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    clearTokens()
    mockApiRefreshToken.mockReset()
    vi.useFakeTimers()
    vi.useRealTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('localStorage 读取抛异常（隐私模式/存储不可用）→ getRefreshTokenValue 返回 null 不抛出', () => {
    const spy = vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError: storage disabled')
    })

    expect(() => getRefreshTokenValue()).not.toThrow()
    expect(getRefreshTokenValue()).toBeNull()
    expect(spy).toHaveBeenCalled()

    spy.mockRestore()
    // 对照：存储恢复后同 key 正常读回写入值（两组输入区分）
    setTokens('at-ok', 'rt-persisted', TTL_S)
    expect(getRefreshTokenValue()).toBe('rt-persisted')
  })
})

describe('dataWidget 信封与形状归一（覆盖缺口）', () => {
  it('{items,total} 与 {results,total} 两种信封都解出 rows（区分输入）', () => {
    const itemsEnv = normalizeRows({ items: [{ id: 1 }, { id: 2 }], total: 2 })
    expect(itemsEnv.rows).toHaveLength(2)
    expect(itemsEnv.columns.map((c) => c.key)).toContain('id')

    const resultsEnv = normalizeRows({ results: [{ name: 'a' }], total: 1 })
    expect(resultsEnv.rows).toHaveLength(1)
    expect(resultsEnv.columns.map((c) => c.key)).toContain('name')

    // 非数组的 items 不误当数据源
    expect(normalizeRows({ items: 'not-array' }).rows).toHaveLength(0)
  })

  it('series 非法输入（null / 非法 datasets）→ 空数据集', () => {
    expect(normalizeSeries(null).datasets).toHaveLength(0)
    expect(normalizeSeries({ datasets: [{ data: 'nope' }] }).datasets).toHaveLength(0)
    expect(normalizeSeries({ labels: ['a'] }).labels).toEqual([])
  })

  it('series 合法：datasets 数值化并保留标签/颜色元信息', () => {
    const out = normalizeSeries({
      labels: ['一月', '二月'],
      datasets: [{ data: ['1', 2], label: '访问量', color: '#f00', backgroundColor: '#fee' }],
    })
    expect(out.labels).toEqual(['一月', '二月'])
    expect(out.datasets).toHaveLength(1)
    expect(out.datasets[0].data).toEqual([1, 2])
    expect(out.datasets[0]).toMatchObject({
      label: '访问量',
      color: '#f00',
      backgroundColor: '#fee',
    })
  })

  it('未声明 shape（default 分支）→ 原样透传，不做推测性归一', () => {
    const payload = { anything: [1, 2, 3] }
    expect(normalizeDataPayload(payload, 'unknown' as never)).toBe(payload)
  })
})
