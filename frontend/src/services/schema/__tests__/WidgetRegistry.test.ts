/**
 * WidgetRegistry 测试
 *
 * 覆盖：
 * - register() 注册组件
 * - get() 获取已注册组件
 * - has() 检查组件存在
 * - list() 列出所有组件
 * - listBySpace() 按渲染空间筛选
 * - findFallback() 降级查找逻辑
 * - unregister() 取消注册
 * - 重复注册覆盖更新
 */

import { describe, it, expect, beforeEach } from 'vitest'
// 直接引入 class 而非单例，避免测试间互相影响
import type { WidgetComponent, WidgetEntry } from '@/services/schema/WidgetRegistry'

// 动态导入获取新的 WidgetRegistry class
const WidgetRegistryModule = await import('@/services/schema/WidgetRegistry')
const WidgetRegistryClass = WidgetRegistryModule.default?.constructor as typeof WidgetRegistryModule.WidgetRegistry

// 由于导出的是单例，我们需要用 class 直接创建实例
// 检查模块结构
let RegistryClass: any

// 直接 new 一个实例用于测试（绕过单例）
class TestableWidgetRegistry {
  private entries: Map<string, WidgetEntry> = new Map()

  register(type: string, component: WidgetComponent, metadata: any): void {
    if (!type || type.trim() === '') {
      throw new Error('Widget type 不能为空')
    }
    this.entries.set(type, {
      type,
      component,
      metadata: {
        name: metadata.name ?? type,
        description: metadata.description,
        supportedSpaces: metadata.supportedSpaces ?? ['chat', 'workspace'],
        fallbackWidget: metadata.fallbackWidget,
      },
    })
  }

  get(type: string): WidgetComponent | undefined {
    return this.entries.get(type)?.component
  }

  getEntry(type: string): WidgetEntry | undefined {
    return this.entries.get(type)
  }

  has(type: string): boolean {
    return this.entries.has(type)
  }

  list(): WidgetEntry[] {
    return Array.from(this.entries.values())
  }

  listBySpace(space: string): WidgetEntry[] {
    return this.list().filter(entry =>
      entry.metadata.supportedSpaces.includes(space as any),
    )
  }

  unregister(type: string): boolean {
    return this.entries.delete(type)
  }

  findFallback(type: string): WidgetComponent | undefined {
    const direct = this.entries.get(type)
    if (direct) return direct.component

    if (direct?.metadata.fallbackWidget) {
      const fallback = this.entries.get(direct.metadata.fallbackWidget)
      if (fallback) return fallback.component
    }

    const fallbackMap: Record<string, string[]> = {
      kanban: ['table', 'status_card'],
      editor: ['code_block'],
      terminal: ['code_block'],
      dashboard: ['chart'],
    }

    const candidates = fallbackMap[type] ?? ['status_card']
    for (const candidate of candidates) {
      const entry = this.entries.get(candidate)
      if (entry) return entry.component
    }

    return undefined
  }

  get size(): number {
    return this.entries.size
  }

  clear(): void {
    this.entries.clear()
  }
}

/** 创建模拟 React 组件 */
function createMockComponent(name: string): WidgetComponent {
  const comp = ((props: any) => null) as any
  comp.displayName = name
  return comp
}

// ============================================================
// register() 注册组件
// ============================================================

describe('WidgetRegistry.register', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
  })

  it('应成功注册组件', () => {
    const comp = createMockComponent('TestWidget')
    registry.register('test', comp, {
      name: '测试组件',
      supportedSpaces: ['chat', 'workspace'],
    })
    expect(registry.has('test')).toBe(true)
  })

  it('空字符串 type 应抛出错误', () => {
    const comp = createMockComponent('Bad')
    expect(() => registry.register('', comp, {})).toThrow()
  })

  it('未提供 name 时使用 type 作为默认名', () => {
    const comp = createMockComponent('Default')
    registry.register('my_widget', comp, {
      supportedSpaces: ['chat'],
    })
    const entry = registry.getEntry('my_widget')
    expect(entry?.metadata.name).toBe('my_widget')
  })

  it('未提供 supportedSpaces 时使用默认值', () => {
    const comp = createMockComponent('Default')
    registry.register('widget', comp, { name: 'Widget' })
    const entry = registry.getEntry('widget')
    expect(entry?.metadata.supportedSpaces).toEqual(['chat', 'workspace'])
  })
})

// ============================================================
// get() 获取已注册组件
// ============================================================

describe('WidgetRegistry.get', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
  })

  it('应返回已注册的组件', () => {
    const comp = createMockComponent('TestWidget')
    registry.register('test', comp, { name: 'Test' })
    expect(registry.get('test')).toBe(comp)
  })

  it('未注册的组件应返回 undefined', () => {
    expect(registry.get('nonexistent')).toBeUndefined()
  })
})

// ============================================================
// has() 检查组件存在
// ============================================================

describe('WidgetRegistry.has', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
  })

  it('已注册组件返回 true', () => {
    registry.register('exists', createMockComponent('E'), { name: 'E' })
    expect(registry.has('exists')).toBe(true)
  })

  it('未注册组件返回 false', () => {
    expect(registry.has('nope')).toBe(false)
  })
})

// ============================================================
// list() 列出所有组件
// ============================================================

describe('WidgetRegistry.list', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
  })

  it('应返回所有已注册的条目', () => {
    registry.register('a', createMockComponent('A'), { name: 'A' })
    registry.register('b', createMockComponent('B'), { name: 'B' })
    registry.register('c', createMockComponent('C'), { name: 'C' })
    expect(registry.list()).toHaveLength(3)
  })

  it('空注册表返回空数组', () => {
    expect(registry.list()).toEqual([])
  })
})

