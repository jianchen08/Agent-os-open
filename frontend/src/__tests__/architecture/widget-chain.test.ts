/**
 * 架构契约测试：§5.3 widget 声明 → 实现链路必须完整
 *
 * 这是统一工程的「架构保险丝」。架构 §5.3 判定 getAllWidgets() 零消费者是
 * 最严重断链；本测试钉死两件事：
 * 1. 链路函数 resolveDeclaredWidgets 正确把声明解析为组件（含降级与未解析兜底）。
 * 2. getAllWidgets 在生产代码中有真实消费者（不允许再次退化为零消费者）。
 *
 * 关联：docs/working/design/frontend-design-unification-execution-plan.md §三 M0.1
 */

import { describe, expect, it, vi } from 'vitest'
import { resolveDeclaredWidgets } from '@/services/schema/widgetChain'
import { scanSourceForPattern } from './harness'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'
import type { WidgetComponent } from '@/services/schema/WidgetRegistry'

/** 生成可区分的 widget 组件桩（id 仅用于调试辨识，不参与逻辑） */
const Stub = (_id: string) => (() => null) as unknown as WidgetComponent

/** 构造可注入的假 registry，隔离测试（不依赖全局单例的真实注册内容） */
function fakeRegistry(known: Record<string, WidgetComponent>) {
  return {
    get: vi.fn((type: string) => known[type]),
    findFallback: vi.fn((type: string) => known[`__fallback_${type}`]),
  }
}

function decl(type: string): WidgetDeclaration {
  return { id: `w-${type}`, type, pluginId: 'test-plugin' }
}

describe('§5.3 链路 — resolveDeclaredWidgets 声明→组件解析', () => {
  it('已直接注册的 type 精确解析，viaFallback=false', () => {
    const registry = fakeRegistry({ chart: Stub('chart') })
    const { resolved, unresolved } = resolveDeclaredWidgets([decl('chart')], registry)

    expect(resolved).toHaveLength(1)
    expect(resolved[0].viaFallback).toBe(false)
    expect(unresolved).toHaveLength(0)
  })

  it('未直接注册但有降级路径的 type 经 findFallback 解析，viaFallback=true', () => {
    const registry = fakeRegistry({ __fallback_kanban: Stub('table') })
    const { resolved, unresolved } = resolveDeclaredWidgets([decl('kanban')], registry)

    expect(resolved).toHaveLength(1)
    expect(resolved[0].viaFallback).toBe(true)
    expect(unresolved).toHaveLength(0)
  })

  it('完全未知的 type 进入 unresolved，原因明确——禁止静默丢弃', () => {
    const registry = fakeRegistry({})
    const { resolved, unresolved } = resolveDeclaredWidgets([decl('mystery')], registry)

    expect(resolved).toHaveLength(0)
    expect(unresolved).toHaveLength(1)
    expect(unresolved[0].reason).toContain('mystery')
  })

  it('混合声明：精确 / 降级 / 未解析三类各得其所', () => {
    const registry = fakeRegistry({
      chart: Stub('chart'),
      __fallback_kanban: Stub('table'),
    })
    const { resolved, unresolved } = resolveDeclaredWidgets(
      [decl('chart'), decl('kanban'), decl('ghost')],
      registry,
    )

    expect(resolved.map((r) => r.declaration.type)).toEqual(['chart', 'kanban'])
    expect(resolved.map((r) => r.viaFallback)).toEqual([false, true])
    expect(unresolved.map((u) => u.declaration.type)).toEqual(['ghost'])
  })

  it('空声明集合返回空结果（不崩溃）', () => {
    const registry = fakeRegistry({})
    const result = resolveDeclaredWidgets([], registry)
    expect(result.resolved).toEqual([])
    expect(result.unresolved).toEqual([])
  })
})

describe('§5.3 链路 — getAllWidgets 必须有生产消费者', () => {
  it('DeclaredWidgetLayer 组件源码引用 getAllWidgets（链路已接通，非零消费者）', () => {
    const hits = scanSourceForPattern('getAllWidgets', [
      'src/components/schema/DeclaredWidgetLayer.tsx',
    ])
    expect(hits.length).toBeGreaterThan(0)
  })

  it('DeclaredWidgetLayer 经 resolveDeclaredWidgets 解析（必须走桥梁，禁止绕过声明表）', () => {
    const hits = scanSourceForPattern('resolveDeclaredWidgets', [
      'src/components/schema/DeclaredWidgetLayer.tsx',
    ])
    expect(hits.length).toBeGreaterThan(0)
  })
})
