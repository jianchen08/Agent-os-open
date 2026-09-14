/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * FileTreeWidget 残余分支补测（簇3，与 .render / .errorState / .branches 互补）
 *
 * 覆盖既有测试未触达的分支：
 * - loadExpandedIds 的非数组/损坏记录 → 回落 null（改用默认展开策略）
 * - getStableNodeId 的 id/path/title 全缺兜底（Math.random 生成临时 id）
 * - findChildrenById 未命中目标 → 返回 null（不误级联无关子树）
 * - formatTime 的 catch（值无法强制转换为日期 → 不渲染时间、不崩）
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - formatTime 的 catch（L1112）在正常数据下不可达：`new Date(<string>)` 对任意
 *   字符串都返回 Invalid Date 而非抛错（该情形由 isNaN 早退覆盖）。仅当
 *   created_at 携带无法强制转换的宿主对象（toString/valueOf 抛错）时才触达——
 *   属"后端载荷被污染"的防御分支。本文件以显式抛错的 toPrimitive 对象驱动，
 *   固化"污染载荷不得崩树"的契约。
 * - getStableNodeId 的 Math.random 兜底（L218）产生的是临时 id（每次调用不同），
 *   正常树数据不会走到；属防御分支，本文件以全字段缺失节点驱动。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args) },
}))
vi.mock('@/services/api/tasks', () => ({
  pauseTask: vi.fn().mockResolvedValue(undefined),
  resumeTask: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('@/services/schema/parser', () => ({
  parseDataSourceRef: (ref: string) => ({ endpoint: ref, params: {} }),
  resolveDataSource: (ref: { endpoint: string }) => ({ endpoint: ref.endpoint, params: {} }),
}))
const layoutState = vi.hoisted(() => ({
  workspaceTabs: [] as Array<{ id: string }>,
  setActiveTab: vi.fn(),
  addWorkspaceTab: vi.fn(),
}))
vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: { getState: () => layoutState, setState: vi.fn() },
}))
vi.mock('../CreateTaskFormModal', () => ({
  CreateTaskFormModal: () => null,
}))
vi.mock('../FileTreeContextMenu', () => ({
  FileTreeContextMenu: () => null,
}))

import { pauseTask } from '@/services/api/tasks'
import { FileTreeWidget } from '../FileTreeWidget'

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  layoutState.workspaceTabs = []
})

describe('展开状态持久化的异常记录', () => {
  it.each([
    ['合法 JSON 但非数组（对象）', '{"a":1}'],
    ['合法 JSON 但非数组（字符串）', '"oops"'],
    ['合法 JSON 但非数组（数字）', '42'],
    ['损坏的 JSON 文本', '{not json'],
  ])('localStorage 记录为%s → 忽略该记录并回落默认展开策略', async (_name, raw) => {
    localStorage.setItem('tree_expanded_default_异常记录树', raw)
    render(
      <FileTreeWidget
        title="异常记录树"
        expandLevel={-1}
        data={[
          {
            id: 'root',
            title: '根目录',
            status: 'running',
            children: [{ id: 'c', title: '子文档', status: 'running' }],
          },
        ]}
      />,
    )

    // 记录不可用 → 按默认策略（expandLevel=-1 全展开）渲染子节点
    expect(await screen.findByText('子文档')).toBeInTheDocument()
  })

  it('记录为合法数组 → 按其内容恢复折叠态（与异常记录区分）', async () => {
    localStorage.setItem('tree_expanded_default_数组记录树', '[]')
    render(
      <FileTreeWidget
        title="数组记录树"
        data={[
          {
            id: 'root',
            title: '根目录',
            status: 'running',
            children: [{ id: 'c', title: '子文档', status: 'running' }],
          },
        ]}
      />,
    )
    // 空数组 = 全部折叠：子节点按记录隐藏（性质与异常记录相反）
    expect(screen.queryByText('子文档')).not.toBeInTheDocument()
  })
})