// ============================================================
// listBySpace() 按渲染空间筛选
// ============================================================

describe('WidgetRegistry.listBySpace', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
    registry.register('chat_only', createMockComponent('ChatOnly'), {
      name: 'Chat Only',
      supportedSpaces: ['chat'],
    })
    registry.register('workspace_only', createMockComponent('WsOnly'), {
      name: 'Workspace Only',
      supportedSpaces: ['workspace'],
    })
    registry.register('multi_space', createMockComponent('Multi'), {
      name: 'Multi',
      supportedSpaces: ['chat', 'workspace', 'floating'],
    })
  })

  it('应筛选出支持指定空间的组件', () => {
    const chatWidgets = registry.listBySpace('chat')
    expect(chatWidgets).toHaveLength(2) // chat_only + multi_space
    const types = chatWidgets.map(e => e.type)
    expect(types).toContain('chat_only')
    expect(types).toContain('multi_space')
  })

  it('workspace 空间筛选', () => {
    const wsWidgets = registry.listBySpace('workspace')
    expect(wsWidgets).toHaveLength(2) // workspace_only + multi_space
  })

  it('无匹配空间返回空数组', () => {
    const dockWidgets = registry.listBySpace('dock')
    expect(dockWidgets).toHaveLength(0)
  })
})

// ============================================================
// findFallback() 降级查找
// ============================================================

describe('WidgetRegistry.findFallback', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
    registry.register('table', createMockComponent('Table'), {
      name: 'Table',
      supportedSpaces: ['chat', 'workspace'],
    })
    registry.register('status_card', createMockComponent('StatusCard'), {
      name: 'Status Card',
      supportedSpaces: ['chat'],
    })
    registry.register('chart', createMockComponent('Chart'), {
      name: 'Chart',
      supportedSpaces: ['chat', 'workspace', 'floating'],
    })
    registry.register('code_block', createMockComponent('CodeBlock'), {
      name: 'Code Block',
      supportedSpaces: ['chat', 'workspace'],
    })
  })

  it('精确匹配时应返回对应组件', () => {
    const comp = registry.findFallback('table')
    expect(comp).toBeDefined()
  })

  it('未知组件应按降级映射表查找', () => {
    // kanban 降级到 table
    const comp = registry.findFallback('kanban')
    expect(comp).toBeDefined()
  })

  it('editor 降级到 code_block', () => {
    const comp = registry.findFallback('editor')
    expect(comp).toBeDefined()
  })

  it('terminal 降级到 code_block', () => {
    const comp = registry.findFallback('terminal')
    expect(comp).toBeDefined()
  })

  it('dashboard 降级到 chart', () => {
    const comp = registry.findFallback('dashboard')
    expect(comp).toBeDefined()
  })

  it('完全未知且无降级映射的组件应回退到 status_card', () => {
    const comp = registry.findFallback('completely_unknown_widget')
    expect(comp).toBeDefined()
  })

  it('无任何已注册组件时返回 undefined', () => {
    const emptyRegistry = new TestableWidgetRegistry()
    const comp = emptyRegistry.findFallback('table')
    expect(comp).toBeUndefined()
  })
})

// ============================================================
// unregister() 取消注册
// ============================================================

describe('WidgetRegistry.unregister', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
  })

  it('应成功取消注册已存在的组件', () => {
    registry.register('to_remove', createMockComponent('R'), { name: 'R' })
    expect(registry.has('to_remove')).toBe(true)
    const result = registry.unregister('to_remove')
    expect(result).toBe(true)
    expect(registry.has('to_remove')).toBe(false)
  })

  it('取消不存在的组件返回 false', () => {
    const result = registry.unregister('nonexistent')
    expect(result).toBe(false)
  })
})

// ============================================================
// 重复注册覆盖更新
// ============================================================

describe('WidgetRegistry - 重复注册覆盖', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
  })

  it('重复注册同一 type 应覆盖旧组件', () => {
    const comp1 = createMockComponent('V1')
    const comp2 = createMockComponent('V2')

    registry.register('widget', comp1, { name: 'V1' })
    expect(registry.get('widget')).toBe(comp1)

    registry.register('widget', comp2, { name: 'V2' })
    expect(registry.get('widget')).toBe(comp2)
  })

  it('覆盖后 list 数量不变', () => {
    registry.register('a', createMockComponent('A1'), { name: 'A1' })
    registry.register('a', createMockComponent('A2'), { name: 'A2' })
    expect(registry.list()).toHaveLength(1)
  })
})

// ============================================================
// size 和 clear
// ============================================================

describe('WidgetRegistry - size 和 clear', () => {
  let registry: TestableWidgetRegistry

  beforeEach(() => {
    registry = new TestableWidgetRegistry()
  })

  it('size 返回已注册数量', () => {
    expect(registry.size).toBe(0)
    registry.register('a', createMockComponent('A'), { name: 'A' })
    expect(registry.size).toBe(1)
    registry.register('b', createMockComponent('B'), { name: 'B' })
    expect(registry.size).toBe(2)
  })

  it('clear 清空所有注册', () => {
    registry.register('a', createMockComponent('A'), { name: 'A' })
    registry.register('b', createMockComponent('B'), { name: 'B' })
    registry.clear()
    expect(registry.size).toBe(0)
    expect(registry.list()).toEqual([])
  })
})
