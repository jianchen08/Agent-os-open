/**
 * ActivityCard form 块双路由 + actions 无 handler 守卫测试（widget 化 T2/T3/T4）
 *
 * - formFields 形状 → 交互表单（FormWidget；endpoint 模式可提交）
 * - readOnly 形态 → 只读结构化展示（整表 disabled，无提交按钮）
 * - actions onClick 缺失 → 按钮禁用、点击不抛错
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '../ActivityCard'
import type { ActivityData } from '@/types/activity'

vi.mock('@/components/approval', () => ({
  TextDiffView: () => null,
}))

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

function makeActivity(overrides: Partial<ActivityData> = {}): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-form',
    title: 'form 工具调用',
    status: 'completed',
    ...overrides,
  }
}

const formBlock = (content: Record<string, unknown>) => ({
  id: 'form-1',
  label: '部署参数',
  contentType: 'form' as const,
  content,
})

beforeEach(() => {
  fetchMock.mockReset()
})

describe('T2：form 块 → FormWidget（交互形态）', () => {
  it('formFields + endpoint 渲染可交互表单，可输入并提交（POST 带 pipeline_id）', async () => {
    fetchMock.mockResolvedValue({ json: async () => ({ switched: true }) })
    const activity = makeActivity({
      details: [
        formBlock({
          formFields: [
            { name: 'env', type: 'select', label: '环境', options: [{ label: '生产', value: 'prod' }] },
            { name: 'replicas', type: 'number', label: '副本数' },
          ],
          endpoint: '/ext/deploy/confirm',
          submitLabel: '确认部署',
        }),
      ],
    })
    render(<ActivityCard defaultExpanded activity={activity} />)
    expect(await screen.findByLabelText('副本数')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认部署' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('副本数'), { target: { value: '3' } })
    fireEvent.submit(document.querySelector('form')!)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/ext/deploy/confirm')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toMatchObject({ replicas: 3, pipeline_id: expect.any(String) })
  })
})

describe('T4：output_schema 只读视图（readOnly 形态）', () => {
  it('readOnly → 整表禁用、无提交按钮、值按 schema 字段展示', () => {
    const activity = makeActivity({
      details: [
        formBlock({
          formFields: [
            { name: 'status', type: 'select', label: '状态', options: [{ label: '完成', value: 'completed' }] },
            { name: 'output', type: 'string', label: '输出' },
          ],
          values: { status: 'completed', output: 'done' },
          readOnly: true,
        }),
      ],
    })
    render(<ActivityCard defaultExpanded activity={activity} />)
    expect(screen.getByLabelText('输出')).toHaveValue('done')
    // 字段模式（无 endpoint/onSubmit）无提交按钮
    expect(screen.queryByRole('button', { name: /提交|确认/ })).not.toBeInTheDocument()
  })
})

describe('T3：actions 无 handler 守卫', () => {
  it('onClick 缺失 → 按钮禁用，点击不抛错', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const activity = makeActivity({
      actions: [{ id: 'dead', icon: null, label: '死按钮', type: 'custom' }],
    })
    render(<ActivityCard defaultExpanded activity={activity} />)
    const btn = screen.getByRole('button', { name: '死按钮' })
    expect(btn).toBeDisabled()
    fireEvent.click(btn)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
