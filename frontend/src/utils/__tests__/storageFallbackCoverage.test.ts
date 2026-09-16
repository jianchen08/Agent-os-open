// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 覆盖率收口批：存储容错与本地存储读写分支
 *
 * - tolerantStorage：localStorage 被禁用/配额满时的降级（getItem 返回 null、
 *   setItem 只告警一次、removeItem 静默）——断言可观察读写结果与告警次数，
 *   不断言内部计数变量。
 * - storage（StorageService 单例）：字符串/布尔/JSON 解析路径、预检查无效值、
 *   undefined 特殊处理、localStorage 抛错时不外泄异常。
 * - indexedDbStorage：jsdom 无 IndexedDB → 内存降级读写（节流落盘按真实时钟等待）。
 *
 * 环境注：setup.ts 装的是 MemoryStorage 内存 shim（不继承 Storage.prototype），
 * 故打桩用 spyOn(localStorage, 'setItem') 实例方法，spyOn(Storage.prototype) 无效。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import { storage, uiStorage, STORAGE_KEYS } from '@/utils/storage'
import { loggers } from '@/utils/logger'

describe('createTolerantStorage - localStorage 不可用降级', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('正常可用时透传读写删（写入值可读回）', () => {
    const s = createTolerantStorage()
    s.setItem('tolerant:k1', 'v1')
    expect(s.getItem('tolerant:k1')).toBe('v1')
    s.removeItem('tolerant:k1')
    expect(s.getItem('tolerant:k1')).toBeNull()
  })

  it('getItem 抛错 → 返回 null，不向外抛异常', () => {
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError: storage disabled')
    })

    const s = createTolerantStorage()
    expect(s.getItem('any')).toBeNull()
  })

  it('setItem 配额满 → 不抛异常且同一实例只告警一次', () => {
    const warnSpy = vi.spyOn(loggers.storage, 'warn').mockImplementation(() => {})
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })

    const s = createTolerantStorage()
    expect(() => {
      s.setItem('a', '1')
      s.setItem('b', '2')
      s.setItem('c', '3')
    }).not.toThrow()
    expect(warnSpy).toHaveBeenCalledTimes(1)
  })

  it('removeItem 抛错 → 静默返回（不抛异常、不告警）', () => {
    const warnSpy = vi.spyOn(loggers.storage, 'warn').mockImplementation(() => {})
    vi.spyOn(localStorage, 'removeItem').mockImplementation(() => {
      throw new Error('remove failed')
    })

    const s = createTolerantStorage()
    expect(() => s.removeItem('x')).not.toThrow()
    expect(warnSpy).not.toHaveBeenCalled()
  })
})

describe('storage（StorageService）- 读取解析与写入分支', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  afterEach(() => {
    // 先还原打桩再清存储：否则「clear 抛错」用例装上的桩会在本行清理时炸掉。
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('setItem/getItem 往返：对象经 JSON 序列化保留结构', () => {
    storage.setItem('cov:obj', { a: 1, b: [2, 3] })
    expect(storage.getItem('cov:obj')).toEqual({ a: 1, b: [2, 3] })
  })

  it('数字与布尔经 JSON 往返保持类型', () => {
    storage.setItem('cov:num', 42)
    storage.setItem('cov:bool', false)
    expect(storage.getItem('cov:num')).toBe(42)
    expect(storage.getItem('cov:bool')).toBe(false)
  })

  it('setItem(undefined) → 移除该键（不写 "undefined" 字面量）', () => {
    storage.setItem('cov:undef', 'init')
    storage.setItem('cov:undef', undefined)
    expect(localStorage.getItem('cov:undef')).toBeNull()
  })

  it('裸字符串值（非 JSON）→ JSON.parse 失败后按字面量返回', () => {
    localStorage.setItem('cov:theme', 'dark')
    expect(storage.getItem('cov:theme')).toBe('dark')
  })

  it('裸 "true"/"false" → 解析为布尔值', () => {
    localStorage.setItem('cov:on', 'true')
    localStorage.setItem('cov:off', 'false')
    expect(storage.getItem('cov:on')).toBe(true)
    expect(storage.getItem('cov:off')).toBe(false)
  })

  it('预检查字面量 "undefined"/"null"/"NaN" → 清除该键并返回 null', () => {
    localStorage.setItem('cov:bad1', 'undefined')
    localStorage.setItem('cov:bad2', 'null')
    localStorage.setItem('cov:bad3', 'NaN')

    expect(storage.getItem('cov:bad1')).toBeNull()
    expect(storage.getItem('cov:bad2')).toBeNull()
    expect(storage.getItem('cov:bad3')).toBeNull()
    // 预检查同时移除键，避免残留无效值反复触发
    expect(localStorage.getItem('cov:bad1')).toBeNull()
  })

  it('解析失败且非已知字面量 → 返回 null（不外抛）', () => {
    localStorage.setItem('cov:broken', '{not json')
    expect(storage.getItem('cov:broken')).toBeNull()
  })

  it('键不存在 → 返回 null', () => {
    expect(storage.getItem('cov:missing')).toBeNull()
  })

  it('hasItem 反映键存在性', () => {
    expect(storage.hasItem('cov:has')).toBe(false)
    storage.setItem('cov:has', 'v')
    expect(storage.hasItem('cov:has')).toBe(true)
  })

  it('getAllKeys 含已写入键', () => {
    storage.setItem('cov:keyA', '1')
    storage.setItem('cov:keyB', '2')
    expect(storage.getAllKeys()).toEqual(expect.arrayContaining(['cov:keyA', 'cov:keyB']))
  })

  it('getSize 随写入增长（键+值长度累计）', () => {
    localStorage.clear()
    const before = storage.getSize()
    storage.setItem('cov:size', 'x'.repeat(100))
    expect(storage.getSize()).toBeGreaterThan(before)
  })

  it('removeItem 移除后读回 null', () => {
    storage.setItem('cov:temp', 'v')
    storage.removeItem('cov:temp')
    expect(storage.getItem('cov:temp')).toBeNull()
  })

  it('clear 清空全部键', () => {
    storage.setItem('cov:c1', '1')
    storage.setItem('cov:c2', '2')
    storage.clear()
    expect(storage.getItem('cov:c1')).toBeNull()
    expect(storage.getItem('cov:c2')).toBeNull()
  })

  it('localStorage.getItem 抛错 → 返回 null 不抛异常', () => {
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('access denied')
    })
    expect(storage.getItem('anything')).toBeNull()
  })

  it('localStorage.setItem 抛错 → 不向外抛异常', () => {
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    expect(() => storage.setItem('k', 'v')).not.toThrow()
  })

  it('localStorage.removeItem 抛错 → 不向外抛异常', () => {
    vi.spyOn(localStorage, 'removeItem').mockImplementation(() => {
      throw new Error('remove denied')
    })
    expect(() => storage.removeItem('k')).not.toThrow()
  })

  it('localStorage.clear 抛错 → 不向外抛异常', () => {
    vi.spyOn(localStorage, 'clear').mockImplementation(() => {
      throw new Error('clear denied')
    })
    expect(() => storage.clear()).not.toThrow()
  })
})

