/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * CreateTaskFormModal（widget 化 T12）测试。
 *
 * 焦点：字段声明已后移 task_form 服务插件——本组件把 fieldsUri 交给 FormWidget
 * （= /ext/task_form/form?session_id=...，动态选项由字段 datasourceUri 自内核取），
 * 自身只保留 createRootTask 提交矩阵派生（父容器→子任务、容器任务无 agent/拓扑/隔离）。
 */
import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { CreateTaskFormModal } from '../CreateTaskFormModal'
import { createRootTask } from '@/services/api/tasks'

vi.mock('@/services/api/tasks', () => ({
  createRootTask: vi.fn(),
}))

vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

const h = vi.hoisted(() => ({ formProps: {} as Record<string, unknown> }))
vi.mock('../FormWidget', () => ({
  FormWidget: (props: Record<string, unknown>) => {
    h.formProps = props
    return null
  },
}))

const createRootMock = vi.mocked(createRootTask)

beforeEach(() => {
  h.formProps = {}
  createRootMock.mockReset()
  createRootMock.mockResolvedValue(undefined as never)
})

async function submit(values: Record<string, unknown>): Promise<void> {
  const handler = h.formProps.onSubmit as (v: Record<string, unknown>) => Promise<void>
  await handler(values)
}

describe('CreateTaskFormModal：字段声明后移 + 提交矩阵', () => {
  it('fieldsUri 指向 task_form 服务（session 内嵌）', () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    expect(h.formProps.fieldsUri).toBe('/ext/task_form/form?session_id=sess_1')
    expect(h.formProps.submitLabel).toBe('创建')
  })

  it('无 session 时仍拉声明（不回退硬编码）', () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="" onCreated={() => {}} />,
    )
    expect(h.formProps.fieldsUri).toBe('/ext/task_form/form')
  })

  it('非容器根任务：target_id 透传', async () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    await submit({
      title: '写周报',
      description: 'd',
      task_scope: 'non_container',
      target_id: 'general_agent',
      workspace_mode: '',
      isolation_level: '',
    })
    expect(createRootMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '写周报',
        task_scope: 'non_container',
        target_id: 'general_agent',
        thread_id: 'sess_1',
        parent_task_id: undefined,
      }),
    )
  })

  it('容器任务：无 target_id / 无拓扑 / 无隔离', async () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    await submit({
      title: '建容器',
      task_scope: 'container',
      workspace_mode: 'plain',
      isolation_level: 'isolated',
    })
    expect(createRootMock).toHaveBeenCalledWith(
      expect.objectContaining({
        task_scope: 'container',
        target_id: '',
        workspace_mode: '',
        isolation_level: '',
      }),
    )
  })

  it('选了父容器 → 强制非容器子任务', async () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    await submit({
      title: '子任务',
      task_scope: 'container',
      parent_task_id: 'container_1',
      target_id: 'general_agent',
    })
    expect(createRootMock).toHaveBeenCalledWith(
      expect.objectContaining({
        task_scope: 'non_container',
        parent_task_id: 'container_1',
      }),
    )
  })

  it('非容器缺 target_id → 拒绝提交', async () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    await expect(
      submit({ title: 'x', task_scope: 'non_container', target_id: '  ' }),
    ).rejects.toThrow('非容器任务必须选择执行 Agent')
    expect(createRootMock).not.toHaveBeenCalled()
  })
})
