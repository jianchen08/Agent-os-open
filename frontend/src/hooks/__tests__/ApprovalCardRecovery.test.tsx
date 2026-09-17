/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * ApprovalCardRecovery.test.tsx（BUG-36 回归）
 *
 * 契约：WS 推送（fire-and-forget）丢失时，审批卡不得在整个等待窗口内静默缺席——
 * useInteractionHandler 以有界间隔轮询 /interaction/pending 做拉取补偿，恢复
 * 审批卡到聊天界面（GlobalInteractionOverlay），且补偿链异常必须可见：
 * - 轮询恢复：无任何 WS 推送，仅 get_pending 返回审批记录 → 卡片出现（标题 +
 *   批准/拒绝按钮 + 倒计时字段消费）
 * - 点击批准：发出的响应 selected_option = 选项 label（内核 security_check
 *   按 label→id 映射裁决，approved_once/approved_remember/denied）
 * - 解析丢弃可见：pending 记录形状异常（无 request_id）不得静默跳过
 * - 拉取失败可见：get_pending 请求失败须 warn 留痕，且不中断后续轮询
 *
 * mock 仅限网络/WS/重 UI 边界（apiClient、globalWS、音频、sonner、Markdown、Dialog）。
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { GlobalInteractionOverlay } from '@/components/chat/GlobalInteractionOverlay'
import { INTERACTION_PENDING_POLL_INTERVAL_MS } from '@/hooks/useInteractionHandler'
import { useInteractionStore } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'

Element.prototype.scrollIntoView = () => {}

const pendingResponse = vi.hoisted(() => ({ items: [] as unknown[] }))
const apiGetMock = vi.hoisted(() => vi.fn(async (url: string) => {
  if (String(url).includes('/interaction/pending')) {
    return { data: { items: pendingResponse.items, total: pendingResponse.items.length } }
  }
  return { data: { success: true, content: '' } }
}))

const ws = vi.hoisted(() => {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  return {
    handlers,
    status: 'connected',
    subscribe: vi.fn((topic: string, fn: (data: unknown) => void) => {
      if (!handlers.has(topic)) handlers.set(topic, new Set())
      handlers.get(topic)!.add(fn)
    }),
    unsubscribe: vi.fn((topic: string, fn: (data: unknown) => void) => {
      handlers.get(topic)?.delete(fn)
    }),
    sendInteractionResponse: vi.fn().mockResolvedValue(undefined),
    emit: (topic: string, data: unknown) => {
      handlers.get(topic)?.forEach((fn) => fn(data))
    },
  }
})

vi.mock('@/services/api/client', () => ({ default: { get: apiGetMock } }))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({ globalWS: ws }))
vi.mock('@/utils/audioNotification', () => ({
  playNotificationSound: vi.fn(async () => false),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { error: vi.fn() },
}))
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="markdown-renderer">{content}</div>
  ),
}))
vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}))

/**
 * 后端 /interaction/pending 返回的审批记录（嵌套形状：业务字段在 message_data，
 * 与 human 服务 _make_request_record 同构）。options 为 security_check 发起的
 * 三选项审批（id+label 对象数组）。
 */
function approvalRecord(overrides: {
  id?: string
  session_id?: string
  title?: string
  threadId?: string
}) {
  return {
    id: overrides.id ?? 'req-36-a',
    session_id: overrides.session_id ?? 'sess-36-a',
    type: 'interaction_request',
    status: 'pending',
    // 倒计时起点 = 1 分钟前（相对测试当前时钟）：保证有剩余时间可显示，
    // 断言不锚字面剩余值（时钟无关的性质断言）
    created_at: new Date(Date.now() - 60_000).toISOString(),
    message_data: {
      interaction_mode: 'choice',
      title: overrides.title ?? '安全审批: project_state',
      description: 'action=transition, target_state=plan',
      thread_id: overrides.threadId ?? overrides.session_id ?? 'sess-36-a',
      tab_id: '',
      options: [
        { id: 'approved_once', label: '仅本次执行' },
        { id: 'approved_remember', label: '本管道内同命令免批' },
        { id: 'denied', label: '拒绝执行' },
      ],
      timeout_seconds: 600,
      priority: 'high',
    },
  }
}

async function mountOverlay() {
  const view = render(
    <MemoryRouter>
      <GlobalInteractionOverlay />
    </MemoryRouter>,
  )
  // 挂载即拉取一次（此时 pendingResponse 为空）+ 订阅就位
  await act(async () => {})
  return view
}

/** 推进 N 个轮询周期（fake timers：interval + 微任务一并冲刷） */
async function advancePolls(cycles: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(INTERACTION_PENDING_POLL_INTERVAL_MS * cycles)
  })
}

