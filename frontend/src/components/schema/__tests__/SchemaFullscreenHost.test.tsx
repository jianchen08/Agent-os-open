/**
 * SchemaFullscreenHost.test.tsx
 *
 * 验证「声明 → 实现」链路在全屏空间的落点：
 * 1. trigger 求值（on_event:xxx → 事件名）与声明收集（space 过滤）
 * 2. 事件到达 → 打开 FullscreenOverlay → 声明 widget 渲染（payload 并入 props）
 * 3. 提交走既有真实通道（choice → sendInteractionResponse / review → sendApproval）
 * 4. 去重 / 关闭 / 队列导航
 */

import { act, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import {
  SchemaFullscreenHost,
  parseEventTrigger,
  collectFullscreenEventDeclarations,
  toSchemaEventItem,
} from '@/components/schema/SchemaFullscreenHost'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'

// 副作用：把真实 widget 组件注册进 WidgetRegistry 单例（review_document 等），
// 让「声明 → 实现」链路在测试里真实解析。
import { initializeWidgets } from '@/services/schema/registerWidgets'

// globalWS：捕获订阅 handler，测试内手动触发；send 方法记调用
const handlers = new Map<string, (raw: Record<string, unknown>) => void>()
const sendInteractionResponseMock = vi.fn()
const sendApprovalMock = vi.fn()

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, handler: (raw: Record<string, unknown>) => void) => {
      handlers.set(event, handler)
    }),
    unsubscribe: vi.fn((event: string) => {
      handlers.delete(event)
    }),
    sendInteractionResponse: (...args: unknown[]) => sendInteractionResponseMock(...args),
    sendApproval: (...args: unknown[]) => sendApprovalMock(...args),
  },
}))

// sessionStore：避免真实 store 依赖（zustand store = hook + getState 静态方法）
const sessionState = { activeSessionId: 'session-1' }
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: Object.assign(() => sessionState, { getState: () => sessionState }),
}))

/** 与 approval/plugin.json ui_schema 对齐的声明 */
const approvalDecl: WidgetDeclaration = {
  id: 'approval_panel',
  type: 'review_document',
  space: 'fullscreen',
  trigger: 'on_event:approval.created',
  props: { diff_view: true, annotation: true },
}

function fireApprovalCreated(payload: Record<string, unknown>) {
  const handler = handlers.get('approval.created')
  if (!handler) throw new Error('approval.created handler 未注册')
  act(() => handler(payload))
}

describe('trigger 求值与声明收集', () => {
  it('parseEventTrigger: on_event:xxx 提取事件名，其余返回 null', () => {
    expect(parseEventTrigger('on_event:approval.created')).toBe('approval.created')
    expect(parseEventTrigger('on_route_signal:wait')).toBeNull()
    expect(parseEventTrigger(undefined)).toBeNull()
  })

  it('collectFullscreenEventDeclarations: 只收 fullscreen + on_event 声明', () => {
    const decls: WidgetDeclaration[] = [
      approvalDecl,
      { id: 'workspace_panel', type: 'table', space: 'workspace', trigger: 'on_event:approval.created' },
      { id: 'no_trigger', type: 'table', space: 'fullscreen' },
    ]
    const collected = collectFullscreenEventDeclarations(decls)
    expect(collected).toHaveLength(1)
    expect(collected[0].eventName).toBe('approval.created')
    expect(collected[0].declaration.id).toBe('approval_panel')
  })

  it('toSchemaEventItem: 兼容扁平与 data 嵌套封装，缺 request_id 返回 null', () => {
    const flat = toSchemaEventItem('approval_panel', 'approval.created', {
      request_id: 'req-1',
      title: '请选择',
      options: ['批准', '拒绝'],
      mode: 'choice',
    })
    expect(flat?.requestId).toBe('req-1')
    expect(flat?.mode).toBe('choice')
    expect(flat?.options).toEqual(['批准', '拒绝'])

    const nested = toSchemaEventItem('approval_panel', 'approval.created', {
      data: { approval_id: 'req-2', mode: 'review' },
    })
    expect(nested?.requestId).toBe('req-2')
    expect(nested?.mode).toBe('review')

    expect(toSchemaEventItem('approval_panel', 'approval.created', { title: '无 id' })).toBeNull()
  })
})

