/** @feature FP-0.2.多端同步 借权免登服务 | @ci: frontend-test */
/**
 * 借权服务缺口补测（ADR 2026-10-22 借权免登）：错误面与降级面的闭合行为。
 *
 * 既有 deviceBorrow.test.ts 覆盖主路径（mint→handshake→落盘→切换、403/404
 * 判别），本文件只补错误/降级分支：
 * - 响应体解包失败 / 缺令牌 = 目标内核协议违约 → 显式失败（不静默当成功）
 * - 本机 login 失败 → null（借权显式不可用）
 * - 壳层凭据 Preferences 读失败 → localStorage 同键兜底；
 *   storage 读不可用 / 坏 JSON → 无凭据（不请求）
 * - borrowAndConnect：无本机令牌 / 落盘不可用 / 探活失败 → 不切换连接，
 *   回传 canRetryManual（卡片「仍要切换」逃生口）
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  borrowAndConnect,
  borrowSession,
  borrowViaShellDevice,
  mintLocalKernelToken,
} from '../deviceBorrow'

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

const TARGET = 'http://203.0.113.153'

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

function stubPreferences(get: ((opts: { key: string }) => Promise<{ value?: string | null }>) | null): void {
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

/** 临时让 localStorage 读取抛错；await 覆盖整个异步调用面（否则覆写会提前还原） */
async function withBrokenRead<T>(fn: () => Promise<T>): Promise<T> {
  const storage = window.localStorage
  const original = storage.getItem
  storage.getItem = () => {
    throw new Error('storage unavailable')
  }
  try {
    return await fn()
  } finally {
    storage.getItem = original
  }
}

