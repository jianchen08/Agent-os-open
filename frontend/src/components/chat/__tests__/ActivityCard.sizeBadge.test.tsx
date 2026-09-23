/** @feature: FP-0.2.四 前端Schema(R174 产物文件卡 render 契约) | @ci: frontend-test */
/**
 * 功能测试：ActivityCard 头部大小徽标（file_card 的写后文件字节数）。
 *
 * 折叠 chip 形态契约：size 在场 → 头部展示 formatFileSize 徽标；
 * size 缺省 → 不渲染徽标节点（无占位噪音）。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ActivityCard from '@/components/chat/ActivityCard'
import type { ActivityData } from '@/types/activity'

function makeActivity(size?: number): ActivityData {
  return {
    type: 'tool_call',
    id: 'a1',
    toolName: 'file_write',
    title: 'calculator.html',
    status: 'completed',
    ...(size !== undefined ? { size } : {}),
  } as ActivityData
}

describe('ActivityCard 头部大小徽标（file_card）', () => {
  it('size 在场 → 头部渲染人类可读大小', () => {
    render(<ActivityCard activity={makeActivity(2389)} />)
    expect(screen.getByText('2.33 KB')).toBeInTheDocument()
  })

  it('size 为 0 → 渲染 0 B（字节数语义，不当作缺省）', () => {
    render(<ActivityCard activity={makeActivity(0)} />)
    expect(screen.getByText('0 B')).toBeInTheDocument()
  })

  it('size 缺省 → 无徽标（其它卡的头部不受影响）', () => {
    render(<ActivityCard activity={makeActivity()} />)
    expect(screen.queryByText(/B$/)).not.toBeInTheDocument()
  })
})
