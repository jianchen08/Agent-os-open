/** @feature FP-T12 聊天浮层装配契约 | @ci: frontend-test */

/**

 * ChatFloatingLayer · 迟到 schema 响应化契约（真 registry 夹具）。

 *

 * 契约：悬浮槽声明来自 /api/v1/schema 异步装载（main.tsx bootstrap 在组件

 * 挂载之后完成），装载/清空/插件热重载经 registry bumpVersion 通知订阅方。

 * 本层必须订阅变更——声明迟到、resync 重同步、插件热重载后，悬浮件跟随

 * registry 终态即时出现/消失，不依赖宿主任何无关重渲染带过。

 *

 * 断行为（可观察 DOM）：chat-floating-item 在场与否随 registry 装载状态变化。

 */

import { act, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { ChatFloatingLayer } from '../ChatFloatingLayer'



vi.mock('@/components/schema/widgets/WebviewWidget', () => ({

  WebviewWidget: (props: { widgetId: string; title?: string }) => (

    <div data-testid="webview-stub" data-widget-id={props.widgetId} />

  ),

}))



/** 与 todo_track plugin.json contributes.pages[0] 同构的声明（manifest 真值镜像） */

function todoBadgeSchema(): Record<string, unknown> {

  return {

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

                offset: { x: 12, y: 8 },

                transparent: true,

              },

            },

          ],

        },

      },

    ],

  }

}



function badgeInDom(): Element | null {

  return document.querySelector('[data-testid="chat-floating-item"]')

}



describe('ChatFloatingLayer 迟到 schema 响应化', () => {

  beforeEach(() => {

    // 幂等清空：空 schema 装载 = clear() 全清、零注册

    act(() => {

      contributionRegistry.loadFromSchema({ plugin_contributes: [] })

    })

  })



  it('schema 先于挂载在场 → 角标立即可见（对照组）', () => {

    act(() => {

      contributionRegistry.loadFromSchema(todoBadgeSchema())

    })

    render(<ChatFloatingLayer />)

    expect(badgeInDom()).not.toBeNull()

    expect(screen.getByTestId('webview-stub').getAttribute('data-widget-id')).toBe('todo_badge')

  })



  it('挂载后 schema 才到达 → 角标随装载通知即时出现（回归主判据）', () => {

    render(<ChatFloatingLayer />)

    // 首渲染时 registry 尚空：零渲染

    expect(badgeInDom()).toBeNull()

    // 模拟迟到装载（bootstrap 异步完成 / resync 重同步 / 插件热重载）

    act(() => {

      contributionRegistry.loadFromSchema(todoBadgeSchema())

    })

    // 无需任何无关重渲染，角标即出现

    expect(badgeInDom()).not.toBeNull()

  })



  it('已显示后 registry 重载清空 → 角标随之消失（重同步一致性）', () => {

    act(() => {

      contributionRegistry.loadFromSchema(todoBadgeSchema())

    })

    render(<ChatFloatingLayer />)

    expect(badgeInDom()).not.toBeNull()

    act(() => {

      contributionRegistry.loadFromSchema({ plugin_contributes: [] })

    })

    expect(badgeInDom()).toBeNull()

  })

})
