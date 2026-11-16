/** @feature FP-T12 聊天浮层 transparent 透传契约 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * ChatFloatingLayer · props.transparent 透传契约测试（2026-10-25 用户实测
 * "圆角钮带方框背景"根修）。
 *
 * 契约：悬浮页声明 props.transparent=true → 原样透传 WebviewWidget（iframe
 * 元素底透明，插件页自带圆角透出宿主）；未声明 → false（canvas 画布底）。
 * 声明面零业务知识：本层只做布尔透传，不解释语义。
 */
import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { ChatFloatingLayer } from '../ChatFloatingLayer'

const webviewProps: Array<Record<string, unknown>> = []
vi.mock('@/components/schema/widgets/WebviewWidget', () => ({
  WebviewWidget: (p: Record<string, unknown>) => {
    webviewProps.push(p)
    return <div data-testid="webview-stub" />
  },
  CSP_META: '',
}))

function seedFloatingPage(transparent: unknown): void {
  contributionRegistry.loadFromSchema({
    plugin_contributes: [
      {
        plugin_id: 'pipeline_todo_track',
        plugin_name: 'todo',
        contributes: {
          pages: [
            {
              id: 'todo_badge',
              title: '待办角标',
              space: 'floating',
              slot: 'panel',
              widget: 'webview',
              props: {
                pluginId: 'pipeline_todo_track',
                htmlPath: '/assets/cards/todo_badge.html',
                widgetId: 'todo_badge',
                anchor: 'top-right',
                ...(transparent !== undefined ? { transparent } : {}),
              },
            },
          ],
        },
      },
    ],
  })
}

describe('ChatFloatingLayer transparent 透传', () => {
  beforeEach(() => {
    webviewProps.length = 0
  })

  it('声明 transparent=true → WebviewWidget 收到 transparent===true', () => {
    seedFloatingPage(true)
    render(<ChatFloatingLayer />)
    expect(webviewProps.at(-1)!.transparent).toBe(true)
  })

  it('未声明 → transparent===false（canvas 画布底缺省）', () => {
    seedFloatingPage(undefined)
    render(<ChatFloatingLayer />)
    expect(webviewProps.at(-1)!.transparent).toBe(false)
  })

  it('声明非布尔真值（如字符串）→ fail-closed 落 false', () => {
    seedFloatingPage('yes')
    render(<ChatFloatingLayer />)
    expect(webviewProps.at(-1)!.transparent).toBe(false)
  })
})
