/**
 * 审批视图模式声明路由测试（widget 化 T10）
 *
 * 覆盖：view_modes 声明装载/覆盖内置映射/声明新 view_mode 路由到已注册
 * widget（前端零改动）/声明但 widget 未注册降级/内置默认/未知模式兜底。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ApprovalRouter } from '../ApprovalRouter'
import {
  clearViewModes,
  getViewModeDecl,
  loadViewModes,
  resolveViewModeRoute,
} from '@/utils/viewModeRoutes'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'

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

describe('ApprovalRouter 声明驱动路由', () => {
  it('声明新 view_mode → 路由到已注册 widget（前端路由器零改动）', () => {
    widgetRegistry.register(
      'storyboard_widget',
      () => <div data-testid="storyboard-view">分镜视图</div>,
      { name: 'storyboard_widget', supportedSpaces: ['workspace'] },
    )
    loadViewModes([
      { ui: { view_modes: [{ view_mode: 'storyboard', widget: 'storyboard_widget' }] } },
    ])
    render(<ApprovalRouter viewMode="storyboard" oldContent="a" newContent="b" />)
    expect(screen.getByTestId('approval-route-storyboard')).toBeInTheDocument()
    expect(screen.getByTestId('storyboard-view')).toBeInTheDocument()
  })

  it('声明覆盖内置：text_diff → 自定义 widget', () => {
    widgetRegistry.register(
      'custom_diff',
      (props: Record<string, unknown>) => <div data-testid="custom-diff">{String(props.oldContent)}</div>,
      { name: 'custom_diff', supportedSpaces: ['workspace'] },
    )
    loadViewModes([
      { ui: { view_modes: [{ view_mode: 'text_diff', widget: 'custom_diff' }] } },
    ])
    render(<ApprovalRouter viewMode="text_diff" oldContent="OLD" newContent="NEW" />)
    expect(screen.getByTestId('custom-diff')).toHaveTextContent('OLD')
  })

  it('声明了但 widget 未注册 → 降级 text_diff 不白屏', () => {
    loadViewModes([
      { ui: { view_modes: [{ view_mode: 'storyboard', widget: 'not_registered' }] } },
    ])
    render(<ApprovalRouter viewMode="storyboard" oldContent="x" newContent="y" />)
    expect(screen.getByTestId('approval-route-text_diff')).toBeInTheDocument()
  })
})

describe('ApprovalRouter 内置默认（声明缺席）', () => {
  it.each(['text_diff', 'image_annotation', 'media_timeline'] as const)(
    '%s 直连内置组件渲染（不依赖 registry）',
    (mode) => {
      render(<ApprovalRouter viewMode={mode} oldContent="a" newContent="b" />)
      expect(screen.getByTestId(`approval-route-${mode}`)).toBeInTheDocument()
    },
  )

  it('未知 view_mode 降级 text_diff', () => {
    render(<ApprovalRouter viewMode="bogus_mode" oldContent="a" newContent="b" />)
    expect(screen.getByTestId('approval-route-text_diff')).toBeInTheDocument()
  })

  it('空 viewMode 降级 text_diff', () => {
    render(<ApprovalRouter viewMode="" oldContent="a" newContent="b" />)
    expect(screen.getByTestId('approval-route-text_diff')).toBeInTheDocument()
  })
})