describe('SchemaFullscreenHost 组件', () => {
  beforeAll(() => {
    initializeWidgets()
  })

  beforeEach(() => {
    handlers.clear()
    sendInteractionResponseMock.mockClear()
    sendApprovalMock.mockClear()
  })

  it('事件到达 → 打开全屏浮层并渲染声明 widget（payload 并入 props）', async () => {
    render(<SchemaFullscreenHost declarations={[approvalDecl]} />)

    fireApprovalCreated({
      request_id: 'req-1',
      title: '请审阅版本差异',
      options: ['批准', '拒绝'],
      mode: 'choice',
      artifacts: [
        { id: 'art-1', title: '小说第一章', content: '新版本内容', baselineContent: '旧版本内容' },
      ],
      annotations: [],
    })

    // 全屏浮层出现（FullscreenOverlay 顶栏 + 标题）
    expect(await screen.findByText('请审阅版本差异')).toBeTruthy()
    expect(screen.getByTestId('fullscreen-toolbar')).toBeTruthy()

    // 声明 widget 被渲染（DeclaredWidgetLayer 的 ResolvedItem testid）
    expect(screen.getByTestId('declared-widget-approval_panel')).toBeTruthy()
    // payload 并入 props 后，widget 读到真实制品
    expect(screen.getByText('小说第一章')).toBeTruthy()
  })

  it('choice 模式：点击选项 → sendInteractionResponse（真实 human 通道），提交后浮层关闭', async () => {
    render(<SchemaFullscreenHost declarations={[approvalDecl]} />)

    fireApprovalCreated({
      request_id: 'req-1',
      title: '请选择',
      options: ['批准', '拒绝'],
      mode: 'choice',
      run_id: 'run-42',
    })

    const approveBtn = await screen.findByRole('button', { name: '批准' })
    act(() => approveBtn.click())

    expect(sendInteractionResponseMock).toHaveBeenCalledWith(
      'session-1',
      'req-1',
      expect.objectContaining({ response_type: 'answered', selected_option: '批准' }),
    )
    // 提交后条目移除 → 浮层关闭
    expect(screen.queryByTestId('fullscreen-toolbar')).toBeNull()
  })

  it('review 模式：批准/拒绝 → sendApproval（审批决策通道）', async () => {
    render(<SchemaFullscreenHost declarations={[approvalDecl]} />)

    fireApprovalCreated({
      request_id: 'req-2',
      title: '文档审阅',
      mode: 'review',
      run_id: 'run-7',
    })

    const rejectBtn = await screen.findByRole('button', { name: '拒绝' })
    act(() => rejectBtn.click())

    expect(sendApprovalMock).toHaveBeenCalledWith('session-1', 'rejected', '')
    expect(screen.queryByTestId('fullscreen-toolbar')).toBeNull()
  })

  it('同一 requestId 去重：重复事件只入队一次', async () => {
    render(<SchemaFullscreenHost declarations={[approvalDecl]} />)

    fireApprovalCreated({ request_id: 'req-1', title: '第一次', options: ['a'], mode: 'choice' })
    fireApprovalCreated({ request_id: 'req-1', title: '重复', options: ['a'], mode: 'choice' })

    // 队列仅 1 条：导航计数显示 "1 / 1"
    expect(await screen.findByText('1 / 1')).toBeTruthy()
    expect(screen.getByText('第一次')).toBeTruthy()
  })

  it('无 fullscreen + on_event 声明时，不订阅事件、不弹浮层', () => {
    render(
      <SchemaFullscreenHost
        declarations={[{ id: 'p', type: 'table', space: 'workspace', trigger: 'on_event:approval.created' }]}
      />,
    )
    // 该声明 space=workspace，不满足 fullscreen 收集条件 → 不订阅 approval.created
    expect(handlers.has('approval.created')).toBe(false)
    expect(screen.queryByTestId('fullscreen-toolbar')).toBeNull()
  })
})
