import { describe, expect, it } from 'vitest'
import { assetKeyOf, eventAssetKey, validateEvent } from '../syncEvent'
import { newUlid } from '../ulid'

const VALID_ULID = '01ARZ3NDEKTSV4RRFFQ69G5FAV'

export function validWire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ulid: VALID_ULID,
    device_id: 'desktop-1',
    asset_type: 'config',
    asset_id: 'agents/main/agentos.yaml',
    version: 1,
    updated_at: '2026-10-17T00:00:00.000Z',
    payload: { content: 'key: value\n' },
    hlc: { physical_ms: 1_760_000_000_000, logical: 0, device_id: 'desktop-1' },
    cursor: 3,
    ...overrides,
  }
}

describe('sync_event 校验：五必填缺一即拒（fail-closed，参数化）', () => {
  it.each([
    ['ulid'],
    ['device_id'],
    ['asset_type'],
    ['version'],
    ['updated_at'],
  ])('缺失 %s 即拒', (field) => {
    const wire = validWire()
    delete wire[field]
    expect(() => validateEvent(wire)).toThrow(/缺少必填字段/)
  })

  it.each([
    ['ulid 置 null', { ulid: null }],
    ['device_id 置 undefined（读取后删键同效）', { device_id: undefined }],
  ])('%s 同样按缺失拒绝', (_label, patch) => {
    expect(() => validateEvent(validWire(patch))).toThrow(/缺少必填字段/)
  })
})

describe('sync_event 校验：字段类型违约即拒（参数化）', () => {
  it.each([
    ['ulid 非 26 位', { ulid: 'SHORT' }],
    ['ulid 小写', { ulid: VALID_ULID.toLowerCase() }],
    ['ulid 含非 Crockford 字符', { ulid: VALID_ULID.slice(0, 25) + 'U' }],
    ['device_id 空串', { device_id: '' }],
    ['asset_type 空串', { asset_type: '' }],
    ['version 布尔', { version: true }],
    ['version 负数', { version: -1 }],
    ['version 浮点', { version: 1.5 }],
    ['updated_at 空串', { updated_at: '' }],
    ['asset_id 非字符串', { asset_id: 7 }],
    ['payload 数组', { payload: [] }],
    ['hlc 非对象', { hlc: 'nope' }],
    ['hlc 物理分量负数', { hlc: { physical_ms: -1, logical: 0, device_id: 'a' } }],
    ['cursor 负数', { cursor: -1 }],
    ['cursor 布尔', { cursor: true }],
  ])('%s', (_label, patch) => {
    expect(() => validateEvent(validWire(patch))).toThrow()
  })
})

describe('sync_event 校验：合法事件通过（可选字段缺省语义）', () => {
  it('全量字段通过且视图字段齐备', () => {
    const event = validateEvent(validWire())
    expect(event.ulid).toBe(VALID_ULID)
    expect(event.deviceId).toBe('desktop-1')
    expect(event.assetType).toBe('config')
    expect(event.assetId).toBe('agents/main/agentos.yaml')
    expect(event.version).toBe(1)
    expect(event.hlc?.physicalMs).toBe(1_760_000_000_000)
    expect(event.cursor).toBe(3)
    expect(eventAssetKey(event)).toBe('config/agents/main/agentos.yaml')
  })

  it('无 hlc / 无 asset_id / 无 payload / 无 cursor 仍通过（协议宽容，merge 层执法）', () => {
    const event = validateEvent({
      ulid: newUlid(),
      device_id: 'desktop-1',
      asset_type: 'config',
      version: 0,
      updated_at: '2026-10-17T00:00:00.000Z',
    })
    expect(event.hlc).toBeNull()
    expect(event.assetId).toBe('')
    expect(event.payload).toEqual({})
    expect(event.cursor).toBe(0)
  })

  it('非对象输入即拒', () => {
    expect(() => validateEvent('nope')).toThrow(/必须是对象/)
    expect(() => validateEvent([1, 2])).toThrow(/必须是对象/)
    expect(() => validateEvent(null)).toThrow(/必须是对象/)
  })
})

describe('asset_key 组装', () => {
  it('有 asset_id 拼类型；无 asset_id 仅类型', () => {
    expect(assetKeyOf('config', 'a/b.yaml')).toBe('config/a/b.yaml')
    expect(assetKeyOf('config', '')).toBe('config')
  })
})
