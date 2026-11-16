import { describe, expect, it } from 'vitest'
import { compareHlc, HybridLogicalClock, hlcFromDict, hlcToDict } from '../hlc'

const stamp = (physicalMs: number, logical: number, deviceId: string) => ({
  physicalMs,
  logical,
  deviceId,
})

describe('HLC 全序（(physical, logical, device) 字典序恒可比）', () => {
  it.each([
    ['物理分量决胜', stamp(100, 5, 'a'), stamp(101, 0, 'a'), -1],
    ['逻辑分量决胜', stamp(100, 5, 'a'), stamp(100, 6, 'a'), -1],
    ['device_id 决胜（同物理同逻辑毫秒恒有唯一胜者）', stamp(100, 5, 'cloud'), stamp(100, 5, 'phone-x'), -1],
    ['全等', stamp(100, 5, 'a'), stamp(100, 5, 'a'), 0],
    ['反对称', stamp(101, 0, 'a'), stamp(100, 5, 'a'), 1],
  ])('%s', (_label, a, b, expected) => {
    expect(Math.sign(compareHlc(a, b))).toBe(expected)
  })
})

describe('HLC issue 单调（fake clock 注入断言时序不变量）', () => {
  it('wall 钟停走：物理分量守住，逻辑计数器兜底严格递增', () => {
    const clock = new HybridLogicalClock('phone-x', { clockMs: () => 100 })
    const first = clock.now()
    const second = clock.now()
    const third = clock.now()
    expect(first).toEqual(stamp(100, 0, 'phone-x'))
    expect(second).toEqual(stamp(100, 1, 'phone-x'))
    expect(third).toEqual(stamp(100, 2, 'phone-x'))
  })

  it('wall 钟前跳：物理分量取新值，逻辑归零；回拨：物理守住逻辑续走', () => {
    let now = 100
    const clock = new HybridLogicalClock('phone-x', { clockMs: () => now })
    expect(clock.now()).toEqual(stamp(100, 0, 'phone-x'))
    now = 200
    expect(clock.now()).toEqual(stamp(200, 0, 'phone-x'))
    now = 150
    expect(clock.now()).toEqual(stamp(200, 1, 'phone-x'))
  })

  it('恢复态续接（initial）：新戳严格大于持久化前的任何戳（重启不回退）', () => {
    const clock = new HybridLogicalClock('phone-x', {
      clockMs: () => 50,
      initial: [100, 7],
    })
    const next = clock.now()
    expect(next).toEqual(stamp(100, 8, 'phone-x'))
    expect(compareHlc(next, stamp(100, 7, 'phone-x'))).toBeGreaterThan(0)
  })
})

describe('HLC observe 保因果', () => {
  it.each([
    ['远端物理领先本机与 wall 钟', 10, { physicalMs: 500, logical: 3, deviceId: 'cloud' }, 500],
    ['远端与本机同物理（wall 钟落后）', 100, { physicalMs: 300, logical: 3, deviceId: 'cloud' }, 300],
    ['wall 钟领先远端与本机', 900, { physicalMs: 300, logical: 3, deviceId: 'cloud' }, 900],
  ])('%s：observe 后状态 > 远端且 > 本机此前', (_label, wallNow, remote, expectedPhysical) => {
    const clock = new HybridLogicalClock('phone-x', { clockMs: () => wallNow })
    clock.now()
    const after = clock.observe(remote)
    expect(after.physicalMs).toBe(expectedPhysical)
    expect(compareHlc(after, remote)).toBeGreaterThan(0)
    // 因果保序：observe 之后 issue 的事件 causally-after 该远端事件。
    expect(compareHlc(clock.now(), remote)).toBeGreaterThan(0)
  })

  it('同物理同逻辑双端竞争：双方后续事件互不冲突（各自严格大于远端）', () => {
    const phone = new HybridLogicalClock('phone-x', { clockMs: () => 100 })
    const remote = stamp(100, 4, 'cloud')
    phone.now()
    const after = phone.observe(remote)
    expect(after.logical).toBe(5)
  })
})

describe('hlc 线格式往返', () => {
  it('toDict/fromDict 往返等值', () => {
    const origin = stamp(1234, 5, 'phone-x')
    expect(hlcFromDict(hlcToDict(origin))).toEqual(origin)
  })

  it.each([
    ['非对象', 'nope'],
    ['物理分量负数', { physical_ms: -1, logical: 0, device_id: 'a' }],
    ['物理分量布尔', { physical_ms: true, logical: 0, device_id: 'a' }],
    ['逻辑分量浮点', { physical_ms: 1, logical: 0.5, device_id: 'a' }],
    ['device_id 空', { physical_ms: 1, logical: 0, device_id: '' }],
    ['device_id 缺失', { physical_ms: 1, logical: 0 }],
  ])('违约拒绝：%s', (_label, raw) => {
    expect(() => hlcFromDict(raw)).toThrow()
  })
})
