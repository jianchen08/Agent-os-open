/**
 * DebugSessionsPage / DebugExecutionRecordsPage 组件测试
 *
 * 验证调试中心两个页面能正确消费 executionRecords API 返回的数据并渲染表格。
 * 背景：后端 /execution/records* 端点从 stub 接到真实 storage 后，
 * 需确认前端组件的数据获取→状态更新→表格渲染链路完整。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'

// mock 整个 executionRecords API 模块
vi.mock('@/services/api/executionRecords', () => ({
  getExecutionRecordsSessions: vi.fn(),
  getExecutionRecords: vi.fn(),
}))

import { DebugSessionsPage } from '../DebugSessionsPage'
import { DebugExecutionRecordsPage } from '../DebugExecutionRecordsPage'
import * as api from '@/services/api/executionRecords'

const mockGetSessions = vi.mocked(api.getExecutionRecordsSessions)
const mockGetRecords = vi.mocked(api.getExecutionRecords)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DebugSessionsPage', () => {
  it('加载成功后渲染会话表格', async () => {
    mockGetSessions.mockResolvedValue({
      sessions: [
        {
          id: '504f14e3d403',
          title: '测试会话标题',
          created_at: '2026-07-01T10:00:00',
          updated_at: '2026-07-01T10:00:00',
          record_count: 42,
        },
      ],
      total: 1,
    })

    render(<DebugSessionsPage />)

    // 等待数据渲染
    await waitFor(() => {
      expect(screen.getByText('共 1 个会话')).toBeInTheDocument()
    })

    // 会话 ID 出现在表格中（移动端卡片 + 桌面端表格两处渲染）
    expect(screen.getAllByText('504f14e3d403').length).toBeGreaterThan(0)
    // 标题渲染
    expect(screen.getAllByText('测试会话标题').length).toBeGreaterThan(0)
    // 记录数渲染
    expect(screen.getAllByText('42').length).toBeGreaterThan(0)
    // 不再显示加载态
    expect(screen.queryByText('加载中...')).not.toBeInTheDocument()
    // 不显示空状态
    expect(screen.queryByText('暂无数据')).not.toBeInTheDocument()
  })

  it('加载失败时显示错误提示', async () => {
    mockGetSessions.mockRejectedValue(new Error('网络错误'))

    render(<DebugSessionsPage />)

    await waitFor(() => {
      expect(screen.getByText('网络错误')).toBeInTheDocument()
    })
  })

  it('无数据时显示空状态', async () => {
    mockGetSessions.mockResolvedValue({ sessions: [], total: 0 })

    render(<DebugSessionsPage />)

    await waitFor(() => {
      expect(screen.getByText('暂无数据')).toBeInTheDocument()
    })
  })
})

describe('DebugExecutionRecordsPage', () => {
  it('加载成功后渲染记录表格', async () => {
    mockGetSessions.mockResolvedValue({ sessions: [], total: 0 })
    mockGetRecords.mockResolvedValue({
      records: [
        {
          id: 'abc123def456',
          session_id: '504f14e3d403',
          record_type: 'ai',
          status: 'completed',
          depth: 3,
          sequence: 7,
          message_data: {},
          created_at: '2026-07-01T10:00:00',
        },
      ],
      total: 1,
    })

    render(<DebugExecutionRecordsPage />)

    await waitFor(() => {
      expect(screen.getByText('共 1 条')).toBeInTheDocument()
    })

    // 记录 ID 渲染（移动端卡片 + 桌面端表格两处）
    expect(screen.getAllByText('abc123def456').length).toBeGreaterThan(0)
    // 不显示空状态
    expect(screen.queryByText('暂无数据')).not.toBeInTheDocument()
  })
})