/** 临时让 localStorage 写入抛错；await 覆盖整个异步调用面 */
async function withBrokenWrite<T>(fn: () => Promise<T>): Promise<T> {
  const storage = window.localStorage
  const original = storage.setItem
  storage.setItem = () => {
    throw new Error('quota exceeded')
  }
  try {
    return await fn()
  } finally {
    storage.setItem = original
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

describe('borrowSession — 目标内核协议违约', () => {
  it('响应体 base64 解包失败（非 JSON）→ 协议违约错误，不重试', async () => {
    postMock.mockResolvedValueOnce({ data: { body: btoa('not-json') } })

    const r = await borrowSession(TARGET, 'local-token', true)
    expect(r).toEqual({ ok: false, error: '借权响应缺令牌（目标内核协议违约）' })
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('网络错误（无 response.status）→ 立即失败不重试（无端点判别依据）', async () => {
    postMock.mockRejectedValueOnce(new Error('Network Error'))

    const r = await borrowSession(TARGET, 'local-token')
    expect(r).toEqual({ ok: false, error: 'Network Error' })
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('503（对方未配对/管理口令缺失）→ 显式失败文案，不重试', async () => {
    postMock.mockRejectedValueOnce({ response: { status: 503 } })

    const r = await borrowSession(TARGET, 'local-token')
    expect(r).toEqual({
      ok: false,
      error: '目标内核借权不可用（未配对或管理口令缺失）',
    })
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('错误信封字符串形态（data.error=字符串）→ 原样冒泡为失败原因', async () => {
    postMock.mockRejectedValueOnce({ response: { status: 400, data: { error: '目标拒绝借权' } } })

    const r = await borrowSession(TARGET, 'local-token')
    expect(r).toEqual({ ok: false, error: '目标拒绝借权' })
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('响应体解包成功但缺令牌字段 → 协议违约错误（不把半截响应当成功）', async () => {
    postMock.mockResolvedValueOnce({ data: { body: b64({ verified_device: 'dev-1' }) } })

    const r = await borrowSession(TARGET, 'local-token', true)
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toBe('借权响应缺令牌（目标内核协议违约）')
    expect(postMock).toHaveBeenCalledTimes(1)
  })
})

describe('mintLocalKernelToken — 本机 login 失败面', () => {
  it('本机 login 请求失败 → null（借权显式不可用，不抛错冒泡）', async () => {
    stubHostCredential()
    postMock.mockRejectedValueOnce({ response: { status: 500 } })

    expect(await mintLocalKernelToken()).toBeNull()
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('宿主凭据为空（load 返回 null）→ null，不发 login', async () => {
    ;(window as unknown as { electronAPI?: unknown }).electronAPI = {
      adminCredential: { load: vi.fn(async () => null) },
    }

    expect(await mintLocalKernelToken()).toBeNull()
    expect(postMock).not.toHaveBeenCalled()
  })

  it('本机 login 响应缺 access_token → null（不把 undefined 冒泡成令牌）', async () => {
    stubHostCredential()
    postMock.mockResolvedValueOnce({ data: {} })

    expect(await mintLocalKernelToken()).toBeNull()
  })
})

describe('borrowViaShellDevice — 壳层凭据读取降级', () => {
  it('Preferences 读失败 → localStorage 同键兜底，借权照常发出', async () => {
    stubPreferences(async () => {
      throw new Error('Preferences unavailable')
    })
    localStorage.setItem('lingxi_sync_credential', JSON.stringify(SHELL_SYNC_CRED))
    postMock.mockResolvedValueOnce({ data: { access_token: 'at-relay', refresh_token: 'rt-relay' } })

    const r = await borrowViaShellDevice(TARGET)
    expect(r.ok).toBe(true)
    expect(postMock).toHaveBeenCalledWith(
      'http://203.0.113.153/ext/sync_relay/borrow_auth',
      null,
      { headers: { Authorization: 'Bearer pdtk-1' } },
    )
  })

  it('Preferences 返回空值 → localStorage 同键兜底（空值不等于无凭据）', async () => {
    stubPreferences(async () => ({ value: null }))
    localStorage.setItem('lingxi_sync_credential', JSON.stringify(SHELL_SYNC_CRED))
    postMock.mockResolvedValueOnce({ data: { access_token: 'at-relay', refresh_token: 'rt-relay' } })

    const r = await borrowViaShellDevice(TARGET)
    expect(r.ok).toBe(true)
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('storage 读不可用 → 视为无凭据（未入网），不发起请求', async () => {
    const r = await withBrokenRead(() => borrowViaShellDevice(TARGET))

    expect(r).toEqual({ ok: false, error: '壳层无配对设备凭据（未入网）' })
    expect(postMock).not.toHaveBeenCalled()
  })

  it('凭据坏 JSON → 视为无凭据，不发起请求（坏行不外发）', async () => {
    localStorage.setItem('lingxi_sync_credential', '{not-json')

    const r = await borrowViaShellDevice(TARGET)
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toBe('壳层无配对设备凭据（未入网）')
    expect(postMock).not.toHaveBeenCalled()
  })
})

describe('borrowAndConnect — 切换前的失败面', () => {
  it('无本机令牌（未播种凭据）→ 显式失败可手动兜底，不发起借权', async () => {
    const r = await borrowAndConnect(TARGET, true)

    expect(r.ok).toBe(false)
    if (!r.ok) {
      expect(r.error).toBe('本机内核不可达或未播种凭据，借权不可用')
      expect(r.canRetryManual).toBe(true)
    }
    expect(postMock).not.toHaveBeenCalled()
    expect(connectMock).not.toHaveBeenCalled()
  })

  it('令牌落盘不可用 → 不切换连接（拒绝「切了但刷新即掉线」的半态）', async () => {
    stubHostCredential()
    postMock
      .mockResolvedValueOnce({ data: { access_token: 'local-at' } })
      .mockResolvedValueOnce({ data: { body: b64({ access_token: 'at', refresh_token: 'rt' }) } })

    const outcome = await withBrokenWrite(() => borrowAndConnect(TARGET, true))

    expect(outcome.ok).toBe(false)
    if (!outcome.ok) {
      expect(outcome.error).toBe('本地存储不可用，未切换连接')
      expect(outcome.canRetryManual).toBe(true)
    }
    expect(connectMock).not.toHaveBeenCalled()
  })

  it('探活切换失败 → 回传探活错误可手动兜底', async () => {
    stubHostCredential()
    postMock
      .mockResolvedValueOnce({ data: { access_token: 'local-at' } })
      .mockResolvedValueOnce({ data: { body: b64({ access_token: 'at', refresh_token: 'rt' }) } })
    connectMock.mockResolvedValueOnce({ ok: false, error: '目标内核探活失败' })

    const r = await borrowAndConnect(TARGET, true)

    expect(r).toEqual({ ok: false, error: '目标内核探活失败', canRetryManual: true })
    expect(localStorage.getItem(`refresh_token@${TARGET}`)).toBe('rt')
  })
})
