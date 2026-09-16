/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * HtmlPreviewWidget / PanelHostWidget 覆盖缺口补测
 *
 * HtmlPreviewWidget 契约：
 * - 内联 html 优先：直接 iframe srcDoc，不走 API
 * - filePath + containerTaskId：经 /workspaces 内容 API 读取后渲染；成功但无 content
 *   时按空串渲染（仍进 iframe 分支以外的加载中→有内容判定）
 * - API 业务失败（success:false）：显示 message 或兜底「读取失败」
 * - API 拒绝：显示错误 message
 * - 两参缺一时不发起请求，保持「加载中...」
 * - iframe sandbox 不含 allow-same-origin（不可信 HTML 隔离契约）
 * - title 回退 'HTML Preview'
 *
 * PanelHostWidget 契约：
 * - props.panel/kind/widget 三处 kind 来源优先级
 * - settings_hub / agents_panel / pipeline_manager 三分支渲染目标
 * - 未知名：显示「未知面板：{kind}」占位；空 kind 回退 SettingsHubWidget
 * - SettingsHubPanel / AgentsPanel 薄包装注入固定 panel
 *
 * 不可达说明（逐条）：
 * 1. PanelHostWidget 第 45 行 `{kind || '(empty)'}` 的 `'(empty)'` 分支不可达：
 *    空 kind 在上一行 `if (... || !kind) return <SettingsHubWidget/>` 已提前返回，
 *    走到该行时 kind 必为非空字符串。
 *
 * 测试策略：只 mock 外部依赖（workspaces API / 设置中枢与 AgentManagerPage 等
 * 重量级子页），断言渲染结果与请求参数；PanelHostWidget 的子页以可识别桩替身注入。
 */

import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
import { PanelHostWidget, SettingsHubPanel, AgentsPanel } from '@/components/schema/widgets/PanelHostWidget'
import { getWorkspaceFileContent } from '@/services/api/workspaces'
import type * as workspacesMod from '@/services/api/workspaces'

vi.mock('@/services/api/workspaces', async (importOriginal) => {
  const actual = await importOriginal<typeof workspacesMod>()
  return { ...actual, getWorkspaceFileContent: vi.fn() }
})

vi.mock('@/components/schema/widgets/SettingsHubWidget', () => ({
  SettingsHubWidget: (props: Record<string, unknown>) => (
    <div data-testid="settings-hub" data-props={JSON.stringify(props)} />
  ),
}))
vi.mock('@/components/agent/AgentManagerPage', () => ({
  AgentManagerPage: (props: Record<string, unknown>) => (
    <div data-testid="agent-manager" data-props={JSON.stringify(props)} />
  ),
}))
vi.mock('@/components/schema/widgets/PipelineManagerWidget', () => ({
  PipelineManagerWidget: () => <div data-testid="pipeline-manager-widget" />,
}))

