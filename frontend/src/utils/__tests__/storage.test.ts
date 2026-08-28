// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 本地存储工具测试（storage 单例 + uiStorage 便捷封装）
 *
 * 覆盖：JSON 序列化/反序列化、非法值清理（undefined/null/NaN）、
 * 裸字符串/布尔回退解析、键存在性/枚举/大小统计、异常容错，
 * 以及 uiStorage 各主题/侧边栏/会话/面板/思考模式存取。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loggers } from '@/utils/logger'
import { storage, uiStorage } from '@/utils/storage'
import { STORAGE_KEYS } from '@/constants/storage'

describe('storage 单例', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  describe('setItem / getItem - JSON 序列化往返', () => {
    it('对象存取往返', () => {
      storage.setItem('obj', { a: 1, b: [2, 3] })
      expect(storage.getItem('obj')).toEqual({ a: 1, b: [2, 3] })
    })

    it('数字与布尔存取往返', () => {
      storage.setItem('num', 42)
      expect(storage.getItem('num')).toBe(42)
      storage.setItem('bool', true)
      expect(storage.getItem('bool')).toBe(true)
    })

    it('setItem(undefined) 删除键而非写入 "undefined"', () => {
      storage.setItem('k', 'v')
      storage.setItem('k', undefined)
      expect(localStorage.getItem('k')).toBeNull()
      expect(storage.getItem('k')).toBeNull()
    })
  })

  describe('getItem - 非法值清理', () => {
    it.each(['undefined', 'null', 'NaN'])('存储值 %s 时移除并返回 null', (raw) => {
      localStorage.setItem('bad', raw)
      expect(storage.getItem('bad')).toBeNull()
      expect(localStorage.getItem('bad')).toBeNull()
    })

    it('损坏 JSON 时回退解析裸字符串', () => {
      localStorage.setItem('theme', 'dark')
      expect(storage.getItem('theme')).toBe('dark')
    })

    it('损坏 JSON 时回退解析布尔字符串', () => {
      localStorage.setItem('flag', 'true')
      expect(storage.getItem('flag')).toBe(true)
      localStorage.setItem('flag2', 'false')
      expect(storage.getItem('flag2')).toBe(false)
    })

    it('无法解析的损坏 JSON 返回 null 并告警一次（warn-once 契约）', () => {
      localStorage.setItem('broken', '{oops')
      const warnSpy = vi.spyOn(loggers.storage, 'warn').mockImplementation(() => {})
      expect(storage.getItem('broken')).toBeNull()
      expect(warnSpy).toHaveBeenCalledTimes(1)
      // warn-once：同会话第二次失败不再重复告警
      expect(storage.getItem('broken')).toBeNull()
      expect(warnSpy).toHaveBeenCalledTimes(1)
      warnSpy.mockRestore()
    })
  })

  describe('removeItem / clear / hasItem / getAllKeys / getSize', () => {
    it('removeItem 删除指定键', () => {
      storage.setItem('a', 1)
      storage.removeItem('a')
      expect(storage.hasItem('a')).toBe(false)
    })

    it('clear 清空全部', () => {
      storage.setItem('a', 1)
      storage.setItem('b', 2)
      storage.clear()
      expect(localStorage.length).toBe(0)
    })

    it('getAllKeys 返回全部键名', () => {
      storage.setItem('a', 1)
      storage.setItem('b', 2)
      expect(storage.getAllKeys().sort()).toEqual(['a', 'b'])
    })

    it('getSize 统计键值字节数（值经 JSON 序列化）', () => {
      storage.setItem('ab', 'cd')
      // 键 'ab' 2 字节 + 序列化值 '"cd"' 4 字节 = 6
      expect(storage.getSize()).toBe(6)
    })
  })

  describe('异常容错', () => {
    it('setItem 抛异常时记录错误不冒泡（warn-once 标志或已被先前失败消耗，仅断行为）', () => {
      // setup.ts 的 MemoryStorage shim 是普通类实例，需 spy 实例方法而非 Storage.prototype
      const setItemSpy = vi
        .spyOn(localStorage, 'setItem')
        .mockImplementation(() => {
          throw new Error('QuotaExceededError')
        })
      // 会话级 warn-once 标志可能被前面的失败用例先行消耗——严格次数语义
      // 在「损坏 JSON 返回 null 并告警一次」用例覆盖，这里只断不冒泡。
      expect(() => storage.setItem('k', 'v')).not.toThrow()
      setItemSpy.mockRestore()
    })

    it('getItem 抛异常时返回 null 不冒泡（同上，仅断行为）', () => {
      const getItemSpy = vi
        .spyOn(localStorage, 'getItem')
        .mockImplementation(() => {
          throw new Error('SecurityError')
        })
      expect(storage.getItem('k')).toBeNull()
      getItemSpy.mockRestore()
    })
  })
})

describe('uiStorage 便捷封装', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('主题存取往返', () => {
    uiStorage.setTheme('dark')
    expect(uiStorage.getTheme()).toBe('dark')
    expect(localStorage.getItem(STORAGE_KEYS.THEME)).toBe('"dark"')
  })

  it('侧边栏折叠状态存取往返', () => {
    uiStorage.setSidebarCollapsed(true)
    expect(uiStorage.getSidebarCollapsed()).toBe(true)
  })

  describe('侧边栏宽度比例（0~1 校验）', () => {
    it.each([0.3, 0.99])('合法比例 %s 原样返回', (ratio) => {
      uiStorage.setSidebarRatio(ratio)
      expect(uiStorage.getSidebarRatio()).toBe(ratio)
    })

    it.each([0, 1, -0.1, 1.5, NaN, Infinity])('非法比例 %s 返回 null', (ratio) => {
      uiStorage.setSidebarRatio(ratio)
      expect(uiStorage.getSidebarRatio()).toBeNull()
    })

    it('未设置时返回 null', () => {
      expect(uiStorage.getSidebarRatio()).toBeNull()
    })
  })

  it('最后活跃会话存取往返', () => {
    uiStorage.setLastActiveSession('s1')
    expect(uiStorage.getLastActiveSession()).toBe('s1')
  })

  it('任务面板折叠状态存取往返', () => {
    uiStorage.setTaskPanelCollapsed(true)
    expect(uiStorage.getTaskPanelCollapsed()).toBe(true)
  })

  it('工作区面板折叠状态存取往返', () => {
    uiStorage.setWorkspaceCollapsed(false)
    expect(uiStorage.getWorkspaceCollapsed()).toBe(false)
  })

  describe('工作区面板宽度比例（0~1 校验）', () => {
    it('合法比例原样返回', () => {
      uiStorage.setWorkspacePanelRatio(0.5)
      expect(uiStorage.getWorkspacePanelRatio()).toBe(0.5)
    })

    it('非法比例返回 null', () => {
      uiStorage.setWorkspacePanelRatio(1.2)
      expect(uiStorage.getWorkspacePanelRatio()).toBeNull()
    })
  })

  it('思考模式启用状态存取往返', () => {
    uiStorage.setThinkingModeEnabled(true)
    expect(uiStorage.getThinkingModeEnabled()).toBe(true)
  })
})
