// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * Schema 组件覆盖缺口补测（SchemaFullscreenHost / PageRenderer / InlineEditWidget /
 * ReviewDocumentWidget）
 *
 * 断行为：用户可见的渲染产物、点击/键盘交互的可观察副作用（提交回调、props 透传、
 * 视图切换），不断言内部实现。
 *
 * 覆盖契约：
 * - SchemaFullscreenHost 队列导航：多条目时「上一个/下一个」按钮在首/末位置
 *   分别禁用（边界），中间位置可双向移动；
 * - SchemaFullscreenHost conversation 模式：输入正文后「提交」把草稿作为 feedback
 *   发送；空草稿时按钮禁用（两组互斥输入）；
 * - SchemaFullscreenHost 无匹配 widget 声明 → 显式占位文案（不静默空白）；
 * - PageRenderer：workspace 空间的 widget 命中降级映射 → 渲染且打 data-fallback
 *   标记（声明↔注册断链可排查）；无页面 → 「暂无页面」空态；pages props 与
 *   registry 来源二选一（两组输入）；
 * - InlineEditWidget：multiline 走 textarea（Cmd/Ctrl+Enter 提交、Esc 取消），
 *   单行走 input（Enter 提交）；空值显示 placeholder 而非空串；
 * - ReviewDocumentWidget：多制品切换（activeId 变更改变内容区）、diff 视图开关
 *   按钮切换文案、批注区可关闭（annotation=false）、无制品 → 空态。
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
// ── SchemaFullscreenHost 依赖面 mock（与既有测试同构） ──
const handlers = new Map<string, (raw: Record<string, unknown>) => void>()
const sendInteractionResponseMock = vi.fn()

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, handler: (raw: Record<string, unknown>) => void) => {
      handlers.set(event, handler)
    }),
    unsubscribe: vi.fn((event: string) => {
      handlers.delete(event)
    }),
    sendInteractionResponse: (...args: unknown[]) => sendInteractionResponseMock(...args),
    sendApproval: vi.fn(),
  },
}))

const sessionState = { activeSessionId: 'session-1' }
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: Object.assign(() => sessionState, { getState: () => sessionState }),
}))
import { PageRenderer } from '@/components/schema/PageRenderer'
import {
  SchemaFullscreenHost,
  collectFullscreenEventDeclarations,
  toSchemaEventItem,
} from '@/components/schema/SchemaFullscreenHost'
import { InlineEditWidget } from '@/components/schema/widgets/InlineEditWidget'
import { ReviewDocumentWidget } from '@/components/schema/widgets/ReviewDocumentWidget'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { PageDeclaration, WidgetDeclaration } from '@/services/schema/ContributionRegistry'

const approvalDecl: WidgetDeclaration = {
  id: 'approval_panel',
  type: 'review_document',
  space: 'fullscreen',
  trigger: 'on_event:approval.created',
  props: { diff_view: true, annotation: true },
}

/** 仅声明 trigger 但 widget 类型不存在于注册表（渲染应走显式占位） */
const unmappedDecl: WidgetDeclaration = {
  id: 'ghost_panel',
  type: 'no_such_widget_type',
  space: 'fullscreen',
  trigger: 'on_event:ghost.created',
}

function fire(event: string, payload: Record<string, unknown>) {
  const handler = handlers.get(event)
  if (!handler) throw new Error(`${event} handler 未注册`)
  act(() => handler(payload))
}

/* ---------------------------------- SchemaFullscreenHost ---------------------------------- */

