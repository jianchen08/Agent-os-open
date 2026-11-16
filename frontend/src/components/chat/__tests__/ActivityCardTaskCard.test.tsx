/** @feature: FP-0.2.任务卡导航 | @ci: frontend-test
 *
 * 任务卡片功能测试（点击交互全链路，声明取自真实 plugin.json）
 *
 * 覆盖 task_submit（render.card=task_card）卡片的两条打开入口：
 * 1. 点击头部「打开任务执行管道」图标按钮 → pipelineNavigator 跳转；
 * 2. 点击卡片标题（任务名）→ 同一跳转。
 *
 * 导航参数契约：pipelineId + 任务标题（子标签名）+ taskId + 当前活跃会话
 * （血缘提示，自环子任务管道三级查找必落空，main.tsx 注册形态同源）。
 *
 * 声明直接读 plugins/shared/task_submit/plugin.json（与生产 schema 同源），
 * 防止测试 fixture 与插件声明镜像漂移。
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '@/components/chat/ActivityCard'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { useSessionStore } from '@/stores/sessionStore'
import { toolCallToActivity } from '@/utils/activityConverter'
import { loadRenderIntents } from '@/utils/renderIntent'
import {
  enhanceActivityWithToolConfig,
  registerGlobalOpenTaskPipelineCallback,
} from '@/utils/toolCardRegistry'
import type { MessageToolCall } from '@/types/models'

vi.mock('@/services/pipelineNavigator', () => ({
  navigateToPipeline: vi.fn().mockResolvedValue(true),
}))

// 本测试无 markdown 块；mock 掉以隔离 @lobehub 重依赖（渲染细节归专门测试）
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))

// ── 声明装载：直读真实 plugin.json（生产 /api/v1/schema 的源头）──
const PLUGIN_JSON_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../../plugins/shared/task_submit/plugin.json',
)
const taskSubmitPlugin = JSON.parse(readFileSync(PLUGIN_JSON_PATH, 'utf-8')) as {
  capabilities: { tools: Array<Record<string, unknown>> }
}
const taskSubmitDecl = taskSubmitPlugin.capabilities.tools.find((t) => t.name === 'task_submit')!

const navigateMock = vi.mocked(navigateToPipeline)

function makeToolCall(overrides: Partial<MessageToolCall>): MessageToolCall {
  return {
    call_id: 'tc-1',
    tool_name: 'task_submit',
    tool_args: { goal_title: '修复登录超时', target_id: 'coder' },
    status: 'completed',
    duration_ms: 120,
    ...overrides,
  } as MessageToolCall
}

/** 渲染一条经真实增强管线的工具卡（converter → enhance → ActivityCard） */
function renderToolCard(toolCall: MessageToolCall) {
  const activity = enhanceActivityWithToolConfig(toolCallToActivity(toolCall), toolCall)
  return render(<ActivityCard activity={activity} defaultExpanded />)
}

describe('任务卡片功能: task_submit 打开执行管道链路（点击 → pipelineNavigator）', () => {
  beforeEach(() => {
    navigateMock.mockClear()
    loadRenderIntents([taskSubmitDecl as never])
    // 对齐 main.tsx 的全局回调注册（当前活跃会话作血缘提示）
    useSessionStore.setState({ activeSessionId: 'sess-1' })
    registerGlobalOpenTaskPipelineCallback(async (pipelineId, info) => {
      await navigateToPipeline(pipelineId, {
        agentName: info?.title || '子任务',
        taskId: info?.taskId,
        fallbackSessionId: useSessionStore.getState().activeSessionId ?? undefined,
      })
    })
  })

  afterEach(() => {
    loadRenderIntents([])
    registerGlobalOpenTaskPipelineCallback(() => {})
    vi.restoreAllMocks()
  })

  const submittedCall = () =>
    makeToolCall({
      resultData: {
        task_id: 'abc123def456',
        title: '修复登录超时',
        status: 'running',
        pipeline_id: 'abc123def456',
        target_id: 'coder',
        message: '任务 [修复登录超时]（ID: abc123def456）已提交，执行管道已创建，任务正在后台执行。',
      },
    })

  it('头部标题=任务名、摘要=任务短 id；点击「打开任务执行管道」按钮 → 携 taskId/血缘会话跳转', async () => {
    const { container } = renderToolCard(submittedCall())

    // 标题主位=任务名（详情 kv 也含同文案，只断头部可点元素）
    expect(container.querySelector('[title="点击打开任务执行管道: 修复登录超时"]')).toBeInTheDocument()
    // 摘要行 + 详情 kv 均展示任务短 id（任务即管道，两 id 同值）
    expect(screen.getAllByText('abc123def456').length).toBeGreaterThanOrEqual(1)

    fireEvent.click(screen.getByRole('button', { name: '打开任务执行管道 修复登录超时' }))

    await waitFor(() => {
      expect(navigateMock).toHaveBeenCalledTimes(1)
    })
    expect(navigateMock).toHaveBeenCalledWith('abc123def456', {
      agentName: '修复登录超时',
      taskId: 'abc123def456',
      fallbackSessionId: 'sess-1',
    })
  })

  it('卡片标题（任务名）同样可点击跳转（pipelineId 注入链）', async () => {
    const { container } = renderToolCard(submittedCall())

    const titleEl = container.querySelector('[title="点击打开任务执行管道: 修复登录超时"]')
    expect(titleEl).toBeInTheDocument()
    fireEvent.click(titleEl!)

    await waitFor(() => {
      expect(navigateMock).toHaveBeenCalledTimes(1)
    })
    expect(navigateMock).toHaveBeenCalledWith('abc123def456', {
      agentName: '修复登录超时',
      taskId: 'abc123def456',
      fallbackSessionId: 'sess-1',
    })
  })

  it('在途调用（无结果）→ 标题=工具名人性化、无打开入口（无处可跳不伪装）', () => {
    renderToolCard(makeToolCall({ status: 'running' }))

    expect(screen.getByText('提交任务')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /打开任务执行管道/ })).not.toBeInTheDocument()
    expect(document.querySelector('[title^="点击打开任务执行管道"]')).toBeNull()
  })
})