describe('节点标识兜底', () => {
  it('节点带 path 无 id → 以 path 作为标识（稳定渲染，无重复告警）', async () => {
    render(
      <FileTreeWidget
        data={[
          { path: 'a/b.txt', title: '文件甲', status: 'running' },
          { path: 'a/c.txt', title: '文件乙', status: 'running' },
        ]}
      />,
    )
    expect(await screen.findByText('文件甲')).toBeInTheDocument()
    expect(screen.getByText('文件乙')).toBeInTheDocument()
  })

  it('节点仅有 name（无 id/path/title）→ name 作为标识并正常渲染', async () => {
    render(
      <FileTreeWidget
        data={[
          { name: '名称甲', status: 'running' },
          { name: '名称乙', status: 'running' },
        ]}
      />,
    )
    // name 不在默认 titleField 上 → 标题回落「未命名」，但节点仍渲染且不崩
    expect((await screen.findAllByText('未命名')).length).toBe(2)
  })

  it('节点四字段全缺 → 生成临时标识，渲染不崩（防御兜底）', async () => {
    const { container } = render(
      <FileTreeWidget data={[{ status: 'running' }, { status: 'running' }]} />,
    )
    await screen.findAllByText('未命名')

    // 两个无标识节点各自渲染出行（key 为临时随机串，无 React key 重复告警）
    const occurrences = (container.textContent ?? '').split('未命名').length - 1
    expect(occurrences).toBe(2)
  })

  it('无标识节点的开关切换 → 查子节点未命中返回空，不误级联他节点', async () => {
    const { container } = render(
      <FileTreeWidget
        showEnabledToggle
        data={[
          // 四字段全缺：getStableNodeId 每次调用生成新临时串，
          // 按该 id 查子节点遍历整树后未命中（返回 null）
          { status: 'running' },
          { id: 'keep', title: '有标识节点', status: 'running' },
        ]}
      />,
    )
    await screen.findByText('有标识节点')

    // 无标识节点渲染在前（无子节点者优先），其开关为第一个
    const toggles = Array.from(
      container.querySelectorAll('button[title*="点击"]'),
    ) as HTMLElement[]
    expect(toggles).toHaveLength(2)
    expect(toggles[0].parentElement?.parentElement?.textContent).toContain('未命名')

    // 查子节点未命中（返回 null）→ 无后代可级联，切换仅作用于自身
    const calledBefore = vi.mocked(pauseTask).mock.calls.length
    fireEvent.click(toggles[0])
    await waitFor(() =>
      expect(vi.mocked(pauseTask).mock.calls.length).toBeGreaterThan(calledBefore),
    )
    // 仅一次调用（无后代级联）
    expect(vi.mocked(pauseTask).mock.calls.length - calledBefore).toBe(1)
    // 另一节点不受影响（仍在渲染）
    expect(screen.getByText('有标识节点')).toBeInTheDocument()
  })
})

describe('创建时间格式化容错', () => {
  // 时间行仅在节点有 error（hasMeta）时随元信息行渲染——这是现状契约
  const errorNode = (over: Record<string, unknown>) => ({
    id: 'n1',
    title: '时间节点',
    status: 'running',
    error: '任务失败',
    ...over,
  })

  it.each([
    ['合法 ISO 时间', '2026-03-05T08:07:00', '03-05 08:07'],
    ['仅日期', '2026-12-31', '12-31'],
  ])('%s → 渲染 MM-DD 形态时间', async (_name, createdAt, expectedText) => {
    const { container } = render(<FileTreeWidget data={[errorNode({ created_at: createdAt })]} />)
    await screen.findByText('时间节点')

    expect(container.textContent ?? '').toContain(expectedText)
  })

  it.each([
    ['非法日期串', 'not-a-date'],
    ['空串', ''],
    ['null', null],
  ])('创建时间为%s → 不渲染时间（不崩）', async (_name, createdAt) => {
    const { container } = render(<FileTreeWidget data={[errorNode({ created_at: createdAt })]} />)
    await screen.findByText('时间节点')

    expect(container.textContent ?? '').not.toMatch(/\d{2}-\d{2} \d{2}:\d{2}/)
  })

  it('创建时间值无法强制转换（toString 抛错）→ 捕获后不渲染时间，树不崩', async () => {
    const hostile = {
      toString() {
        throw new Error('malformed payload')
      },
      valueOf() {
        return this
      },
    }
    const { container } = render(<FileTreeWidget data={[errorNode({ created_at: hostile })]} />)

    // 树整体仍正常渲染（catch 把污染值降级为"无时间"）
    await screen.findByText('时间节点')
    expect(container.textContent ?? '').not.toMatch(/\d{2}-\d{2} \d{2}:\d{2}/)
    // 错误文案照常展示（元信息行未被异常吞掉）
    expect(screen.getByText(/任务失败/)).toBeInTheDocument()
  })
})
