import { describe, expect, it } from 'vitest'
import { ULID_CHARSET, isValidUlid, newUlid } from '../ulid'

const BASE_TS = 1_760_000_000_000

describe('ULID 生成', () => {
  it('形态：26 位大写 Crockford Base32', () => {
    const ulid = newUlid(BASE_TS)
    expect(ulid).toHaveLength(26)
    expect([...ulid].every((ch) => ULID_CHARSET.includes(ch))).toBe(true)
  })

  it('字典序即时间序：后发恒大于先发（≥2 组区分度输入）', () => {
    const a = newUlid(BASE_TS)
    const b = newUlid(BASE_TS + 5)
    const c = newUlid(BASE_TS + 50_000)
    expect(b > a).toBe(true)
    expect(c > b).toBe(true)
  })

  it('同毫秒发号单调递增（随机段 +1）', () => {
    const first = newUlid(BASE_TS)
    const second = newUlid(BASE_TS)
    const third = newUlid(BASE_TS)
    expect(second > first).toBe(true)
    expect(third > second).toBe(true)
  })

  it('时钟回拨按上一毫秒继续发号，字典序不回退（性质断言：全序列单调）', () => {
    const issued: string[] = [
      newUlid(BASE_TS + 100),
      newUlid(BASE_TS - 5_000),
      newUlid(BASE_TS),
      newUlid(BASE_TS + 1),
    ]
    for (let i = 1; i < issued.length; i++) {
      expect(issued[i]! > issued[i - 1]!).toBe(true)
    }
  })
})

describe('isValidUlid（校验违约参数化）', () => {
  it.each([
    ['合法 Crockford', '01ARZ3NDEKTSV4RRFFQ69G5FAV', true],
    ['长度不足', '01ARZ3NDEKTSV4RRFFQ69G5FA', false],
    ['超长', '01ARZ3NDEKTSV4RRFFQ69G5FAVV', false],
    ['含 I（Crockford 排除）', '01ARZ3NDEKTSV4RRFFQ69G5FAI', false],
    ['含 L（Crockford 排除）', '01ARZ3NDEKTSV4RRFFQ69G5FAL', false],
    ['含 O（Crockford 排除）', '01ARZ3NDEKTSV4RRFFQ69G5FAO', false],
    ['含 U（Crockford 排除）', '01ARZ3NDEKTSV4RRFFQ69G5FAU', false],
    ['小写违约', '01arz3ndektsv4rrffq69g5fav', false],
    ['非字符串', 12345, false],
    ['null', null, false],
  ])('%s', (_label, raw, expected) => {
    expect(isValidUlid(raw)).toBe(expected)
  })

  it('生成物恒通过自家校验（性质断言）', () => {
    for (let i = 0; i < 32; i++) {
      expect(isValidUlid(newUlid(BASE_TS + i))).toBe(true)
    }
  })
})