describe('SchemaFullscreenHost 队列导航与 conversation 提交', () => {
  beforeEach(() => {
    handlers.clear()
    sendInteractionResponseMock.mockClear()
  })

  it('多条目：首条「上一个」禁用，「下一个」可用；移到末条后「下一个」禁用', () => {
    render(<SchemaFullscreenHost declarations={[approvalDecl]} />)

    fire('approval.created', { request_id: 'r1', title: '第一条', mode: 'review' })
    fire('approval.created', { request_id: 'r2', title: '第二条', mode: 'review' })

    expect(screen.getByText('1 / 2')).toBeTruthy()
    const prev = screen.getByTitle('上一个')
    const next = screen.getByTitle('下一个')
    // 首条：上一个禁用、下一个可用（边界）
    expect(prev).toBeDisabled()
    expect(next).not.toBeDisabled()

    // 点下一个 → 到末条
    act(() => next.click())
    expect(screen.getByText('2 / 2')).toBeTruthy()
    expect(screen.getByTitle('下一个')).toBeDisabled()
    expect(screen.getByTitle('上一个')).not.toBeDisabled()

    // 点上一个 → 回首条（双向可达）
    act(() => screen.getByTitle('上一个').click())
    expect(screen.getByText('1 / 2')).toBeTruthy()
  })

  it('conversation 模式：空草稿时提交按钮禁用；输入正文后提交带 feedback 发送', () => {
    render(<SchemaFullscreenHost declarations={[approvalDecl]} />)

    fire('approval.created', { request_id: 'rc', title: '请回复', mode: 'conversation' })

    const textarea = screen.getByPlaceholderText('请输入回复...') as HTMLTextAreaElement
    const submitBtn = screen.getByRole('button', { name: '提交' })
    // 空草稿（含纯空白）：禁用，不发送
    expect(submitBtn).toBeDisabled()
    fireEvent.change(textarea, { target: { value: '   ' } })
    expect(submitBtn).toBeDisabled()
    expect(sendInteractionResponseMock).not.toHaveBeenCalled()

    // 输入正文 → 可提交
    fireEvent.change(textarea, { target: { value: '这是我的意见' } })
    expect(submitBtn).not.toBeDisabled()
    act(() => submitBtn.click())

    expect(sendInteractionResponseMock).toHaveBeenCalledWith(
      'session-1',
      'rc',
      expect.objectContaining({ response_type: 'answered', feedback: '这是我的意见' }),
    )
    expect(screen.queryByTestId('fullscreen-toolbar')).toBeNull()
  })

  it('声明 widget 类型既无注册也无降级路径 → 不渲染 + console.warn 报断链', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    render(<SchemaFullscreenHost declarations={[unmappedDecl]} />)

    fire('ghost.created', { request_id: 'rg', title: '幽灵面板', mode: 'review' })

    // 浮层打开（标题可见），但 widget 区无解析结果 → 无声明组件渲染
    expect(screen.getByText('幽灵面板')).toBeTruthy()
    expect(screen.queryByTestId('declared-widget-ghost_panel')).toBeNull()
    // 断链诊断：告警列出未解析的声明 id/type
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('个 widget 声明未解析'),
      expect.arrayContaining([
        expect.objectContaining({ id: 'ghost_panel', type: 'no_such_widget_type' }),
      ]),
    )
    warnSpy.mockRestore()
  })

  it('review 模式：点「批准」→ 以 approved 经 interaction 通道提交（与拒绝成对）', () => {
    render(<SchemaFullscreenHost declarations={[approvalDecl]} />)

    fire('approval.created', { request_id: 'r-ok', title: '待批准文档', mode: 'review' })

    const approveBtn = screen.getByRole('button', { name: '批准' })
    expect(approveBtn).not.toBeDisabled()
    act(() => approveBtn.click())

    expect(sendInteractionResponseMock).toHaveBeenCalledWith(
      'session-1',
      'r-ok',
      expect.objectContaining({ response_type: 'answered', selected_option: 'approved' }),
    )
    expect(screen.queryByTestId('fullscreen-toolbar')).toBeNull()
  })

  it('collectFullscreenEventDeclarations：非 on_event 触发（route_signal）不被收集', () => {
    const decls: WidgetDeclaration[] = [
      { ...approvalDecl, id: 'a1', trigger: 'on_route_signal:wait' },
      { ...approvalDecl, id: 'a2', trigger: 'on_event:approval.created' },
    ]
    const collected = collectFullscreenEventDeclarations(decls)
    expect(collected.map((c) => c.declaration.id)).toEqual(['a2'])
  })

  it('toSchemaEventItem：options 非数组时不携带，mode 缺省为 review', () => {
    const item = toSchemaEventItem('approval_panel', 'approval.created', {
      request_id: 'r-norm',
      options: 'not-an-array',
    })
    expect(item?.requestId).toBe('r-norm')
    expect(item?.options).toEqual([])
    expect(item?.mode).toBe('review')
  })
})

