/**
 * 文件工具卡片功能测试（点击交互全链路，声明取自真实 plugin.json）
 *
 * 覆盖两条此前零功能测试的链路：
 * 1. file_read（render.card=read）：点击卡片"打开文件"按钮 → 真实 fileOpener
 *    （仅 mock HTTP 层）→ 拉取内容 → 注册编辑器数据 + 工作区 Tab 出现；
 * 2. file_write（ui.chat_card 声明）：diff 块真实渲染（TextDiffView +/- 行）→
 *    切换"完整文件"视图（写后全文按行）→ 切回差异对比。
 *
 * 声明直接读 plugins/shared/tools/builtin_tools/plugin.json（与生产 schema 同源），
 * 防止测试 fixture 与插件声明镜像漂移。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ActivityCard from '@/components/chat/ActivityCard'
import { toolCallToActivity } from '@/utils/activityConverter'
import {
  enhanceActivityWithToolConfig,
  registerGlobalOpenFileCallback,
} from '@/utils/toolCardRegistry'
import { loadRenderIntents } from '@/utils/dshRenderIntent'
import { addChatCardDeclaration, clearChatCardDeclarations } from '@/utils/chatCardInterpreter'
import { openFile } from '@/services/fileOpener'
import { apiClient } from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { getFileEditorData } from '@/stores/fileEditorRegistry'
import type { MessageToolCall } from '@/types/models'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

// 本测试无 markdown 块；mock 掉以隔离 @lobehub 重依赖（渲染细节归专门测试）
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))

// ── 声明装载：直读真实 plugin.json（生产 /api/v1/schema 的源头）──
const PLUGIN_JSON_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../../plugins/shared/tools/builtin_tools/plugin.json',
)
const builtinTools = JSON.parse(readFileSync(PLUGIN_JSON_PATH, 'utf-8')) as {
  capabilities: { tools: Array<Record<string, unknown>> }
}
const fileReadDecl = builtinTools.capabilities.tools.find((t) => t.name === 'file_read')!
const fileWriteDecl = builtinTools.capabilities.tools.find((t) => t.name === 'file_write')!

const getMock = vi.mocked(apiClient.get)
const CONTENT_URL = (id: string) =>
  WORKSPACE_SERVICE_ENDPOINTS.workspaces_file_content_get.replace('{container_task_id}', id)

function makeToolCall(overrides: Partial<MessageToolCall>): MessageToolCall {
  return {
    call_id: 'tc-1',
    tool_name: 'file_read',
    tool_args: {},
    status: 'completed',
    duration_ms: 8,
    ...overrides,
  } as MessageToolCall
}

/** 渲染一条经真实增强管线的工具卡（converter → enhance → ActivityCard） */
function renderToolCard(toolCall: MessageToolCall) {
  const activity = enhanceActivityWithToolConfig(toolCallToActivity(toolCall), toolCall)
  return render(<ActivityCard activity={activity} defaultExpanded />)
}

describe('文件工具卡片功能: file_read 打开文件链路（点击 → fileOpener → 工作区 Tab）', () => {
  beforeEach(() => {
    getMock.mockReset()
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
    loadRenderIntents([fileReadDecl as never])
    // 对齐 main.tsx 的全局回调注册（真实 openFile，仅 HTTP 层 mock）
    registerGlobalOpenFileCallback((filePath, containerTaskId) =>
      openFile(filePath, { containerTaskId }),
    )
  })

  afterEach(() => {
    loadRenderIntents([])
    vi.restoreAllMocks()
  })

  it('read 卡渲染文件内容，点击"打开文件"→ 拉内容 + 建工作区 Tab + 注册编辑器数据', async () => {
    getMock.mockResolvedValueOnce({
      data: { success: true, content: 'print(1)\nprint(2)', size: 20 },
    } as never)

    renderToolCard(
      makeToolCall({
        tool_args: { path: 'src/main.py' },
        resultData: { file: '/app/src/main.py', lines: 2, size: 20, content: 'print(1)\nprint(2)' },
        containerTaskId: 'task-7',
      }),
    )

    // read 卡正文渲染（ReadBlock 行文本可见）
    expect(screen.getByText('print(1)')).toBeInTheDocument()

    // 点击"打开文件"按钮 → 真实 fileOpener 走通（file 为插件输出的宿主绝对路径）
    fireEvent.click(screen.getByRole('button', { name: '打开文件 /app/src/main.py' }))

    await waitFor(() => {
      expect(getFileEditorData('file-local-_app_src_main.py')?.content).toBe('print(1)\nprint(2)')
    })
    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('task-7'), { params: { path: '/app/src/main.py' } })

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0].moduleId).toBe('__file_editor__')
    expect(tabs[0].title).toBe('main.py')
  })

  it('卡片标题文本同样可点击打开（filePath 注入链）', async () => {
    getMock.mockResolvedValueOnce({
      data: { success: true, content: 'x', size: 1 },
    } as never)

    const { container } = renderToolCard(
      makeToolCall({
        tool_args: { path: 'lib/util.ts' },
        resultData: { file: '/app/lib/util.ts', lines: 1, size: 1, content: 'x' },
      }),
    )

    // 标题是可点击文件名（title 属性提示打开行为）
    const titleEl = container.querySelector('[title="点击打开文件: /app/lib/util.ts"]')
    expect(titleEl).toBeInTheDocument()
    fireEvent.click(titleEl!)

    await waitFor(() => {
      expect(getFileEditorData('file-local-_app_lib_util.ts')).toBeDefined()
    })
    // 无 containerTaskId 时走 _local（项目根）；绝对路径由后端直读
    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('_local'), { params: { path: '/app/lib/util.ts' } })
  })
})

