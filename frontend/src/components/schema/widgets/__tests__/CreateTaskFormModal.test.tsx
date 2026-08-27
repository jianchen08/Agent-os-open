/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * CreateTaskFormModal（widget 化 T12）测试。
 *
 * 焦点：字段声明已后移 task_form 服务插件——本组件把 fieldsUri 交给 FormWidget
 * （= /ext/task_form/form，动态选项由字段 datasourceUri 自内核取），
 * 自身只保留 createRootTask 提交派生（project_id 可选挂靠，任务必选执行 Agent）。
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

describe('CreateTaskFormModal：字段声明后移 + 提交派生', () => {
  it('fieldsUri 指向 task_form 服务', () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    expect(h.formProps.fieldsUri).toBe('/ext/task_form/form')
    expect(h.formProps.submitLabel).toBe('创建')
  })

  it('独立任务：target_id 透传，不带 project_id', async () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    await submit({
      title: '写周报',
      description: 'd',
      project_id: '',
      target_id: 'general_agent',
      workspace_mode: '',
      isolation_level: '',
    })
    expect(createRootMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '写周报',
        target_id: 'general_agent',
        thread_id: 'sess_1',
        project_id: undefined,
      }),
    )
  })

  it('挂靠项目：project_id 透传', async () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    await submit({
      title: '项目任务',
      project_id: 'aabbccddeeff',
      target_id: 'general_agent',
      workspace_mode: 'worktree',
      isolation_level: '',
    })
    expect(createRootMock).toHaveBeenCalledWith(
      expect.objectContaining({
        project_id: 'aabbccddeeff',
        workspace_mode: 'worktree',
      }),
    )
  })

  it('缺 target_id → 拒绝提交', async () => {
    render(
      <CreateTaskFormModal isOpen onClose={() => {}} sessionId="sess_1" onCreated={() => {}} />,
    )
    await expect(
      submit({ title: 'x', target_id: '  ' }),
    ).rejects.toThrow('必须选择执行 Agent')
    expect(createRootMock).not.toHaveBeenCalled()
  })
})