/* -------------------------------------- PageRenderer -------------------------------------- */

describe('PageRenderer 渲染来源与降级标记', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  function makePage(overrides: Partial<PageDeclaration> = {}): PageDeclaration {
    return { type: 'pages', id: 'p1', space: 'workspace', ...overrides } as PageDeclaration
  }

  it('widget 命中降级映射（非精确注册）→ 渲染降级组件并打 data-fallback 标记', () => {
    // 注册 'table'（降级候选），声明 'kanban'（未直接注册，映射到 table）
    widgetRegistry.register('table', () => <div data-testid="fallback-widget">降级组件</div>, {
      name: 'table',
      supportedSpaces: ['workspace'],
    })

    const { unmount } = render(<PageRenderer pages={[makePage({ id: 'fb', widget: 'kanban' })]} />)

    expect(screen.getByTestId('fallback-widget')).toBeTruthy()
    // 降级标记：声明名挂在 data-fallback 上（声明↔注册断链可排查）
    const marked = document.querySelector('[data-fallback="kanban"]')
    expect(marked).toBeTruthy()
    expect(marked?.contains(screen.getByTestId('fallback-widget'))).toBe(true)
    unmount()

    // 对照：同一组件精确注册时渲染无降级标记（两组区分输入）
    render(<PageRenderer pages={[makePage({ id: 'exact', widget: 'table' })]} />)
    expect(screen.getByTestId('fallback-widget')).toBeTruthy()
    expect(document.querySelector('[data-fallback]')).toBeNull()
  })

  it('pages 为空数组 → 「暂无页面」空态；无 space 过滤时渲染全部（两组输入）', () => {
    const { unmount } = render(<PageRenderer pages={[]} space="workspace" />)
    expect(screen.getByText('暂无页面')).toBeTruthy()
    unmount()

    // 无 space 限制：workspace + settings 两个页面都渲染
    render(
      <PageRenderer
        pages={[
          makePage({ id: 'w1', title: '工作区' }),
          makePage({ id: 's1', title: '设置', space: 'settings' }),
        ]}
      />,
    )
    expect(screen.getByTestId('page-w1')).toBeTruthy()
    expect(screen.getByTestId('page-s1')).toBeTruthy()
  })

  it('pages props 优先于 registry 来源（两者同时存在时只渲染 props）', () => {
    contributionRegistry.register({
      ...makePage({ id: 'from-registry', title: '注册表页' }),
      type: 'pages',
    })

    render(<PageRenderer pages={[makePage({ id: 'from-props', title: 'Props 页' })]} />)

    expect(screen.getByTestId('page-from-props')).toBeTruthy()
    expect(screen.queryByTestId('page-from-registry')).toBeNull()
  })

  it('slot 过滤：只渲染匹配 slot 的页面', () => {
    render(
      <PageRenderer
        pages={[
          makePage({ id: 'tab-page', slot: 'tab' }),
          makePage({ id: 'nav-page', slot: 'nav' }),
        ]}
        slot="tab"
      />,
    )
    expect(screen.getByTestId('page-tab-page')).toBeTruthy()
    expect(screen.queryByTestId('page-nav-page')).toBeNull()
  })

  it('声明 datasourceUri 的 schema 页：提交把表单值 PUT 到该数据源', async () => {
    const putSpy = vi.fn().mockResolvedValue({ data: { ok: true } })
    const clientMod = await import('@/services/api/client')
    vi.spyOn(clientMod.default, 'put').mockImplementation(putSpy as never)

    const page = makePage({
      id: 'ds-1',
      title: '可写配置页',
      space: 'settings',
      datasourceUri: '/api/v1/config/demo',
      schema: { fields: [{ name: 'name', type: 'string', label: '名称' }] },
    })
    render(<PageRenderer pages={[page]} />)

    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '新名字' } })
    fireEvent.click(screen.getByRole('button', { name: /保\s*存/ }))

    await vi.waitFor(() => expect(putSpy).toHaveBeenCalledTimes(1))
    expect(putSpy).toHaveBeenCalledWith('/api/v1/config/demo', { name: '新名字' })
  })

  it('无 datasourceUri 的 schema 页：提交只走本地交互，不打网络', async () => {
    const clientMod = await import('@/services/api/client')
    const putSpy = vi.spyOn(clientMod.default, 'put').mockResolvedValue({ data: {} } as never)

    const page = makePage({
      id: 'local-1',
      title: '本地表单页',
      space: 'settings',
      schema: { fields: [{ name: 'name', type: 'string', label: '名称' }] },
    })
    render(<PageRenderer pages={[page]} />)

    fireEvent.click(screen.getByRole('button', { name: /保\s*存/ }))
    // 给潜在异步提交一个落地窗口
    await act(async () => {
      await Promise.resolve()
    })
    expect(putSpy).not.toHaveBeenCalled()
  })

  it('order 排序：order 小的先渲染（缺省 50）', () => {    render(
      <PageRenderer
        pages={[
          makePage({ id: 'late', order: 90 }),
          makePage({ id: 'early', order: 10 }),
          makePage({ id: 'default-order' }),
        ]}
      />,
    )
    const ids = Array.from(document.querySelectorAll('[data-testid^="page-"]'))
      .map((n) => n.getAttribute('data-testid'))
      // 排除容器自身与占位内部 testid（只留页面卡片）
      .filter((id) => !id.startsWith('page-renderer') && !id.startsWith('page-placeholder-'))
    expect(ids).toEqual(['page-early', 'page-default-order', 'page-late'])
  })
})