describe('文件工具卡片功能: file_write diff 渲染 + 完整文件切换（真实 chat_card 声明）', () => {
  beforeEach(() => {
    getMock.mockReset()
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
    addChatCardDeclaration('file_write', (fileWriteDecl as never as { ui: { chat_card: never } }).ui.chat_card)
    registerGlobalOpenFileCallback((filePath, containerTaskId) =>
      openFile(filePath, { containerTaskId }),
    )
  })

  afterEach(() => {
    clearChatCardDeclarations()
    vi.restoreAllMocks()
  })

  /** search_replace 编辑已有文件的后端真实输出（fs_tools.file_write，扁平 result_data） */
  const editedOutput = {
    file: '/app/a.txt',
    added: 1,
    removed: 1,
    backup: null,
    old_content: 'hello world',
    new_content: 'hello Rust\nsecond line',
  }

  it('diff 块渲染 +/- 行与统计徽标；"写入内容"块在有 old_content 时隐藏', () => {
    renderToolCard(
      makeToolCall({
        tool_name: 'file_write',
        tool_args: { path: 'a.txt', action: 'search_replace', old_str: 'world', new_str: 'Rust' },
        resultData: editedOutput,
      }),
    )

    // 头部徽标 = 声明 diffStat 求值（added:1/removed:1）；diff 视图统计 = LCS 实算
    // （'hello world' → 'hello Rust'+'second line' = +2/-1），两处口径独立
    expect(screen.getAllByText('+1').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('-1').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByTestId('diff-added-count')).toHaveTextContent('+2')
    expect(screen.getByTestId('diff-removed-count')).toHaveTextContent('-1')

    // diff 块真实渲染（TextDiffView 未 mock）：删除行 + 新增行可见
    expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
    expect(screen.getByText('hello world')).toBeInTheDocument()
    expect(screen.getByText('hello Rust')).toBeInTheDocument()
    expect(screen.getByText('second line')).toBeInTheDocument()

    // unless: old_content → "写入内容"块隐藏
    expect(screen.queryByText('写入内容')).not.toBeInTheDocument()
  })

  it('切换"完整文件"视图：写后全文按行渲染（带行号），可切回差异对比', () => {
    renderToolCard(
      makeToolCall({
        tool_name: 'file_write',
        tool_args: { path: 'a.txt', action: 'search_replace', old_str: 'world', new_str: 'Rust' },
        resultData: editedOutput,
      }),
    )

    // 切到完整文件
    fireEvent.click(screen.getByTestId('diff-view-full'))
    const full = screen.getByTestId('diff-full-content')
    expect(full).toBeInTheDocument()
    // diff 视图隐藏、全文两行均在（含行号 1/2）
    expect(screen.queryByTestId('text-diff-view')).not.toBeInTheDocument()
    expect(screen.getByTestId('full-line-0').textContent).toContain('hello Rust')
    expect(screen.getByTestId('full-line-1').textContent).toContain('second line')
    expect(screen.getByTestId('full-line-1').textContent).toContain('2')

    // 切回差异对比
    fireEvent.click(screen.getByTestId('diff-view-diff'))
    expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
    expect(screen.queryByTestId('diff-full-content')).not.toBeInTheDocument()
  })

  it('新建文件（old_content 为空串）：diff 全新增渲染，"写入内容"块保留', () => {
    renderToolCard(
      makeToolCall({
        tool_name: 'file_write',
        tool_args: { path: 'new.txt', action: 'write', content: 'brand new' },
        resultData: { added: 1, removed: 0, backup: null, old_content: '', new_content: 'brand new' },
      }),
    )

    expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
    expect(screen.getByText('brand new')).toBeInTheDocument()
    // old_content 为空（falsy）→ unless 不命中 → 写入内容块显示
    expect(screen.getByText('写入内容')).toBeInTheDocument()
  })

  it('output.* 包装形态同样命中 diff 数据源（output.old_content || result.old_content 回退）', () => {
    renderToolCard(
      makeToolCall({
        tool_name: 'file_write',
        tool_args: { path: 'a.txt', action: 'search_replace', old_str: 'x', new_str: 'y' },
        // 部分链路以 ToolResult dict（{output: {...}}）透传——声明用 || 双路径兜底
        resultData: { output: editedOutput },
      }),
    )

    expect(screen.getByTestId('text-diff-view')).toBeInTheDocument()
    expect(screen.getByText('hello Rust')).toBeInTheDocument()
  })

  it('声明 open_file 动作按钮：点击经真实 fileOpener 打开目标文件', async () => {
    getMock.mockResolvedValueOnce({
      data: { success: true, content: 'hello Rust\nsecond line', size: 25 },
    } as never)

    renderToolCard(
      makeToolCall({
        tool_name: 'file_write',
        tool_args: { path: 'a.txt', action: 'search_replace', old_str: 'world', new_str: 'Rust' },
        resultData: editedOutput,
        containerTaskId: 'task-9',
      }),
    )

    // actions 区的"打开文件"按钮（与头部按钮 aria-label 不同名）；
    // 打开路径 = 输出 file 字段（写盘后绝对路径），而非 args.path 原始参数
    fireEvent.click(screen.getByRole('button', { name: '打开文件', exact: true }))

    await waitFor(() => {
      expect(getFileEditorData('file-local-_app_a.txt')?.content).toContain('hello Rust')
    })
    expect(getMock).toHaveBeenCalledWith(CONTENT_URL('task-9'), { params: { path: '/app/a.txt' } })
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
  })
})