describe('uiStorage - 主题与侧栏状态读写', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('setTheme/getTheme 往返', () => {
    uiStorage.setTheme('dark')
    expect(uiStorage.getTheme()).toBe('dark')
    expect(storage.getItem(STORAGE_KEYS.THEME)).toBe('dark')
  })

  it('未设置主题 → getTheme 返回 null', () => {
    expect(uiStorage.getTheme()).toBeNull()
  })

  it('setSidebarCollapsed(true) → 读回布尔 true', () => {
    uiStorage.setSidebarCollapsed(true)
    expect(storage.getItem(STORAGE_KEYS.SIDEBAR_COLLAPSED)).toBe(true)
  })
})

describe('indexedDbStorage - IndexedDB 不可用时的内存降级', () => {
  it('写入后读回同值（jsdom 无 IDB → 降级内存生效）', async () => {
    const { indexedDbStorage } = await import('@/utils/indexedDbStorage')
    // setItem 走 1s 节流窗口，之后才落盘；按真实时钟等待窗口结束。
    indexedDbStorage.setItem('cov-mem', JSON.stringify({ v: 1 }))
    await new Promise((r) => setTimeout(r, 1300))
    await expect(indexedDbStorage.getItem('cov-mem')).resolves.toBe(
      JSON.stringify({ v: 1 }),
    )
  }, 10000)

  it('removeItem 后读回 null', async () => {
    const { indexedDbStorage } = await import('@/utils/indexedDbStorage')
    indexedDbStorage.setItem('cov-rm', JSON.stringify('x'))
    await new Promise((r) => setTimeout(r, 1300))
    indexedDbStorage.removeItem('cov-rm')
    await new Promise((r) => setTimeout(r, 50))
    await expect(indexedDbStorage.getItem('cov-rm')).resolves.toBeNull()
  }, 10000)

  it('节流合并：窗口内多次写只保留最后一次', async () => {
    const { indexedDbStorage } = await import('@/utils/indexedDbStorage')
    indexedDbStorage.setItem('cov-dup', JSON.stringify('first'))
    indexedDbStorage.setItem('cov-dup', JSON.stringify('second'))
    indexedDbStorage.setItem('cov-dup', JSON.stringify('third'))
    await new Promise((r) => setTimeout(r, 1300))

    await expect(indexedDbStorage.getItem('cov-dup')).resolves.toBe(
      JSON.stringify('third'),
    )
  }, 10000)

  it('未写入的键 → null', async () => {
    const { indexedDbStorage } = await import('@/utils/indexedDbStorage')
    await expect(indexedDbStorage.getItem('cov-never')).resolves.toBeNull()
  })
})
