/** @feature FP-0.2.四/五 fallback-audit FE项 审批声明widget未注册显式占位 @ci frontend-test */
/**
 * 审批视图模式声明注册表测试（widget 化 T10）
 *
 * 覆盖：view_modes 声明装载/覆盖内置映射/未知模式查询。
 * （渲染层路由归 widgetRegistry 注册的 text_diff/image_annotation/media_timeline
 * 复用件；ApprovalRouter 直连路由已随死组件删除。）
 */
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  clearViewModes,
  getViewModeDecl,
  loadViewModes,
  resolveViewModeRoute,
} from '@/utils/viewModeRoutes'

beforeEach(() => {
  clearViewModes()
})
afterEach(() => {
  clearViewModes()
  cleanup()
})

describe('声明注册表', () => {
  it('tools[].ui.view_modes 装载与查询（无效条目丢弃）', () => {
    loadViewModes([
      {
        ui: {
          view_modes: [
            { view_mode: 'text_diff', widget: 'text_diff' },
            { view_mode: 'storyboard', widget: 'my_storyboard' },
            { widget: 'x' }, // 缺 view_mode → 丢弃
            'garbage',
          ],
        },
      },
      {},
    ])
    expect(getViewModeDecl('text_diff')?.widget).toBe('text_diff')
    expect(getViewModeDecl('storyboard')?.widget).toBe('my_storyboard')
    expect(getViewModeDecl('unknown')).toBeUndefined()
  })

  it('resolveViewModeRoute：声明优先，未声明回退内置同构映射，未知 → null', () => {
    loadViewModes([
      { ui: { view_modes: [{ view_mode: 'text_diff', widget: 'custom_diff' }] } },
    ])
    expect(resolveViewModeRoute('text_diff')).toEqual({
      viewMode: 'text_diff',
      widget: 'custom_diff',
      source: 'declared',
    })
    expect(resolveViewModeRoute('image_annotation')).toEqual({
      viewMode: 'image_annotation',
      widget: 'image_annotation',
      source: 'default',
    })
    expect(resolveViewModeRoute('nope')).toBeNull()
  })
})

