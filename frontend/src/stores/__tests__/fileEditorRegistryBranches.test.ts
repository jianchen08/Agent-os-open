/**
 * fileEditorRegistry 分支补测：localStorage 还原的防御性解析、体积上限跳过
 * 落盘、监听器订阅/退订/异常隔离，以及更新/移除的命中与未命中分支。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 复位模块缓存，让 registry 重新执行 _loadFromStorage 启动逻辑 */
async function loadFreshRegistry() {
  vi.resetModules()
  return await import('../fileEditorRegistry')
}

const baseData = {
  filePath: 'src/main.py',
  fileName: 'main.py',
  content: 'print(1)',
  containerTaskId: 'task-1',
}

beforeEach(() => {
  localStorage.clear()
})

describe('fileEditorRegistry — localStorage 还原（防御性解析）', () => {
  it('合法持久化数据被还原（字段完整）', async () => {
    localStorage.setItem(
      'file-editor-registry',
      JSON.stringify({ 'tab-a': { ...baseData, size: 8 } }),
    )
    const reg = await loadFreshRegistry()
    expect(reg.getFileEditorData('tab-a')).toMatchObject({ filePath: 'src/main.py', size: 8 })
  })

  it('JSON 损坏时回退空表（不抛异常）', async () => {
    localStorage.setItem('file-editor-registry', '{not json')
    const reg = await loadFreshRegistry()
    expect(reg.getFileEditorData('anything')).toBeUndefined()
  })

  it('缺 filePath 的条目被丢弃，合法条目保留（混合数据）', async () => {
    localStorage.setItem(
      'file-editor-registry',
      JSON.stringify({
        bad: { fileName: 'x', content: 'c', containerTaskId: 't' },
        nullEntry: null,
        good: baseData,
      }),
    )
    const reg = await loadFreshRegistry()
    expect(reg.getFileEditorData('bad')).toBeUndefined()
    expect(reg.getFileEditorData('nullEntry')).toBeUndefined()
    expect(reg.getFileEditorData('good')).toMatchObject({ filePath: 'src/main.py' })
  })

  it('无持久化数据时注册表为空（启动快照性质断言）', async () => {
    const reg = await loadFreshRegistry()
    expect(reg.getFileEditorData('tab-a')).toBeUndefined()
    expect(localStorage.getItem('file-editor-registry')).toBeNull()
  })

  it('localStorage 缺失（无全局 Storage）时还原为空表且读写不抛错', async () => {
    const realWindowLS = Object.getOwnPropertyDescriptor(window, 'localStorage')
    const realGlobalLS = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
    try {
      // 模拟非浏览器环境：typeof localStorage === 'undefined' 两条短路分支
      delete (globalThis as { localStorage?: unknown }).localStorage
      delete (window as unknown as { localStorage?: unknown }).localStorage
      const reg = await loadFreshRegistry()
      reg.registerFileEditor('tab-x', baseData)
      expect(reg.getFileEditorData('tab-x')).toMatchObject(baseData)
      expect(() => reg.removeFileEditorData('tab-x')).not.toThrow()
    } finally {
      if (realWindowLS) Object.defineProperty(window, 'localStorage', realWindowLS)
      if (realGlobalLS) Object.defineProperty(globalThis, 'localStorage', realGlobalLS)
    }
  })

  it('落盘抛错（quota/权限）被静默吞掉，内存数据仍可读', async () => {
    const reg = await loadFreshRegistry()
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('QuotaExceededError')
    })
    try {
      expect(() => reg.registerFileEditor('tab-q', baseData)).not.toThrow()
      expect(reg.getFileEditorData('tab-q')).toMatchObject({ filePath: 'src/main.py' })
    } finally {
      setItem.mockRestore()
    }
  })
})

describe('fileEditorRegistry — 注册/读取/更新/移除', () => {
  it('register → get 往返（含未注册 id 返回 undefined）', async () => {
    const reg = await loadFreshRegistry()
    reg.registerFileEditor('tab-1', baseData)
    expect(reg.getFileEditorData('tab-1')).toMatchObject(baseData)
    expect(reg.getFileEditorData('tab-missing')).toBeUndefined()
  })

  it('updateFileEditorData 合并部分字段并保留其余（命中分支）', async () => {
    const reg = await loadFreshRegistry()
    reg.registerFileEditor('tab-1', baseData)
    reg.updateFileEditorData('tab-1', { content: 'changed', loading: true })
    expect(reg.getFileEditorData('tab-1')).toMatchObject({
      filePath: 'src/main.py',
      content: 'changed',
      loading: true,
    })
    // 持久化剥离运行时 loading
    const raw = JSON.parse(localStorage.getItem('file-editor-registry')!)
    expect(raw['tab-1'].loading).toBeUndefined()
    expect(raw['tab-1'].content).toBe('changed')
  })

  it('updateFileEditorData 对未注册 id 无副作用（未命中分支）', async () => {
    const reg = await loadFreshRegistry()
    reg.registerFileEditor('tab-1', baseData)
    reg.updateFileEditorData('tab-missing', { content: 'x' })
    expect(reg.getFileEditorData('tab-missing')).toBeUndefined()
    const raw = JSON.parse(localStorage.getItem('file-editor-registry')!)
    expect(Object.keys(raw)).toEqual(['tab-1'])
  })

  it('removeFileEditorData 同时清理数据与监听器', async () => {
    const reg = await loadFreshRegistry()
    reg.registerFileEditor('tab-1', baseData)
    const listener = vi.fn()
    reg.subscribeFileChange('tab-1', listener)
    reg.removeFileEditorData('tab-1')
    expect(reg.getFileEditorData('tab-1')).toBeUndefined()
    reg.emitFileChange('tab-1', 'new')
    expect(listener).not.toHaveBeenCalled()
    expect(JSON.parse(localStorage.getItem('file-editor-registry')!)).toEqual({})
  })
})