const mockGetContent = vi.mocked(getWorkspaceFileContent)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('HtmlPreviewWidget', () => {
  it('内联 html 直接进 iframe（sandbox 无 allow-same-origin）', () => {
    render(<HtmlPreviewWidget html="<h1>hello</h1>" title="预览页" />)
    const frame = screen.getByTitle('预览页') as HTMLIFrameElement
    expect(frame.tagName).toBe('IFRAME')
    expect(frame.getAttribute('srcdoc')).toBe('<h1>hello</h1>')
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-forms allow-popups allow-modals')
    expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin')
    expect(mockGetContent).not.toHaveBeenCalled()
  })

  it('无 title 时 iframe 标题回退 HTML Preview', () => {
    render(<HtmlPreviewWidget html="<p>x</p>" />)
    expect(screen.getByTitle('HTML Preview')).toBeInTheDocument()
  })

  it('filePath + containerTaskId：读取成功后渲染内容', async () => {
    mockGetContent.mockResolvedValue({ success: true, content: '<b>from api</b>' })
    render(<HtmlPreviewWidget filePath="a/b.html" containerTaskId="ct-1" />)

    expect(screen.getByText('加载中...')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTitle('HTML Preview')).toHaveAttribute('srcdoc', '<b>from api</b>')
    })
    expect(mockGetContent).toHaveBeenCalledWith('ct-1', 'a/b.html')
  })

  // 现状契约（已在回报中记录，不改生产代码）：success:true 但 content 缺省时
  // setContent('')，而渲染判据是 `if (!content)` → 空串仍停留「加载中...」，
  // 即空 HTML 文件无法显示为空预览（疑似轻微缺陷，非本任务修复范围）。
  it('读取成功但 content 缺省：空串被 falsy 判据拦下，仍显示加载中', async () => {
    mockGetContent.mockResolvedValue({ success: true })
    render(<HtmlPreviewWidget filePath="empty.html" containerTaskId="ct-2" />)
    await waitFor(() => {
      expect(mockGetContent).toHaveBeenCalledWith('ct-2', 'empty.html')
    })
    expect(screen.getByText('加载中...')).toBeInTheDocument()
    expect(screen.queryByTitle('HTML Preview')).toBeNull()
  })

  it('业务失败（success:false）显示后端 message', async () => {
    mockGetContent.mockResolvedValue({ success: false, message: '文件不存在' })
    render(<HtmlPreviewWidget filePath="missing.html" containerTaskId="ct-3" />)
    await waitFor(() => {
      expect(screen.getByText('加载失败: 文件不存在')).toBeInTheDocument()
    })
  })

  it('业务失败且无 message 时兜底「读取失败」', async () => {
    mockGetContent.mockResolvedValue({ success: false })
    render(<HtmlPreviewWidget filePath="x.html" containerTaskId="ct-4" />)
    await waitFor(() => {
      expect(screen.getByText('加载失败: 读取失败')).toBeInTheDocument()
    })
  })

  it('API 拒绝时显示错误 message 且不再显示加载中', async () => {
    mockGetContent.mockRejectedValue(new Error('网络中断'))
    render(<HtmlPreviewWidget filePath="y.html" containerTaskId="ct-5" />)
    await waitFor(() => {
      expect(screen.getByText('加载失败: 网络中断')).toBeInTheDocument()
    })
    expect(screen.queryByText('加载中...')).toBeNull()
  })

  it.each([
    ['filePath 缺失', { containerTaskId: 'ct-6' }],
    ['containerTaskId 缺失', { filePath: 'z.html' }],
  ] as const)('%s 时不发起请求，保持加载中', async (_label, props) => {
    render(<HtmlPreviewWidget {...props} />)
    expect(screen.getByText('加载中...')).toBeInTheDocument()
    // 等待微任务队列排空后仍无请求
    await waitFor(() => {
      expect(mockGetContent).not.toHaveBeenCalled()
    })
  })

  it('html 属性变化时同步更新 iframe 内容（内联优先于文件读取）', () => {
    const { rerender } = render(<HtmlPreviewWidget html="<p>1</p>" filePath="a.html" containerTaskId="ct" />)
    expect(screen.getByTitle('HTML Preview')).toHaveAttribute('srcdoc', '<p>1</p>')
    rerender(<HtmlPreviewWidget html="<p>2</p>" filePath="a.html" containerTaskId="ct" />)
    expect(screen.getByTitle('HTML Preview')).toHaveAttribute('srcdoc', '<p>2</p>')
    expect(mockGetContent).not.toHaveBeenCalled()
  })
})

describe('PanelHostWidget', () => {
  it('panel=settings_hub 渲染设置中枢', () => {
    render(<PanelHostWidget panel="settings_hub" />)
    expect(screen.getByTestId('settings-hub')).toBeInTheDocument()
  })

  it('kind=agents_panel 渲染 Agent 管理页并透传其余 props', () => {
    render(<PanelHostWidget kind="agents_panel" typeLabels={{ a: 'A' }} />)
    const node = screen.getByTestId('agent-manager')
    expect(JSON.parse(node.getAttribute('data-props') as string)).toMatchObject({
      typeLabels: { a: 'A' },
    })
  })

  it('widget=pipeline_manager 渲染管道管理面板容器', () => {
    render(<PanelHostWidget widget="pipeline_manager" />)
    expect(screen.getByTestId('pipeline-manager')).toBeInTheDocument()
    expect(screen.getByTestId('pipeline-manager-widget')).toBeInTheDocument()
  })

  it('未知面板显示占位文案（含 kind 名）', () => {
    render(<PanelHostWidget panel="no_such_panel" />)
    expect(screen.getByText('未知面板：no_such_panel')).toBeInTheDocument()
  })

  it('空 props 回退设置中枢（default 分支）', () => {
    render(<PanelHostWidget />)
    expect(screen.getByTestId('settings-hub')).toBeInTheDocument()
  })

  it('panel 优先于 kind 与 widget', () => {
    render(<PanelHostWidget panel="agents_panel" kind="settings_hub" widget="pipeline_manager" />)
    expect(screen.getByTestId('agent-manager')).toBeInTheDocument()
    expect(screen.queryByTestId('settings-hub')).toBeNull()
  })

  it('薄包装 SettingsHubPanel / AgentsPanel 注入对应固定 panel', () => {
    const { unmount } = render(<SettingsHubPanel />)
    expect(screen.getByTestId('settings-hub')).toBeInTheDocument()
    unmount()

    render(<AgentsPanel />)
    expect(screen.getByTestId('agent-manager')).toBeInTheDocument()
  })
})
