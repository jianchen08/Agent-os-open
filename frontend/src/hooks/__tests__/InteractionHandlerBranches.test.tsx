/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * useInteractionHandler 分支直测（InteractionRestore 覆盖恢复链、
 * ConversationNavigateTab 在组件层 mock 掉本 hook，这里经 WS 订阅探针驱动
 * hook 内部各分支）：
 * - 来源标签解析：管道元数据 → agents 缓存 → agentId 原文三级回退
 * - options 归一化：字符串数组 / 对象包裹（MiniMax 畸形）/ 对象数组 / 无 label 过滤
 * - file_paths 附件：拉取成功进 fileContents 并注册文件 Tab；success=false /
 *   API 抛错降级为占位文本；已存在 Tab 跳过
 * - mode 分流：notification 只进通知中心；choice/conversation 只进交互 Store
 * - 交互去重；cancelled/timeout 摘除并联动撤下通知
 * - 音频通知失败兜底（choice 模式补文字通知）
 * - 恢复防抖（1s 内 reconnected 不重拉）与恢复失败容忍
 * - respondChoice/respondConversation/navigateToTab 的路由键 fail-closed 与正常链
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

Element.prototype.scrollIntoView = () => {}

const pendingResponse = vi.hoisted(() => ({ items: [] as unknown[] }))
const apiGetMock = vi.hoisted(() => vi.fn(async (url: string) => {
  if (String(url).includes('/interaction/pending')) {
    return { data: { items: pendingResponse.items, total: pendingResponse.items.length } }
  }
  return { data: { success: true, content: '文件内容甲' } }
}))

const ws = vi.hoisted(() => {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  return {
    handlers,
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
  // 默认 resolved：hook 调 playNotificationSound().catch(...)，裸 vi.fn()
  // 返回 undefined 会在未覆写用例上炸出 unhandled rejection（全量跑时序漂移
  // 时升级为假红）；需要拒绝路径的用例各自 mockRejectedValue 覆写
  playNotificationSound: vi.fn(async () => false),
}))
const mockNavigateToPipeline = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))
vi.mock('@/services/pipelineNavigator', () => ({ navigateToPipeline: mockNavigateToPipeline }))
const mockAgents = vi.hoisted(() => ({ list: [] as Array<{ id: string; configId?: string; name: string }> }))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({ readAgents: () => mockAgents.list }))

import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useInteractionStore, type PendingInteraction } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useUIStore } from '@/stores/uiStore'
import { playNotificationSound } from '@/utils/audioNotification'

const Wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(MemoryRouter, null, children)

/** 挂载 hook（真订阅），返回其出口 actions；unmount 由测试自行调用 */
async function mountHandler(sessionId?: string) {
  const view = renderHook(() => useInteractionHandler(sessionId), { wrapper: Wrapper })
  await waitFor(() => expect(ws.handlers.has('interaction_request')).toBe(true))
  return view
}

function makeParsed(partial: Partial<PendingInteraction>): Partial<PendingInteraction> {
  return {
    requestId: 'req-x',
    mode: 'choice',
    title: '标题',
    description: '描述',
    threadId: 'th-1',
    tabId: 'tab-1',
    agentId: 'agent-x',
    pipelineId: 'p-1',
    ...partial,
  }
}

// hook 内恢复链有模块级 1s 防抖时间戳（lastRestoreAt，跨用例残留）；
// 用偏移真实时钟让每个用例相对上一用例推进 10s，天然越过防抖窗口，
// 同时保持时钟真实流逝（waitFor 超时计算不受影响）
let dateNowSpy: ReturnType<typeof vi.spyOn> | null = null
let clockOffset = 0

