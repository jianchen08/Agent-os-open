/**
 * Session 组件测试族共享脚手架（SessionList / SessionEditModal）。
 *
 * 只承载纯数据工厂与渲染/事件包装；vi.mock 声明因提升语义必须留在各测试
 * 文件原地，不入本模块。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { SessionEditModal } from '../SessionEditModal'
import type { Session } from '@/types'
import type { Mock } from 'vitest'

/** SessionList 行数据工厂：随机 id 保证行间无 id 冲突 */
export function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: `session-${Math.random().toString(36).slice(2, 9)}`,
    title: '测试会话',
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T12:00:00Z',
    messageCount: 5,
    starred: false,
    pinned: false,
    ...overrides,
  }
}

/** SessionList 全量回调桩：onDeleteSession 默认成功兑现 */
export function createSessionListCallbacks() {
  const callbacks = {
    onSessionClick: vi.fn(),
    onDeleteSession: vi.fn(),
    onEditSession: vi.fn(),
    onCopySession: vi.fn(),
    onStarSession: vi.fn(),
    onPinSession: vi.fn(),
  }
  callbacks.onDeleteSession.mockResolvedValue(undefined)
  return callbacks
}

/** Radix DropdownMenu 需要完整指针序列（pointerDown → pointerUp → click）才打开 */
export function openDropdownMenu(trigger: HTMLElement): void {
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
}

/** agents 查询桩的默认 Agent（active 灵汐） */
export function makeAgent(overrides: Record<string, unknown> = {}) {
  return {
    id: 'a-1',
    configId: 'agentos',
    name: '灵汐',
    status: 'active',
    ...overrides,
  }
}

/** 真实声明照抄 plugins/shared/pipeline/input/workspace_lifecycle/plugin.json */
export const REAL_WORKSPACE_LIFECYCLE_FIELDS = [
  {
    name: 'workspace',
    type: 'directory',
    label: '工作空间',
    x_metadata_key: 'workspace',
    x_execution_path: 'workspace.source_path',
    description: '主会话工作目录（项目路径），留空则使用自动生成的默认目录（拓扑固定 plain）。',
  },
  {
    name: 'workspaceMode',
    type: 'select',
    label: '工作空间拓扑',
    x_metadata_key: 'workspace_mode',
    x_execution_path: 'workspace.mode',
    options: [
      { label: 'plain（直接操作目标目录，默认）', value: 'plain' },
      { label: 'worktree（隔离副本，需先填写工作空间）', value: 'worktree' },
    ],
    description: '会话工作空间的拓扑形态，与隔离模式相互独立。',
    x_guard: { requires: 'workspace', on_empty: 'plain' },
  },
]

/** 真实声明照抄 plugins/shared/system/isolation/plugin.json */
export const REAL_ISOLATION_FIELDS = [
  {
    name: 'isolationMode',
    type: 'select',
    label: '隔离模式',
    x_metadata_key: 'isolation_mode',
    x_execution_path: 'isolation.level',
    options: [
      { label: '隔离（容器）', value: 'isolated' },
      { label: '非隔离', value: 'non_isolated' },
    ],
  },
]

/** 以 create 模式挂载 SessionEditModal，返回 onClose/onSave 桩 */
export function renderCreateModal() {
  const onClose = vi.fn()
  const onSave = vi.fn()
  const view = renderWithProviders(
    <SessionEditModal mode="create" isOpen session={null} onClose={onClose} onSave={onSave} />,
  )
  return { ...view, onClose, onSave }
}

/** antd Select 交互：展开下拉并返回可见选项容器。rc-select virtual 模式下
 *  role=option 落在 aria 镜像列表（点击无效），必须取可见的
 *  .ant-select-item-option；已选值标签与下拉项可能同文，故按容器过滤。 */
export async function openAntdSelectOption(label: RegExp, optionText: RegExp): Promise<HTMLElement> {
  const combo = await screen.findByRole('combobox', { name: label })
  fireEvent.mouseDown(combo.closest('.ant-select')!)
  const items = await screen.findAllByText(optionText)
  const target = items.find((el) => el.closest('.ant-select-item-option'))
  if (!target) throw new Error(`下拉未渲染可见选项: ${String(optionText)}`)
  return target.closest('.ant-select-item-option')!
}

/** 点「创建」提交并等待 onSave 兑现，返回首个调用的第 4 参（options 产物） */
export async function submitCreate(onSave: Mock) {
  fireEvent.click(screen.getByRole('button', { name: /创建/ }))
  await waitFor(() => expect(onSave).toHaveBeenCalled())
  return onSave.mock.calls[0][3]
}
