/**
 * ActivityCard 渲染路由器新增块（table/form）渲染验证
 *
 * 覆盖：渲染路由器（dshRenderIntent）产出的 table 卡（表头+二维数组）与
 * form 卡（kv 标量 + 长文本/对象折叠区）落到 ActivityCard 的 DOM。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '../ActivityCard'
import type { ActivityData } from '@/types/activity'

vi.mock('@/components/approval', () => ({
  TextDiffView: () => null,
}))

vi.mock('@/components/chat/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))

vi.mock('@/utils/toolCardRegistry', () => ({
  getGlobalOpenFileCallback: () => () => {},
}))

function makeActivity(overrides: Partial<ActivityData> = {}): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-1',
    title: 'table 工具调用',
    status: 'completed',
    ...overrides,
  }
}

describe('AC-table块: 渲染路由器 table 卡（表头 + 二维数组行）', () => {
  beforeEach(() => vi.clearAllMocks())

  it('表头与每行单元格渲染（含空单元格兜底）', () => {
    render(
      <ActivityCard
        defaultExpanded
        activity={makeActivity({
          details: [
            {
              id: 't1',
              label: '资源列表',
              contentType: 'table',
              table: {
                columns: ['ID', '名称', '状态'],
                rows: [
                  ['a-1', '运维', 'running'],
                  ['a-2', '开发', ''],
                ],
              },
            },
          ],
        })}
      />,
    )

    expect(screen.getByText('资源列表')).toBeInTheDocument()
    expect(screen.getByText('ID')).toBeInTheDocument()
    expect(screen.getByText('名称')).toBeInTheDocument()
    expect(screen.getByText('状态')).toBeInTheDocument()
    expect(screen.getByText('a-1')).toBeInTheDocument()
    expect(screen.getByText('运维')).toBeInTheDocument()
    expect(screen.getByText('a-2')).toBeInTheDocument()
  })
})

describe('AC-form块: 渲染路由器 form 卡（kv 标量 + 长文本折叠区）', () => {
  beforeEach(() => vi.clearAllMocks())

  it('kv 标量直接可见，长文本折叠区点击展开', () => {
    render(
      <ActivityCard
        defaultExpanded
        activity={makeActivity({
          title: 'task_submit 工具调用',
          details: [
            {
              id: 'f1',
              label: '任务详情',
              contentType: 'form',
              kvItems: [
                { key: '任务ID', value: 't-123' },
                { key: '状态', value: 'running' },
              ],
              jsonItems: [{ label: '验收标准', content: { file_check: { path: 'src/a.py' } } }],
            },
          ],
        })}
      />,
    )

    expect(screen.getByText('任务ID')).toBeInTheDocument()
    expect(screen.getByText('t-123')).toBeInTheDocument()
    expect(screen.getByText('状态')).toBeInTheDocument()
    // 折叠区默认收起：内容不可见，点击后展开
    expect(screen.queryByText(/"file_check"/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('验收标准'))
    expect(screen.getByText(/"file_check"/)).toBeInTheDocument()
  })

  it('空 kv / 空 jsonItems 不崩渲染', () => {
    render(
      <ActivityCard
        defaultExpanded
        activity={makeActivity({
          details: [
            { id: 'f2', label: '空表单', contentType: 'form', kvItems: [], jsonItems: [] },
          ],
        })}
      />,
    )
    expect(screen.getByText('空表单')).toBeInTheDocument()
  })
})