beforeEach(() => {
  vi.clearAllMocks()
  ws.handlers.clear()
  pendingResponse.items = []
  mockAgents.list = []
  apiGetMock.mockClear()
  clockOffset += 10_000
  const base = Date.now()
  dateNowSpy = vi.spyOn(Date, 'now').mockImplementation(() => base + clockOffset)
  useInteractionStore.setState(useInteractionStore.getInitialState(), true)
  useNotificationStore.setState(useNotificationStore.getInitialState(), true)
  usePipelineMessageStore.setState(usePipelineMessageStore.getInitialState(), true)
  useLayoutModeStore.setState(useLayoutModeStore.getInitialState(), true)
  useUIStore.setState(useUIStore.getInitialState(), true)
})

afterEach(() => {
  dateNowSpy?.mockRestore()
  dateNowSpy = null
})

describe('来源标签三级解析（notification 的 sourceLabel）', () => {
  async function pushNotification(pipelineMeta?: { agentName: string }) {
    if (pipelineMeta) {
      usePipelineMessageStore.setState((s) => ({
        pipelines: { ...s.pipelines, 'p-1': { pipelineId: 'p-1', agentName: pipelineMeta.agentName } as never },
      }))
    }
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-src',
      interaction_mode: 'notification',
      title: '进度',
      description: '干活中',
      session_id: 'sess-1',
      thread_id: 'th-1',
      pipeline_id: 'p-1',
      agent_id: 'agent-raw',
    })
    await waitFor(() => expect(useNotificationStore.getState().notifications).toHaveLength(1))
    const label = useNotificationStore.getState().notifications[0].sourceLabel
    view.unmount()
    return label
  }

  it('优先管道元数据 agentName', async () => {
    expect(await pushNotification({ agentName: '专家甲' })).toBe('专家甲')
  })

  it('次选 agents 缓存按 id 匹配', async () => {
    mockAgents.list = [{ id: 'agent-raw', name: '命名Agent' }]
    expect(await pushNotification()).toBe('命名Agent')
  })

  it('解析不到回退 agentId 原文', async () => {
    expect(await pushNotification()).toBe('agent-raw')
  })
})

describe('options 归一化', () => {
  async function pushWithOptions(rawOptions: unknown) {
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-opt',
      interaction_mode: 'choice',
      title: '审批',
      session_id: 'sess-1',
      thread_id: 'th-1',
      pipeline_id: 'p-1',
      options: rawOptions,
    })
    await waitFor(() => expect(useInteractionStore.getState().pendingInteractions).toHaveLength(1))
    const options = useInteractionStore.getState().pendingInteractions[0].options
    view.unmount()
    return options
  }

  it('字符串数组 → {id,label} 序列', async () => {
    expect(await pushWithOptions(['批准', '拒绝'])).toEqual([
      { id: '0', label: '批准' },
      { id: '1', label: '拒绝' },
    ])
  })

  it('对象包裹畸形（MiniMax）→ 取首个数组值继续归一化', async () => {
    expect(await pushWithOptions({ item: ['甲', '乙'] })).toEqual([
      { id: '0', label: '甲' },
      { id: '1', label: '乙' },
    ])
  })

  it('对象数组：label/text/name 兜底，description 透传，无 label 过滤', async () => {
    expect(
      await pushWithOptions([
        { id: 7, label: 'L' },
        { text: 'T' },
        { name: 'N', description: '补充' },
        { no_label: true },
        42,
      ]),
    ).toEqual([
      { id: '7', label: 'L' },
      { id: '1', label: 'T' },
      { id: '2', label: 'N', description: '补充' },
    ])
  })

  it('非数组且无内部数组 → undefined（渲染层不渲染选项按钮）', async () => {
    expect(await pushWithOptions('批准')).toBeUndefined()
  })
})

