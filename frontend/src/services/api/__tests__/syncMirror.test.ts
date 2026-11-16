/** @feature FP-0.2.多端同步 T3 前置·只读快照 UI | @ci: frontend-test */
/**
 * syncMirror 纯函数助手 — 镜像记录的归属过滤/呈现排序/快照时刻/时刻格式化。
 * 可枚举输入参数化展开；排序断顺序不变量（乱序输入 → seq 单调）。
 * fetchRelayDevices — 云端权威注册表换源（R30）：URL 单点 + 降级→错误态映射。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from '@/services/api/client'
import {
  fetchRelayDevices,
  formatSnapshotTime,
  messagesOfSession,
  sessionsOfTerritory,
  snapshotTimeOf,
  type MirrorRecord,
} from '../syncMirror'

vi.mock('../client', () => ({
  default: { get: vi.fn() },
}))

function session(threadId: string, owner: string, hlcMs: number, deleted = false): MirrorRecord {
  return {
    asset_key: threadId,
    table: 'sessions',
    pk: threadId,
    row: { thread_id: threadId },
    owner_device: owner,
    deleted,
    updated_at: '',
    view: null,
    hlc: { physical_ms: hlcMs, logical: 0, device_id: owner },
  }
}

function message(group: string, seq: number, owner: string, hlcMs: number, deleted = false): MirrorRecord {
  return {
    asset_key: `${group}/${seq}`,
    table: 'messages',
    pk: `${group}/${seq}`,
    row: { role: 'user', content: `m${seq}` },
    owner_device: owner,
    deleted,
    updated_at: '',
    view: { group, seq },
    hlc: { physical_ms: hlcMs, logical: 0, device_id: owner },
  }
}

describe('sessionsOfTerritory — 归属过滤 + 墓碑过滤', () => {
  it('只留指定属主且未删除的会话记录', () => {
    const records = [
      session('t1', 'desktop-a', 1000),
      session('t2', 'phone-b', 1000),
      session('t3', 'desktop-a', 1000, true),
    ]
    const result = sessionsOfTerritory(records, 'desktop-a')
    expect(result.map((r) => r.pk)).toEqual(['t1'])
  })

  it('空输入与全不匹配输入均返回空数组', () => {
    expect(sessionsOfTerritory([], 'desktop-a')).toEqual([])
    expect(sessionsOfTerritory([session('t1', 'phone-b', 1)], 'desktop-a')).toEqual([])
  })
})

describe('messagesOfSession — 分组过滤 + seq 单调呈现序', () => {
  it('按 view.group 过滤并按 view.seq 升序（乱序输入断顺序不变量）', () => {
    const records = [
      message('g1', 3, 'd', 30),
      message('g2', 1, 'd', 10),
      message('g1', 1, 'd', 10),
      message('g1', 2, 'd', 20, true),
    ]
    const result = messagesOfSession(records, 'g1')
    expect(result.map((r) => r.view?.seq)).toEqual([1, 3])
  })

  it('两组会话各自独立成序（≥2 组区分输入）', () => {
    const records = [message('g2', 5, 'd', 1), message('g1', 7, 'd', 1), message('g2', 6, 'd', 1)]
    expect(messagesOfSession(records, 'g1').map((r) => r.view?.seq)).toEqual([7])
    expect(messagesOfSession(records, 'g2').map((r) => r.view?.seq)).toEqual([5, 6])
  })

  it('无 view 的畸形记录不进呈现（防御）', () => {
    const record = { ...message('g1', 1, 'd', 1), view: null }
    expect(messagesOfSession([record], 'g1')).toEqual([])
  })
})

describe('snapshotTimeOf — 快照时刻 = 记录集最大 HLC 物理毫秒', () => {
  it('取最大值；单条/多条一致', () => {
    expect(snapshotTimeOf([session('t', 'd', 100), session('t2', 'd', 500)])).toBe(500)
    expect(snapshotTimeOf([message('g', 1, 'd', 42)])).toBe(42)
  })

  it('空记录集返回 null（无快照时刻可标）', () => {
    expect(snapshotTimeOf([])).toBeNull()
  })
})

describe('formatSnapshotTime — 本地时间 YYYY-MM-DD HH:mm', () => {
  it('按本地时区格式化为 分钟粒度', () => {
    const ms = new Date(2026, 9, 17, 14, 5, 30).getTime()
    expect(formatSnapshotTime(ms)).toBe('2026-10-17 14:05')
  })

  it('任意时刻输出均匹配形态约束（性质断言）', () => {
    for (const ms of [0, Date.UTC(2026, 0, 1, 0, 0, 0), Date.UTC(1999, 11, 31, 23, 59, 59)]) {
      expect(formatSnapshotTime(ms)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
    }
  })
})

describe('fetchRelayDevices — 云端权威注册表换源（R30）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('打 sync_agent 透传端点并透传设备数组', async () => {
    const devices = [
      {
        device_id: 'phone-b',
        tier: 'phone',
        app_version: '1.2.0',
        online: true,
        last_heartbeat_at: '2026-10-19T06:00:00+00:00',
      },
    ]
    vi.mocked(apiClient.get).mockResolvedValueOnce({
      data: { devices, source: 'cloud', degraded: false, fetched_at: '2026-10-19T06:00:01+00:00' },
    })
    const result = await fetchRelayDevices()
    expect(result.devices).toEqual(devices)
    expect(result.degraded).toBe(false)
    expect(apiClient.get).toHaveBeenCalledWith('/ext/sync_agent/relay/devices')
  })

  it('降级响应映射为拒绝（禁空数组假装成功——空=「尚无其他设备」误导文案）', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce({
      data: { devices: [], source: 'cloud', degraded: true, error: '云端注册表暂不可达: relay 503' },
    })
    await expect(fetchRelayDevices()).rejects.toThrow('云端注册表暂不可达')
  })
})
