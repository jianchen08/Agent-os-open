/** @feature FP-0.2.多端同步 借权免登服务 | @ci: frontend-test */
// @ci: frontend-test
/**
 * 借权服务测试（ADR 2026-10-22 借权免登）：本机内核代持令牌向目标借权 → 令牌
 * 按 @base 分键落盘 → 切换。
 *
 * 断行为：
 *  - 方向铁律：借权请求打**本机**（axios baseURL=''），绝不打当前远端基址；
 *  - 落盘点：refresh_token@<目标基址>（分键，与本地裸键隔离）；
 *  - 失败闭面：本机不可达/目标拒绝/协议违约 → 不切换，回传可手动兜底标记。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { borrowAndConnect, borrowSession, borrowViaShellDevice, mintLocalKernelToken } from '../deviceBorrow'

const postMock = vi.hoisted(() => vi.fn())
const connectMock = vi.hoisted(() => vi.fn())
const shellCredMock = vi.hoisted(() => vi.fn(async () => null))

vi.mock('axios', () => ({
  default: { create: () => ({ post: postMock }) },
}))
vi.mock('@/pages/settings/devices/connectKernel', () => ({
  connectKernel: connectMock,
  RELOAD_DELAY_MS: 0,
}))
vi.mock('@/services/shellLocalAuth', () => ({
  loadShellLocalCredential: shellCredMock,
  shellAutoLoginDisabled: vi.fn(() => false),
}))


function b64(obj: unknown): string {
  return btoa(unescape(encodeURIComponent(JSON.stringify(obj))))
}

/** 壳层配对设备凭据线格式（app-mobile/src/sync/credential.ts 契约同款） */
const SHELL_SYNC_CRED = {
  relay_url: 'http://203.0.113.153/ext/sync_relay',
  device_id: 'dev-1',
  device_token: 'pdtk-1',
  tenant_id: 't1',
  tier: '',
  app_version: '',
  paired_at: '2026-10-26T00:00:00Z',
}

type PreferencesStub = { get: (opts: { key: string }) => Promise<{ value?: string | null }> }

function stubPreferences(get: PreferencesStub['get'] | null): void {
  const w = globalThis as { Capacitor?: { Plugins?: Record<string, unknown> } }
  w.Capacitor = get ? { Plugins: { Preferences: { get } } } : undefined
}

function stubHostCredential(): void {
  ;(window as unknown as { electronAPI?: unknown }).electronAPI = {
    adminCredential: {
      load: vi.fn(async () => ({ username: 'admin', password: 'seed-pw' })),
    },
  }
}

beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  postMock.mockReset()
  connectMock.mockReset()
  shellCredMock.mockReset()
  shellCredMock.mockResolvedValue(null)
  connectMock.mockResolvedValue({ ok: true })
  ;(window as unknown as { electronAPI?: unknown }).electronAPI = undefined
  stubPreferences(null)
})

describe('mintLocalKernelToken', () => {
  it('装机凭据 → 本机 login 换发令牌', async () => {
    stubHostCredential()
    postMock.mockResolvedValueOnce({ data: { access_token: 'local-at' } })

    expect(await mintLocalKernelToken()).toBe('local-at')
    expect(postMock).toHaveBeenCalledWith('/api/v1/auth/login', {
      username: 'admin',
      password: 'seed-pw',
    })
  })

  it('无凭据（dev/未播种）→ null（借权显式不可用，不静默降级）', async () => {
    expect(await mintLocalKernelToken()).toBeNull()
    expect(postMock).not.toHaveBeenCalled()
  })
})

describe('borrowSession', () => {
  it('解包 http.handle 响应体（data.body=base64）并取令牌对', async () => {
    postMock.mockResolvedValueOnce({
      data: { body: b64({ access_token: 'at', refresh_token: 'rt' }) },
    })

    const r = await borrowSession('http://203.0.113.153', 'local-token', true)
    expect(r.ok).toBe(true)
    expect(r.refreshToken).toBe('rt')
    expect(postMock).toHaveBeenCalledWith(
      '/ext/sync_agent/borrow',
      { target_base: 'http://203.0.113.153', target_is_hub: true },
      { headers: { Authorization: 'Bearer local-token' } },
    )
  })

  it('is_hub 缺省 → 自动判别（中枢端点未命中转成员端点）', async () => {
    postMock
      .mockRejectedValueOnce({ response: { status: 404 } })
      .mockResolvedValueOnce({ data: { body: b64({ access_token: 'at', refresh_token: 'rt' }) } })

    const r = await borrowSession('http://10.0.0.8:9100', 'local-token')
    expect(r.ok).toBe(true)
    expect(postMock).toHaveBeenCalledTimes(2)
    expect(postMock.mock.calls[0][1]).toMatchObject({ target_is_hub: true })
    expect(postMock.mock.calls[1][1]).toMatchObject({ target_is_hub: false })
  })
})