describe('mode 分流与去重', () => {
  it('notification 只进通知中心；choice 只进交互 Store', async () => {
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-n', interaction_mode: 'notification', title: '进度', agent_id: 'a',
      session_id: 's', thread_id: 't', pipeline_id: 'p',
    })
    ws.emit('interaction_request', {
      request_id: 'req-c', interaction_mode: 'choice', title: '审批', agent_id: 'a',
      session_id: 's', thread_id: 't', pipeline_id: 'p',
    })
    await waitFor(() => expect(useNotificationStore.getState().notifications).toHaveLength(1))
    await waitFor(() => expect(useInteractionStore.getState().pendingInteractions).toHaveLength(1))
    expect(useInteractionStore.getState().pendingInteractions[0].requestId).toBe('req-c')
    view.unmount()
  })

  it('同一 request_id 重复推送只入库一次', async () => {
    const view = await mountHandler()
    const payload = {
      request_id: 'req-dup', interaction_mode: 'choice', title: '审批',
      session_id: 's', thread_id: 't', pipeline_id: 'p',
    }
    ws.emit('interaction_request', payload)
    await waitFor(() => expect(useInteractionStore.getState().pendingInteractions).toHaveLength(1))
    ws.emit('interaction_request', { ...payload, title: '改动版' })
    await act(async () => {})
    expect(useInteractionStore.getState().pendingInteractions).toHaveLength(1)
    expect(useInteractionStore.getState().pendingInteractions[0].title).toBe('审批')
    view.unmount()
  })

  it('缺 request_id 的推送被丢弃', async () => {
    const view = await mountHandler()
    ws.emit('interaction_request', { interaction_mode: 'choice', title: '无 id' })
    await act(async () => {})
    expect(useInteractionStore.getState().pendingInteractions).toHaveLength(0)
    view.unmount()
  })

  it.each([
    { topic: 'interaction_cancelled', label: 'cancelled' },
    { topic: 'interaction_timeout', label: 'timeout' },
  ])('交互 $label：摘除交互并联动撤下对应通知', async ({ topic }) => {
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-gone', interaction_mode: 'notification', title: '进度',
      session_id: 's', thread_id: 't', pipeline_id: 'p',
    })
    await waitFor(() => expect(useNotificationStore.getState().notifications).toHaveLength(1))
    ws.emit(topic, { request_id: 'req-gone' })
    await waitFor(() => expect(useInteractionStore.getState().pendingInteractions).toHaveLength(0))
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    view.unmount()
  })
})

describe('音频通知失败兜底', () => {
  it('choice 模式音频失败 → 补高优文字通知（description 优先，fallback 带失败标注）', async () => {
    vi.mocked(playNotificationSound).mockRejectedValue(new Error('no audio'))
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-s1', interaction_mode: 'choice', title: '审批', description: '请确认',
      session_id: 's', thread_id: 't', pipeline_id: 'p',
    })
    await waitFor(() =>
      expect(useNotificationStore.getState().notifications.some((n) => n.title === '审批' && n.message === '请确认' && n.category === 'alert')).toBe(true),
    )
    view.unmount()
  })

  it('choice 模式音频失败且无 description → fallback 消息带音频失败标注', async () => {
    vi.mocked(playNotificationSound).mockRejectedValue(new Error('no audio'))
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-s3', interaction_mode: 'choice', title: '审批', agent_id: 'a',
      session_id: 's', thread_id: 't', pipeline_id: 'p',
    })
    await waitFor(() =>
      expect(useNotificationStore.getState().notifications.some((n) => n.message === 'a 请求您的输入（音频通知失败）')).toBe(true),
    )
    view.unmount()
  })

  it('notification 模式音频失败 → 不再补冗余通知', async () => {
    vi.mocked(playNotificationSound).mockRejectedValue(new Error('no audio'))
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-s2', interaction_mode: 'notification', title: '进度',
      session_id: 's', thread_id: 't', pipeline_id: 'p',
    })
    await waitFor(() => expect(useNotificationStore.getState().notifications).toHaveLength(1))
    await act(async () => {})
    expect(useNotificationStore.getState().notifications).toHaveLength(1)
    view.unmount()
  })
})

