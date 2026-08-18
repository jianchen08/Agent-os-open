/**
 * 受控双向绑定桥测试（widget 化 G4）
 *
 * 覆盖：controlledFieldOf 字段名判定（显式 field / 声明单字段自动取 / 无法判定）；
 * useControlledSlotBridge 返回的 overrideProps 只作用于目标 slotId，注入
 * value/onChange/extra；任意宿主（非 chat-input）都能经钩子控制声明组件。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { controlledFieldOf, useControlledSlotBridge } from '@/hooks/useControlledSlotBridge'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'

const apiGet = vi.fn()
const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiRequest(...args),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))

function decl(fields: Array<{ name: string; label: string }>): WidgetDeclaration {
  return { id: 'ctrl', type: 'form', space: 'workspace', props: { fields } }
}

beforeEach(() => {
  apiGet.mockReset()
  apiRequest.mockReset()
  initializeWidgets()
})

describe('controlledFieldOf', () => {
  it('显式 field 优先', () => {
    expect(controlledFieldOf(decl([{ name: 'a', label: 'A' }]), 'b')).toBe('b')
  })
  it('无显式 field 时取声明单字段名', () => {
    expect(controlledFieldOf(decl([{ name: 'strength', label: '强度' }]))).toBe('strength')
  })
  it('无法判定返回 null（多字段 / 无 fields）', () => {
    expect(controlledFieldOf(decl([{ name: 'a', label: 'A' }, { name: 'b', label: 'B' }]))).toBeNull()
    expect(controlledFieldOf({ id: 'x', type: 'form', space: 'w', props: {} })).toBeNull()
  })
})

describe('useControlledSlotBridge 注入', () => {
  it('注入器契约：只作用于目标 slotId，value/onChange/extra 正确映射，DOM 受控值展示', async () => {
    const set = vi.fn()
    let current = 'medium'
    // 在组件内构造钩子（React 规则），把注入结果交给断言
    let captured: Record<string, unknown> | undefined
    function Host() {
      const overrideProps = useControlledSlotBridge('ctrl', {
        get: () => current,
        set: (_f, v) => {
          current = v as string
          set(v)
        },
        extra: () => ({ disabled: false }),
      })
      // 直接调用注入器派生目标声明上的受控 props（契约级断言，不依赖 Radix 下拉打开）
      const d: WidgetDeclaration = {
        id: 'ctrl',
        type: 'form',
        space: 'workspace',
        props: { fields: [{ name: 'strength', type: 'select', label: '思考强度' }] },
      }
      captured = overrideProps(d)
      return <DeclaredWidgetLayer space="workspace" overrideProps={overrideProps} />
    }
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'p',
          ui_schema: {
            widgets: [
              {
                id: 'ctrl',
                type: 'form',
                space: 'workspace',
                props: {
                  fields: [
                    { name: 'strength', type: 'select', label: '强度', options: [
                      { label: '低', value: 'low' }, { label: '中', value: 'medium' }, { label: '高', value: 'high' },
                    ] },
                  ],
                },
              },
              { id: 'other', type: 'form', space: 'workspace', props: { fields: [{ name: 'x', type: 'input', label: 'X' }] } },
            ],
          },
        },
      ],
      plugin_configs: [],
    })
    render(<Host />)

    // 契约：value={[field]: get}，onChange 写回 set
    expect(captured).toBeDefined()
    expect(captured?.value).toEqual({ strength: 'medium' })
    ;(captured?.onChange as (v: Record<string, unknown>) => void)({ strength: 'high' })
    expect(set).toHaveBeenCalledWith('high')

    // DOM：受控单字段 → compact 形态，按钮反映当前受控值「中」；other 正常渲染
    await waitFor(() => expect(screen.getByRole('button', { name: '中' })).toBeInTheDocument())
    expect(screen.getByLabelText('X')).toBeInTheDocument()
  })
})