describe('borrowAndConnect', () => {
  it('成功 → 令牌按 @base 分键落盘 + 切换（零登录面）', async () => {
    stubHostCredential()
    postMock
      .mockResolvedValueOnce({ data: { access_token: 'local-at' } })
      .mockResolvedValueOnce({ data: { body: b64({ access_token: 'at', refresh_token: 'rt' }) } })

    const r = await borrowAndConnect('http://203.0.113.153', true)
    expect(r.ok).toBe(true)
    expect(localStorage.getItem('refresh_token@http://203.0.113.153')).toBe('rt')
    expect(connectMock).toHaveBeenCalledWith('http://203.0.113.153')
  })

  it('借权失败 → 不落盘不切换，回传可手动兜底', async () => {
    stubHostCredential()
    postMock
      .mockResolvedValueOnce({ data: { access_token: 'local-at' } })
      .mockRejectedValueOnce({ response: { status: 403 } })

    const r = await borrowAndConnect('http://203.0.113.153', true)
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.canRetryManual).toBe(true)
    expect(localStorage.getItem('refresh_token@http://203.0.113.153')).toBeNull()
    expect(connectMock).not.toHaveBeenCalled()
  })
})

describe('borrowViaShellDevice（移动壳配对设备直借·远端免登腿）', () => {
  it('Preferences 凭据 → Bearer per-device token 直打 relay /borrow_auth，回传令牌对', async () => {
    stubPreferences(async () => ({ value: JSON.stringify(SHELL_SYNC_CRED) }))
    postMock.mockResolvedValueOnce({
      data: { access_token: 'at-relay', refresh_token: 'rt-relay', expires_in: 3600 },
    })

    const r = await borrowViaShellDevice('http://203.0.113.153')
    expect(r.ok).toBe(true)
    expect(r.refreshToken).toBe('rt-relay')
    expect(postMock).toHaveBeenCalledWith(
      'http://203.0.113.153/ext/sync_relay/borrow_auth',
      null,
      { headers: { Authorization: 'Bearer pdtk-1' } },
    )
  })

  it('Preferences 缺席 → localStorage 同键兜底（shell-sync 降级通道同读）', async () => {
    localStorage.setItem('lingxi_sync_credential', JSON.stringify(SHELL_SYNC_CRED))
    postMock.mockResolvedValueOnce({
      data: { access_token: 'at-relay', refresh_token: 'rt-relay' },
    })

    const r = await borrowViaShellDevice('http://203.0.113.153')
    expect(r.ok).toBe(true)
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('未配对（两通道皆空）→ null 且不发起请求', async () => {
    const r = await borrowViaShellDevice('http://203.0.113.153')
    expect(r.ok).toBe(false)
    expect(postMock).not.toHaveBeenCalled()
  })

  it('凭据坏行（缺 device_token）→ null 不请求（契约缺面不外发）', async () => {
    localStorage.setItem(
      'lingxi_sync_credential',
      JSON.stringify({ ...SHELL_SYNC_CRED, device_token: '' }),
    )
    const r = await borrowViaShellDevice('http://203.0.113.153')
    expect(r.ok).toBe(false)
    expect(postMock).not.toHaveBeenCalled()
  })

  it('relay 403（令牌不识别=未配对/已解绑）→ 显式失败回传，可手动兜底', async () => {
    stubPreferences(async () => ({ value: JSON.stringify(SHELL_SYNC_CRED) }))
    postMock.mockRejectedValueOnce({
      response: { status: 403, data: { error: { message: 'unrecognized per-device token' } } },
    })

    const r = await borrowViaShellDevice('http://203.0.113.153')
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.canRetryManual).toBe(true)
  })

  it('目标与配对 relay 异源 → 不出手（中枢令牌不落错键、不进异源 refresh 面）', async () => {
    stubPreferences(async () => ({ value: JSON.stringify(SHELL_SYNC_CRED) }))

    const r = await borrowViaShellDevice('http://10.0.0.8:9100')
    expect(r.ok).toBe(false)
    expect(postMock).not.toHaveBeenCalled()
    expect(localStorage.getItem('refresh_token@http://10.0.0.8:9100')).toBeNull()
  })

  it('响应缺令牌（协议违约）→ 显式失败不落盘', async () => {
    stubPreferences(async () => ({ value: JSON.stringify(SHELL_SYNC_CRED) }))
    postMock.mockResolvedValueOnce({ data: { verified_device: 'dev-1' } })

    const r = await borrowViaShellDevice('http://203.0.113.153')
    expect(r.ok).toBe(false)
    expect(localStorage.getItem('refresh_token@http://203.0.113.153')).toBeNull()
  })
})