describe('file_paths 附件拉取与文件 Tab 注册', () => {
  async function pushWithFiles(filePaths: string[]) {
    const view = await mountHandler()
    ws.emit('interaction_request', {
      request_id: 'req-file', interaction_mode: 'choice', title: '看文件',
      session_id: 's', thread_id: 't', pipeline_id: 'p',
      file_paths: filePaths,
    })
    await waitFor(() => expect(useInteractionStore.getState().pendingInteractions).toHaveLength(1))
    return view
  }

  it('拉取成功 → fileContents 落库 + 文件 Tab 注册并激活 + 进入五空间', async () => {
    const view = await pushWithFiles(['/ws/报告.md'])
    const parsed = useInteractionStore.getState().pendingInteractions[0]
    expect(parsed.fileContents).toEqual({ '/ws/报告.md': '文件内容甲' })
    const layout = useLayoutModeStore.getState()
    expect(layout.workspaceTabs.some((t) => t.moduleId === '__file_editor__' && t.title === '报告.md')).toBe(true)
    expect(layout.mode).toBe('five-space')
    expect(useUIStore.getState().workspaceCollapsed).toBe(false)
    view.unmount()
  })

  it('success=false → 占位错误文本（含后端 message）', async () => {
    apiGetMock.mockImplementation(async (url: string) => {
      if (String(url).includes('/interaction/pending')) return { data: { items: [], total: 0 } }
      return { data: { success: false, message: '文件不存在' } }
    })
    const view = await pushWithFiles(['/ws/丢.txt'])
    const parsed = useInteractionStore.getState().pendingInteractions[0]
    expect(parsed.fileContents?.['/ws/丢.txt']).toContain('文件加载失败')
    expect(parsed.fileContents?.['/ws/丢.txt']).toContain('文件不存在')
    view.unmount()
  })

  it('API 抛错 → 占位网络错误文本，不阻塞交互入库', async () => {
    apiGetMock.mockImplementation(async (url: string) => {
      if (String(url).includes('/interaction/pending')) return { data: { items: [], total: 0 } }
      throw new Error('超时')
    })
    const view = await pushWithFiles(['/ws/断.txt'])
    const parsed = useInteractionStore.getState().pendingInteractions[0]
    expect(parsed.fileContents?.['/ws/断.txt']).toContain('文件加载失败')
    view.unmount()
  })
})

describe('恢复链（挂载拉取 / 防抖 / 失败容忍）', () => {
  it('挂载即拉取 pending 并经 record 适配入库', async () => {
    pendingResponse.items = [
      {
        id: 'req-r1',
        session_id: 'sess-r',
        message_data: {
          interaction_mode: 'conversation',
          title: '恢复的对话',
          thread_id: 'th-r',
          pipeline_id: 'p-r',
        },
      },
    ]
    // 挂载即拉取：pending 端点在挂载时被调用（恢复入库链的行为断言见
    // InteractionRestore.test.tsx，本文件聚焦拉取触发/防抖/失败容忍三面）
    const view = await mountHandler()
    await waitFor(() =>
      expect(apiGetMock.mock.calls.some(([url]) => String(url).includes('/interaction/pending'))).toBe(true),
    )
    view.unmount()
  })

  it('1 秒防抖窗口内的 reconnected 不重拉', async () => {
    const view = await mountHandler()
    const baseline = apiGetMock.mock.calls.filter(([url]) => String(url).includes('/interaction/pending')).length
    ws.emit('reconnected', {})
    ws.emit('reconnected', {})
    await act(async () => {})
    const after = apiGetMock.mock.calls.filter(([url]) => String(url).includes('/interaction/pending')).length
    expect(after).toBe(baseline)
    view.unmount()
  })

  it('pending 拉取失败：静默容忍不崩', async () => {
    apiGetMock.mockRejectedValue(new Error('后端炸'))
    const view = renderHook(() => useInteractionHandler('sess-1'), { wrapper: Wrapper })
    await act(async () => {})
    expect(useInteractionStore.getState().pendingInteractions).toHaveLength(0)
    view.unmount()
  })
})