describe('BUG-36 审批卡拉取补偿恢复（WS 推送丢失场景）', () => {
  let warnSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    ws.handlers.clear()
    ws.status = 'connected'
    pendingResponse.items = []
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    useInteractionStore.setState(useInteractionStore.getInitialState(), true)
    useNotificationStore.setState(useNotificationStore.getInitialState(), true)
    usePipelineMessageStore.setState(usePipelineMessageStore.getInitialState(), true)
  })

  afterEach(() => {
    vi.useRealTimers()
    warnSpy.mockRestore()
  })

  it('零 WS 推送：轮询一个周期后审批卡出现在聊天界面（标题+三选项按钮+倒计时）', async () => {
    pendingResponse.items = [approvalRecord({})]
    await mountOverlay()

    // 挂载时已拉到记录：卡片立即可见（恢复链既有行为）
    expect(screen.getByText('安全审批: project_state')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '仅本次执行' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '本管道内同命令免批' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '拒绝执行' })).toBeInTheDocument()
    // 倒计时字段被消费：timeout_seconds=600 且 created_at 为近期 → 显示剩余 mm:ss
    // （性质断言：格式合规而非字面值——时钟漂移不影响契约）
    expect(screen.getByTestId('approval-countdown')).toHaveTextContent(/^剩余 \d{1,2}:\d{2}$/)

    // 性质断言：继续轮询不产生重复卡片（ingest 去重幂等）
    await advancePolls(3)
    expect(screen.getAllByText('安全审批: project_state')).toHaveLength(1)
    expect(useInteractionStore.getState().pendingInteractions).toHaveLength(1)
  })

  it('推送错过+晚到记录：挂载后记录才出现，轮询周期内补挂卡片（核心补偿契约）', async () => {
    await mountOverlay()
    expect(screen.queryByText('安全审批: project_state')).toBeNull()

    // WS 推送从未到达：记录稍后才可从 get_pending 拉到（第二形状：file_write 审批）
    pendingResponse.items = [
      approvalRecord({ id: 'req-36-b', session_id: 'sess-36-b', title: '安全审批: file_write' }),
    ]
    await advancePolls(1)

    expect(screen.getByText('安全审批: file_write')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '仅本次执行' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '拒绝执行' })).toBeInTheDocument()
  })

  it('点击「仅本次执行」：响应经 WS 发往交互自身会话，selected_option=稳定 id approved_once（内核 security_check 直接裁决合法值）', async () => {
    pendingResponse.items = [approvalRecord({})]
    await mountOverlay()

    fireEvent.click(screen.getByRole('button', { name: '仅本次执行' }))
    await act(async () => {})

    expect(ws.sendInteractionResponse).toHaveBeenCalledTimes(1)
    expect(ws.sendInteractionResponse).toHaveBeenCalledWith(
      'sess-36-a',
      'req-36-a',
      expect.objectContaining({
        response_type: 'answered',
        selected_option: 'approved_once',
      }),
    )
    // 点击后卡片进入 responded 终局（不再可重复提交）
    expect(useInteractionStore.getState().pendingInteractions[0].status).toBe('responded')
  })

  it('pending 记录形状异常（无 request_id）：跳过恢复但必须 warn 留痕，不得静默丢弃', async () => {
    pendingResponse.items = [
      { session_id: 'sess-bad', message_data: { interaction_mode: 'choice', title: '坏形状' } },
    ]
    await mountOverlay()

    expect(screen.queryByText('坏形状')).toBeNull()
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('解析失败'),
      expect.objectContaining({ session_id: 'sess-bad' }),
    )
  })

  it('get_pending 拉取失败：warn 留痕且后续周期继续尝试（失败不终止补偿）', async () => {
    await mountOverlay()
    warnSpy.mockClear()

    apiGetMock.mockRejectedValueOnce(new Error('网络瞬断'))
    await advancePolls(1)
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('恢复待处理交互失败'),
      expect.anything(),
    )

    // 失败后的下一个周期恢复拉取并补挂卡片
    pendingResponse.items = [approvalRecord({ id: 'req-36-c', title: '安全审批: bash_execute' })]
    await advancePolls(1)
    expect(screen.getByText('安全审批: bash_execute')).toBeInTheDocument()
  })

  it('轮询间隔常量为有界值（10s：既不风暴也不让用户干等一个超时窗口）', () => {
    expect(INTERACTION_PENDING_POLL_INTERVAL_MS).toBeGreaterThanOrEqual(5_000)
    expect(INTERACTION_PENDING_POLL_INTERVAL_MS).toBeLessThanOrEqual(30_000)
  })
})

describe('BUG-36 对照：WS 推送在途时轮询去重不重挂', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    ws.handlers.clear()
    pendingResponse.items = []
    useInteractionStore.setState(useInteractionStore.getInitialState(), true)
    useNotificationStore.setState(useNotificationStore.getInitialState(), true)
    usePipelineMessageStore.setState(usePipelineMessageStore.getInitialState(), true)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('推送已入库 + 轮询拉到同 id 记录 → 单卡片（双入口去重）', async () => {
    const record = approvalRecord({})
    const view = render(
      <MemoryRouter>
        <GlobalInteractionOverlay />
      </MemoryRouter>,
    )
    await act(async () => {})

    ws.emit('interaction_request', {
      request_id: record.id,
      session_id: record.session_id,
      thread_id: record.message_data.thread_id,
      interaction_mode: 'choice',
      title: record.message_data.title,
      options: record.message_data.options,
      timeout_seconds: 600,
      created_at: record.created_at,
    })
    await act(async () => {})

    pendingResponse.items = [record]
    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERACTION_PENDING_POLL_INTERVAL_MS)
    })

    expect(screen.getAllByText('安全审批: project_state')).toHaveLength(1)
    expect(useInteractionStore.getState().pendingInteractions).toHaveLength(1)
    view.unmount()
  })
})