describe('fileEditorRegistry — 持久化体积上限', () => {
  it('超过 256KB 的文件跳过落盘（其余条目仍写入）', async () => {
    const reg = await loadFreshRegistry()
    const huge = 'x'.repeat(256 * 1024 + 1)
    reg.registerFileEditor('huge', { ...baseData, content: huge })
    reg.registerFileEditor('small', baseData)
    const raw = JSON.parse(localStorage.getItem('file-editor-registry')!)
    expect(raw.huge).toBeUndefined()
    expect(raw.small).toBeDefined()
    // 内存中仍可见（仅持久化被跳过）
    expect(reg.getFileEditorData('huge')!.content.length).toBe(huge.length)
  })

  it('content 缺失（undefined）时按 0 长度处理并正常落盘', async () => {
    const reg = await loadFreshRegistry()
    reg.registerFileEditor('no-content', {
      filePath: 'a.bin',
      fileName: 'a.bin',
      content: undefined as unknown as string,
      containerTaskId: 't',
    })
    const raw = JSON.parse(localStorage.getItem('file-editor-registry')!)
    expect(raw['no-content']).toBeDefined()
    expect(raw['no-content'].filePath).toBe('a.bin')
  })

  it('恰好 256KB 的文件允许落盘（边界：上限为「大于」而非「大于等于」）', async () => {
    const reg = await loadFreshRegistry()
    const exact = 'y'.repeat(256 * 1024)
    reg.registerFileEditor('exact', { ...baseData, content: exact })
    const raw = JSON.parse(localStorage.getItem('file-editor-registry')!)
    expect(raw.exact).toBeDefined()
    expect(raw.exact.content.length).toBe(256 * 1024)
  })
})

describe('fileEditorRegistry — 文件变更监听器', () => {
  it('emit 触发所有订阅者并透传内容与大小', async () => {
    const reg = await loadFreshRegistry()
    const l1 = vi.fn()
    const l2 = vi.fn()
    reg.subscribeFileChange('tab-1', l1)
    reg.subscribeFileChange('tab-1', l2)
    reg.emitFileChange('tab-1', 'fresh', 42)
    expect(l1).toHaveBeenCalledWith('fresh', 42)
    expect(l2).toHaveBeenCalledWith('fresh', 42)
  })

  it('同一监听器重复订阅只触发一次（Set 去重）', async () => {
    const reg = await loadFreshRegistry()
    const listener = vi.fn()
    reg.subscribeFileChange('tab-1', listener)
    reg.subscribeFileChange('tab-1', listener)
    reg.emitFileChange('tab-1', 'once')
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('unsubscribeFileChange 后退订者不再收到通知，其他订阅者不受影响', async () => {
    const reg = await loadFreshRegistry()
    const removed = vi.fn()
    const kept = vi.fn()
    reg.subscribeFileChange('tab-1', removed)
    reg.subscribeFileChange('tab-1', kept)
    reg.unsubscribeFileChange('tab-1', removed)
    reg.emitFileChange('tab-1', 'after')
    expect(removed).not.toHaveBeenCalled()
    expect(kept).toHaveBeenCalledTimes(1)
  })

  it('unsubscribe 未订阅的 tab 不抛错（未命中分支）', async () => {
    const reg = await loadFreshRegistry()
    expect(() => reg.unsubscribeFileChange('nope', () => {})).not.toThrow()
  })

  it('emit 未订阅的 tab 无副作用（未命中分支）', async () => {
    const reg = await loadFreshRegistry()
    expect(() => reg.emitFileChange('nope', 'c')).not.toThrow()
  })

  it('单个监听器抛异常不影响后续监听器（异常隔离）', async () => {
    const reg = await loadFreshRegistry()
    const boom = vi.fn(() => {
      throw new Error('listener failed')
    })
    const after = vi.fn()
    reg.subscribeFileChange('tab-1', boom)
    reg.subscribeFileChange('tab-1', after)
    expect(() => reg.emitFileChange('tab-1', 'payload')).not.toThrow()
    expect(after).toHaveBeenCalledWith('payload', undefined)
  })

  it('不同 tab 的监听器互不串扰（隔离性质）', async () => {
    const reg = await loadFreshRegistry()
    const a = vi.fn()
    const b = vi.fn()
    reg.subscribeFileChange('tab-a', a)
    reg.subscribeFileChange('tab-b', b)
    reg.emitFileChange('tab-a', 'for-a')
    expect(a).toHaveBeenCalledTimes(1)
    expect(b).not.toHaveBeenCalled()
  })
})