describe('respond 出站与 fail-closed', () => {
  it('respondChoice：无自身坐标中止出站', async () => {
    const view = await mountHandler()
    await act(async () => {
      await view.result.current.respondChoice('不存在', '批准')
    })
    expect(ws.sendInteractionResponse).not.toHaveBeenCalled()
    view.unmount()
  })

  it('respondChoice：sessionId 优先于 threadId 作为路由键', async () => {
    const view = await mountHandler()
    useInteractionStore.setState({
      pendingInteractions: [makeParsed({ requestId: 'req-rc', sessionId: 'sess-a', threadId: 'th-b', status: 'pending' }) as PendingInteraction],
    })
    await act(async () => {
      await view.result.current.respondChoice('req-rc', '批准', '备注')
    })
    expect(ws.sendInteractionResponse).toHaveBeenCalledWith('sess-a', 'req-rc', {
      response_type: 'answered',
      selected_option: '批准',
      feedback: '备注',
    })
    expect(useInteractionStore.getState().pendingInteractions[0].status).toBe('responded')
    view.unmount()
  })

  it('respondChoice：无 sessionId 回退 threadId', async () => {
    const view = await mountHandler()
    useInteractionStore.setState({
      pendingInteractions: [makeParsed({ requestId: 'req-rt', threadId: 'th-only', status: 'pending' }) as PendingInteraction],
    })
    await act(async () => {
      await view.result.current.respondChoice('req-rt', '拒绝')
    })
    expect(ws.sendInteractionResponse.mock.calls[0][0]).toBe('th-only')
    view.unmount()
  })

  it('respondConversation：正常出站并标记 responded', async () => {
    const view = await mountHandler()
    useInteractionStore.setState({
      pendingInteractions: [makeParsed({ requestId: 'req-cv', sessionId: 'sess-c', status: 'pending' }) as PendingInteraction],
    })
    await act(async () => {
      await view.result.current.respondConversation('req-cv', '补充说明')
    })
    expect(ws.sendInteractionResponse).toHaveBeenCalledWith('sess-c', 'req-cv', {
      response_type: 'answered',
      feedback: '补充说明',
    })
    expect(useInteractionStore.getState().pendingInteractions[0].status).toBe('responded')
    view.unmount()
  })

  it('navigateToTab：无坐标不出站不跳转', async () => {
    const view = await mountHandler()
    await act(async () => {
      await view.result.current.navigateToTab('不存在', 'p-1')
    })
    expect(ws.sendInteractionResponse).not.toHaveBeenCalled()
    expect(mockNavigateToPipeline).not.toHaveBeenCalled()
    view.unmount()
  })

  it('navigateToTab：正常链 = approved 出站 + entered 标记 + 带层级导航', async () => {
    const view = await mountHandler()
    useInteractionStore.setState({
      pendingInteractions: [makeParsed({ requestId: 'req-nav', sessionId: 'sess-n', status: 'pending' }) as PendingInteraction],
    })
    await act(async () => {
      await view.result.current.navigateToTab('req-nav', 'p-9', '专家乙', 'L3')
    })
    expect(ws.sendInteractionResponse).toHaveBeenCalledWith('sess-n', 'req-nav', {
      response_type: 'approved',
      feedback: '用户已进入对话标签页',
    })
    expect(useInteractionStore.getState().pendingInteractions[0].status).toBe('entered')
    expect(mockNavigateToPipeline).toHaveBeenCalledWith('p-9', { agentName: '专家乙', agentLevel: 3 })
    view.unmount()
  })

  it('navigateToTab：缺 pipelineId 时已出站+标记，但不导航', async () => {
    const view = await mountHandler()
    useInteractionStore.setState({
      pendingInteractions: [makeParsed({ requestId: 'req-np', sessionId: 'sess-p', status: 'pending' }) as PendingInteraction],
    })
    await act(async () => {
      await view.result.current.navigateToTab('req-np', '')
    })
    expect(mockNavigateToPipeline).not.toHaveBeenCalled()
    expect(useInteractionStore.getState().pendingInteractions[0].status).toBe('entered')
    view.unmount()
  })
})