/* ------------------------------------ InlineEditWidget ------------------------------------ */

describe('InlineEditWidget 单行与多行编辑', () => {
  it('多行：Cmd/Ctrl+Enter 提交，Esc 取消；普通 Enter 不提交', () => {
    const onChange = vi.fn()
    render(<InlineEditWidget value="初稿" onChange={onChange} multiline />)

    fireEvent.click(screen.getByTestId('inline-edit-view'))
    const area = screen.getByTestId('inline-edit-input') as HTMLTextAreaElement
    expect(area.tagName).toBe('TEXTAREA')

    fireEvent.change(area, { target: { value: '多行\n版本一' } })
    // 普通 Enter：多行场景不提交（换行语义）
    fireEvent.keyDown(area, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()

    // Ctrl+Enter：提交
    fireEvent.keyDown(area, { key: 'Enter', ctrlKey: true })
    expect(onChange).toHaveBeenCalledWith('多行\n版本一')
  })

  it('多行 Esc 取消：不提交且退回只读态显示原值', () => {
    const onChange = vi.fn()
    render(<InlineEditWidget value="初稿" onChange={onChange} multiline />)

    fireEvent.click(screen.getByTestId('inline-edit-view'))
    const area = screen.getByTestId('inline-edit-input')
    fireEvent.change(area, { target: { value: '改到一半' } })
    fireEvent.keyDown(area, { key: 'Escape' })

    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByTestId('inline-edit-view').textContent).toBe('初稿')
  })

  it('失焦提交：改过值 → onChange；未改值 → 不触发（两组区分输入）', () => {
    const onChange = vi.fn()
    const { unmount } = render(<InlineEditWidget value="原值" onChange={onChange} />)

    fireEvent.click(screen.getByTestId('inline-edit-view'))
    const input = screen.getByTestId('inline-edit-input')
    fireEvent.change(input, { target: { value: '新值' } })
    fireEvent.blur(input)
    expect(onChange).toHaveBeenCalledWith('新值')
    unmount()

    const onChange2 = vi.fn()
    render(<InlineEditWidget value="原值" onChange={onChange2} />)
    fireEvent.click(screen.getByTestId('inline-edit-view'))
    fireEvent.blur(screen.getByTestId('inline-edit-input'))
    expect(onChange2).not.toHaveBeenCalled()
  })

  it('空值：只读态显示 placeholder 文案（viewLabel 缺省回落）', () => {
    render(<InlineEditWidget value={undefined} placeholder="点击填写" />)
    expect(screen.getByTestId('inline-edit-view').textContent).toBe('点击填写')
  })

  it('单行 Enter 提交新值；再进编辑按 Escape 取消不改值（两条键盘路径）', () => {
    const onChange = vi.fn()
    render(<InlineEditWidget value="单行原值" onChange={onChange} />)

    fireEvent.click(screen.getByTestId('inline-edit-view'))
    const input = screen.getByTestId('inline-edit-input') as HTMLInputElement
    expect(input.tagName).toBe('INPUT')
    fireEvent.change(input, { target: { value: '单行新值' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('单行新值')
    // 提交后退回只读态（组件受控于外部 value，此处展示 props 原值）
    expect(screen.getByTestId('inline-edit-view')).toBeTruthy()

    onChange.mockClear()
    fireEvent.click(screen.getByTestId('inline-edit-view'))
    const input2 = screen.getByTestId('inline-edit-input')
    fireEvent.change(input2, { target: { value: '被丢弃的编辑' } })
    fireEvent.keyDown(input2, { key: 'Escape' })
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByTestId('inline-edit-view')).toBeTruthy()
  })
})

/* ---------------------------------- ReviewDocumentWidget ---------------------------------- */

describe('ReviewDocumentWidget 多制品与批注区', () => {
  const artifacts = [
    { id: 'a1', title: '第一章', content: '第一章正文' },
    { id: 'a2', title: '第二章', content: '第二章正文', baselineContent: '第二章旧稿' },
  ]

  it('多制品切换：点击制品按钮切换内容区（首制品为默认）', () => {
    render(<ReviewDocumentWidget artifacts={artifacts} annotation={false} />)

    expect(screen.getByText('第一章正文')).toBeTruthy()
    expect(screen.queryByText('第二章正文')).toBeNull()

    // 制品切换按钮（标题文本）
    fireEvent.click(screen.getByRole('button', { name: '第二章' }))
    expect(screen.getByText('第二章正文')).toBeTruthy()
    expect(screen.queryByText('第一章正文')).toBeNull()
  })

  it('diff_view 开启且首制品带基线 → 出现视图切换按钮，点击后文案翻转', () => {
    // showDiff 取当前选中制品（默认首条）：首条必须带 baselineContent 才启用 diff
    const withBaseline = [
      { id: 'b1', title: '基线章', content: '新稿', baselineContent: '旧稿' },
      { id: 'b2', title: '无基线章', content: '纯文本' },
    ]
    render(<ReviewDocumentWidget artifacts={withBaseline} diff_view annotation={false} />)

    // 默认 side-by-side：按钮提示切到统一视图
    const toggle = screen.getByRole('button', { name: /切换为统一视图/ })
    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: /切换为对比视图/ })).toBeTruthy()
  })

  it('diff_view 开启但选中制品无基线 → 无切换按钮，退回纯文本渲染', () => {
    render(<ReviewDocumentWidget artifacts={artifacts} diff_view annotation={false} />)
    expect(screen.queryByRole('button', { name: /切换为/ })).toBeNull()
    expect(screen.getByText('第一章正文')).toBeTruthy()
  })

  it('diff_view 未开启：不出现切换按钮，内容按纯文本渲染', () => {
    render(<ReviewDocumentWidget artifacts={artifacts} annotation={false} />)
    expect(screen.queryByRole('button', { name: /切换为/ })).toBeNull()
    expect(screen.getByText('第一章正文')).toBeTruthy()
  })

  it('批注区：annotation=false 隐藏；开启时显示批注数与作者/内容', () => {
    const { unmount } = render(
      <ReviewDocumentWidget artifacts={artifacts} annotations={[]} annotation={false} />,
    )
    expect(screen.queryByText(/批注（/)).toBeNull()
    unmount()

    render(
      <ReviewDocumentWidget
        artifacts={artifacts}
        annotations={[
          { id: 'n1', author: '评审员甲', suggestion: '建议补充结论' },
          { id: 'n2', suggestion: '匿名批注' },
        ]}
      />,
    )
    expect(screen.getByText('批注（2）')).toBeTruthy()
    expect(screen.getByText('评审员甲')).toBeTruthy()
    expect(screen.getByText('建议补充结论')).toBeTruthy()
    // 无作者的批注不渲染作者行，但内容仍在
    expect(screen.getByText('匿名批注')).toBeTruthy()
  })

  it('无制品 → 空态文案；annotations 非法（非数组）按 0 条处理', () => {
    const { unmount } = render(<ReviewDocumentWidget artifacts={[]} />)
    expect(screen.getByText('暂无可审阅的制品')).toBeTruthy()
    unmount()

    render(<ReviewDocumentWidget artifacts={artifacts} annotations="not-an-array" />)
    expect(screen.getByText('批注（0）')).toBeTruthy()
    expect(screen.getByText('暂无批注')).toBeTruthy()
  })

  it('制品缺 title 时以 id 兜底显示（标题与切换按钮）', () => {
    render(<ReviewDocumentWidget artifacts={[{ id: 'no-title', content: '正文' }]} annotation={false} />)
    expect(screen.getByRole('heading', { name: 'no-title' })).toBeTruthy()
  })
})

