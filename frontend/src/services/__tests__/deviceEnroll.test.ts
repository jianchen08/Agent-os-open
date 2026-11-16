/** @feature FP-0.2.多端同步 登录即入网 | @ci: frontend-test */
// @ci: frontend-test
/**
 * 登录即入网服务测试（ADR 2026-11-02-login-as-enrollment）：admin 登录态直签
 * 入网，无用户侧配对动作。
 *
 * 断行为：
 *  - admin 门控：非 admin/无用户 → 零网络调用（签发面 admin 专属，无提权）；
 *  - 壳腿：LingxiSync 在场 → joinWithAuth(jwt, base)，已配对跳过；
 *  - 桌面腿：status paired=true 跳过；未入网 → 启用 relay + sync_agent /enroll
 *    代理直签（relay_url=基址+/ext/sync_relay，本机闸 Bearer=本机身份）；
 *  - 候选回退：当前基址失败 → localStorage refresh_token@<base> 换发后重试；
 *    本机闸 403 终止（换 relay 无意义）；全部失败静默不抛。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { enrollAfterLogin } from '../deviceEnroll'

const localGet = vi.hoisted(() => vi.fn())
const localPost = vi.hoisted(() => vi.fn())
const axiosPost = vi.hoisted(() => vi.fn())
const axiosPut = vi.hoisted(() => vi.fn())
const ensureFreshTokenMock = vi.hoisted(() => vi.fn())
const mintMock = vi.hoisted(() => vi.fn())
const resolveApiBaseMock = vi.hoisted(() => vi.fn())

vi.mock('axios', () => ({
  default: {
    create: () => ({ get: localGet, post: localPost }),
    post: axiosPost,
    put: axiosPut,
  },
}))
vi.mock('@/services/auth/tokenLifecycle', () => ({
  ensureFreshToken: ensureFreshTokenMock,
}))
vi.mock('@/services/shellApiBase', () => ({
  resolveApiBase: resolveApiBaseMock,
}))
vi.mock('@/services/deviceBorrow', () => ({
  mintLocalKernelToken: mintMock,
  // 直测只喂直接体形态（base64 信封形态由 deviceBorrow 自测覆盖）
  unwrapBody: (payload: unknown) => payload,
}))

const ADMIN = { role: 'admin' } as const
const USER = { role: 'user' } as const

function stubShell(bridge: unknown): void {
  ;(globalThis as { LingxiSync?: unknown }).LingxiSync = bridge
}

beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  stubShell(undefined)
  ensureFreshTokenMock.mockResolvedValue('cur-jwt')
  mintMock.mockResolvedValue('local-jwt')
  localGet.mockResolvedValue({ data: { paired: false } })
  resolveApiBaseMock.mockReturnValue('http://hub.example')
})

describe('admin 门控', () => {
  it.each([
    ['非 admin 用户', USER],
    ['无用户（未加载）', null],
  ])('%s → 零网络调用（静默跳过）', async (_label, user) => {
    await enrollAfterLogin(user as { role: 'admin' | 'user' } | null)
    expect(localGet).not.toHaveBeenCalled()
    expect(localPost).not.toHaveBeenCalled()
    expect(ensureFreshTokenMock).not.toHaveBeenCalled()
  })

  it('令牌面异常 → 外层捕获静默（副链路不阻塞登录）', async () => {
    ensureFreshTokenMock.mockRejectedValue(new Error('token store broken'))
    await expect(enrollAfterLogin(ADMIN)).resolves.toBeUndefined()
  })
})

describe('壳腿（window.LingxiSync 在场）', () => {
  it('未配对 → joinWithAuth(jwt, 当前基址)；已配对 → 不调用', async () => {
    const joinWithAuth = vi.fn(async () => undefined)
    stubShell({ joinWithAuth, engine: { status: () => ({ paired: false }) } })
    await enrollAfterLogin(ADMIN)
    expect(joinWithAuth).toHaveBeenCalledWith('cur-jwt', 'http://hub.example')

    const pairedShell = { joinWithAuth: vi.fn(), engine: { status: () => ({ paired: true }) } }
    stubShell(pairedShell)
    await enrollAfterLogin(ADMIN)
    expect(pairedShell.joinWithAuth).not.toHaveBeenCalled()
  })
})

describe('桌面腿（无壳）', () => {
  it('status paired=true → 零 enroll 调用（幂等跳过）', async () => {
    localGet.mockResolvedValue({ data: { paired: true } })
    await enrollAfterLogin(ADMIN)
    expect(localPost).not.toHaveBeenCalled()
  })

  it('远端档未入网：启用 relay + sync_agent /enroll（relay_url 拼基址，本机闸走 mint 身份）', async () => {
    axiosPut.mockResolvedValue({ data: {} })
    localPost.mockResolvedValue({ data: { paired: true } })
    await enrollAfterLogin(ADMIN)
    expect(axiosPut).toHaveBeenCalledWith(
      'http://hub.example/api/v1/plugins/sync_relay/enabled',
      { enabled: true },
      { headers: { Authorization: 'Bearer cur-jwt' }, timeout: 15_000 },
    )
    expect(localPost).toHaveBeenCalledWith(
      '/ext/sync_agent/enroll',
      { relay_url: 'http://hub.example/ext/sync_relay', admin_jwt: 'cur-jwt' },
      { headers: { Authorization: 'Bearer local-jwt' } },
    )
  })

  it('本机档（基址空串）：候选=页面 origin，本机闸直用当前 JWT', async () => {
    resolveApiBaseMock.mockReturnValue('')
    axiosPut.mockRejectedValue(new Error('already enabled'))
    localPost.mockResolvedValue({ data: { paired: true } })
    await enrollAfterLogin(ADMIN)
    expect(mintMock).not.toHaveBeenCalled()
    expect(localPost).toHaveBeenCalledWith(
      '/ext/sync_agent/enroll',
      {
        relay_url: `${window.location.origin}/ext/sync_relay`,
        admin_jwt: 'cur-jwt',
      },
      { headers: { Authorization: 'Bearer cur-jwt' } },
    )
  })

  it('当前基址失败 → 存储会话换发（refresh@base）回退第二候选；403 终止不回退', async () => {
    localStorage.setItem('refresh_token@http://hub2.example', 'rt-2')
    axiosPut.mockResolvedValue({ data: {} })
    axiosPost.mockResolvedValue({ data: { access_token: 'hub2-jwt' } })
    localPost
      .mockResolvedValueOnce({ data: { paired: false } }) // hub.example 响应未配对
      .mockResolvedValueOnce({ data: { paired: true } }) // hub2 成
    await enrollAfterLogin(ADMIN)
    expect(axiosPost).toHaveBeenCalledWith(
      'http://hub2.example/api/v1/auth/refresh',
      { refresh_token: 'rt-2' },
      { headers: { Authorization: '' }, timeout: 15_000 },
    )
    expect(localPost).toHaveBeenCalledTimes(2)
    expect(localPost).toHaveBeenLastCalledWith(
      '/ext/sync_agent/enroll',
      { relay_url: 'http://hub2.example/ext/sync_relay', admin_jwt: 'hub2-jwt' },
      { headers: { Authorization: 'Bearer local-jwt' } },
    )
  })

  it('本机闸 403（身份不符）→ 终止候选遍历，不试存储会话', async () => {
    localStorage.setItem('refresh_token@http://hub2.example', 'rt-2')
    axiosPut.mockResolvedValue({ data: {} })
    const err = Object.assign(new Error('forbidden'), { response: { status: 403 } })
    localPost.mockRejectedValue(err)
    await enrollAfterLogin(ADMIN)
    expect(localPost).toHaveBeenCalledTimes(1)
    expect(axiosPost).not.toHaveBeenCalled()
  })

  it('status 查询不可达 → 按未配对继续入网', async () => {
    localGet.mockRejectedValue(new Error('kernel down'))
    axiosPut.mockResolvedValue({ data: {} })
    localPost.mockResolvedValue({ data: { paired: true } })
    await enrollAfterLogin(ADMIN)
    expect(localPost).toHaveBeenCalledTimes(1)
  })

  it('远端档且本机凭据未播种（mint null）→ 跳过入网', async () => {
    mintMock.mockResolvedValue(null)
    await enrollAfterLogin(ADMIN)
    expect(localPost).not.toHaveBeenCalled()
  })

  it('存储会话 refresh 失败 → 该候选跳过（换发 null 不入网）', async () => {
    localStorage.setItem('refresh_token@http://hub2.example', 'rt-2')
    axiosPut.mockResolvedValue({ data: {} })
    localPost.mockResolvedValue({ data: { paired: false } })
    axiosPost.mockRejectedValue(new Error('refresh dead'))
    await enrollAfterLogin(ADMIN)
    expect(localPost).toHaveBeenCalledTimes(1)
  })

  it('全部候选失败 → 静默返回不抛（副链路口径）；字符串错误体走原样呈现', async () => {
    axiosPut.mockResolvedValue({ data: {} })
    const err = Object.assign(new Error('x'), {
      response: { status: 502, data: { error: 'relay 拒绝（原样串）' } },
    })
    localPost.mockRejectedValue(err)
    await expect(enrollAfterLogin(ADMIN)).resolves.toBeUndefined()
  })
})