/* ------------------------------------ WorkspacePanel -------------------------------------- */

describe('WorkspacePanel 空态与全屏入口', () => {
  it('无标签 → 导航页兜底', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    render(
      <WorkspacePanel
        tabs={[]}
        activeTabId={null}
        onTabChange={vi.fn()}
        onTabClose={vi.fn()}
        renderTabContent={() => null}
      />,
    )
    expect(screen.getByTestId('workspace-nav-page')).toBeTruthy()
  })

  it('传入 onFullscreen 时渲染全屏按钮，文案随 isFullscreen 翻转', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    const onFullscreen = vi.fn()
    const props = {
      tabs: [{ id: 't1', title: '标签一', isActive: true, isPinned: false, moduleId: 'm', icon: '' }],
      activeTabId: 't1',
      onTabChange: vi.fn(),
      onTabClose: vi.fn(),
      renderTabContent: () => <div>内容</div>,
      onFullscreen,
    }
    const { unmount } = render(<WorkspacePanel {...(props as never)} />)

    const btn = screen.getByTestId('workspace-toggle-fullscreen')
    expect(btn.getAttribute('aria-label')).toBe('铺满全屏')
    fireEvent.click(btn)
    expect(onFullscreen).toHaveBeenCalledTimes(1)
    unmount()

    render(<WorkspacePanel {...(props as never)} isFullscreen />)
    expect(screen.getByTestId('workspace-toggle-fullscreen').getAttribute('aria-label')).toBe(
      '退出全屏',
    )
  })

  it('未传 onFullscreen → 不渲染全屏按钮', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    render(
      <WorkspacePanel
        tabs={[{ id: 't1', title: '标签一', isActive: true, isPinned: false, moduleId: 'm', icon: '' }] as never}
        activeTabId="t1"
        onTabChange={vi.fn()}
        onTabClose={vi.fn()}
        renderTabContent={() => <div>内容</div>}
      />,
    )
    expect(screen.queryByTestId('workspace-toggle-fullscreen')).toBeNull()
  })

  it('右键菜单三个动作：关闭本标签 / 关闭其他 / 关闭所有（各自落到 store）', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    const { useLayoutModeStore } = await import('@/stores/layoutModeStore')
    const closeTab = vi.spyOn(useLayoutModeStore.getState(), 'closeWorkspaceTab')
    const closeOther = vi.spyOn(useLayoutModeStore.getState(), 'closeOtherWorkspaceTabs')
    const closeAll = vi.spyOn(useLayoutModeStore.getState(), 'closeAllWorkspaceTabs')

    const tabs = [
      { id: 't1', title: '标签一', isActive: true, isPinned: false, moduleId: 'm', icon: '' },
      { id: 't2', title: '标签二', isActive: false, isPinned: false, moduleId: 'm', icon: '' },
    ]
    render(
      <WorkspacePanel
        tabs={tabs as never}
        activeTabId="t1"
        onTabChange={vi.fn()}
        onTabClose={vi.fn()}
        renderTabContent={() => <div>内容</div>}
      />,
    )

    // 「关闭本标签」
    fireEvent.contextMenu(screen.getByTestId('workspace-tab-t1'))
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close'))
    expect(closeTab).toHaveBeenCalledWith('t1')
    // 动作后菜单收起
    expect(screen.queryByTestId('workspace-tab-menu-close')).toBeNull()

    // 「关闭其他标签」
    fireEvent.contextMenu(screen.getByTestId('workspace-tab-t2'))
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-other'))
    expect(closeOther).toHaveBeenCalledWith('t2')

    // 「关闭所有标签」
    fireEvent.contextMenu(screen.getByTestId('workspace-tab-t1'))
    fireEvent.click(screen.getByTestId('workspace-tab-menu-close-all'))
    expect(closeAll).toHaveBeenCalledTimes(1)

    closeTab.mockRestore()
    closeOther.mockRestore()
    closeAll.mockRestore()
  })

  it('右键菜单点击外部与 Esc 都收起；标签点击触发 onTabChange（互斥路径）', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    const onTabChange = vi.fn()
    render(
      <WorkspacePanel
        tabs={[
          { id: 't1', title: '标签一', isActive: true, isPinned: false, moduleId: 'm', icon: '' },
        ] as never}
        activeTabId="t1"
        onTabChange={onTabChange}
        onTabClose={vi.fn()}
        renderTabContent={() => <div>内容</div>}
      />,
    )

    // 点击外部（document mousedown 落在菜单之外）→ 收起
    fireEvent.contextMenu(screen.getByTestId('workspace-tab-t1'))
    expect(screen.getByTestId('workspace-tab-menu-close')).toBeTruthy()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByTestId('workspace-tab-menu-close')).toBeNull()

    // Esc 收起
    fireEvent.contextMenu(screen.getByTestId('workspace-tab-t1'))
    expect(screen.getByTestId('workspace-tab-menu-close')).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('workspace-tab-menu-close')).toBeNull()

    // 标签点击（正常路径）触发 onTabChange，不弹菜单
    fireEvent.click(screen.getByTestId('workspace-tab-t1'))
    expect(onTabChange).toHaveBeenCalledWith('t1')
    expect(screen.queryByTestId('workspace-tab-menu-close')).toBeNull()
  })

  it('关闭按钮点击触发 onTabClose（非钉住标签）', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    const onTabClose = vi.fn()
    render(
      <WorkspacePanel
        tabs={[
          { id: 't1', title: '标签一', isActive: false, isPinned: false, moduleId: 'm', icon: '' },
        ] as never}
        activeTabId="t1"
        onTabChange={vi.fn()}
        onTabClose={onTabClose}
        renderTabContent={() => <div>内容</div>}
      />,
    )
    fireEvent.click(screen.getByTestId('workspace-tab-close-t1'))
    expect(onTabClose).toHaveBeenCalledWith('t1')
  })

  it('钉住标签：不渲染关闭按钮且右键菜单「关闭本标签」禁用', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    render(
      <WorkspacePanel
        tabs={[
          { id: 'pin1', title: '主页', isActive: true, isPinned: true, moduleId: 'm', icon: '' },
        ] as never}
        activeTabId="pin1"
        onTabChange={vi.fn()}
        onTabClose={vi.fn()}
        renderTabContent={() => <div>内容</div>}
      />,
    )
    expect(screen.queryByTestId('workspace-tab-close-pin1')).toBeNull()
    fireEvent.contextMenu(screen.getByTestId('workspace-tab-pin1'))
    expect(screen.getByTestId('workspace-tab-menu-close')).toBeDisabled()
  })

  it('空态导航页声明条目可点击（落到工作区面板打开链路）', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    const { useLayoutModeStore } = await import('@/stores/layoutModeStore')
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
    contributionRegistry.register({
      type: 'pages', id: 'tasks', title: '任务管理',
      space: 'workspace', slot: 'tab', path: '/tasks', pluginId: 'task_service',
    })
    render(
      <WorkspacePanel
        tabs={[]}
        activeTabId={null}
        onTabChange={vi.fn()}
        onTabClose={vi.fn()}
        renderTabContent={() => null}
      />,
    )

    fireEvent.click(screen.getByTestId('nav-item-task_service:tasks'))
    // 入口确实驱动面板打开链路（真实依赖，非静默 no-op）
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-tasks'])
    expect(tabs[0]?.isActive).toBe(true)
    contributionRegistry.clear()
  })

  it('滚轮纵向 delta：横向滚动标签条（非被动绑定使 preventDefault 生效）', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    render(
      <WorkspacePanel
        tabs={[
          { id: 't1', title: '标签一', isActive: true, isPinned: false, moduleId: 'm', icon: '' },
        ] as never}
        activeTabId="t1"
        onTabChange={vi.fn()}
        onTabClose={vi.fn()}
        renderTabContent={() => <div>内容</div>}
      />,
    )

    const scrollEl = screen.getByRole('tablist') as HTMLElement
    // 纵向滚轮转横向：scrollLeft 按 deltaY 推进，且事件被 preventDefault（非被动绑定生效）
    const wheelEvent = new WheelEvent('wheel', { deltaY: 120, deltaX: 0, bubbles: true, cancelable: true })
    scrollEl.dispatchEvent(wheelEvent)

    expect(scrollEl.scrollLeft).toBe(120)
    expect(wheelEvent.defaultPrevented).toBe(true)
  })

  it('滚轮横向 delta 优先：不拦截且不改动 scrollLeft（纵向才转横向）', async () => {
    const { WorkspacePanel } = await import('@/components/layout/WorkspacePanel')
    render(
      <WorkspacePanel
        tabs={[
          { id: 't1', title: '标签一', isActive: true, isPinned: false, moduleId: 'm', icon: '' },
        ] as never}
        activeTabId="t1"
        onTabChange={vi.fn()}
        onTabClose={vi.fn()}
        renderTabContent={() => <div>内容</div>}
      />,
    )

    const scrollEl = screen.getByRole('tablist') as HTMLElement
    const wheelEvent = new WheelEvent('wheel', { deltaY: 10, deltaX: 200, bubbles: true, cancelable: true })
    scrollEl.dispatchEvent(wheelEvent)

    expect(scrollEl.scrollLeft).toBe(0)
    expect(wheelEvent.defaultPrevented).toBe(false)
  })
})

// 让 initializeWidgets（真实 widget 注册）在 SchemaFullscreenHost 用例前生效
beforeEach(() => {
  initializeWidgets()
})
