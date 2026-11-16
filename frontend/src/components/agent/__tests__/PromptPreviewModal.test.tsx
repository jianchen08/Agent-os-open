/** @feature: FP-0.2.提示词预览 | @ci: frontend-test
 *
 * PromptPreviewModal 组件测试 + AgentManagerPage「查看提示词」入口集成
 *
 * 契约：开态拉取组装预览并呈现 system_message / 动态变量消息；
 * 关态零渲染零请求；失败呈现错误 + 重试；页面眼睛按钮打开预览。
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as agentsApi from '@/services/api/agents'
import { renderWithProviders } from '@/test/renderWithProviders'
import { AgentManagerPage } from '../AgentManagerPage'
import { PromptPreviewModal } from '../PromptPreviewModal'

const getPreviewMock = vi.mocked(agentsApi.getAgentPromptPreview)
const getAgentsMock = vi.mocked(agentsApi.getAgents)

vi.mock('@/services/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/api/agents')>()),
  getAgents: vi.fn(),
  getAgentPromptPreview: vi.fn(),
}))

describe('PromptPreviewModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('关态零渲染零请求', () => {
    renderWithProviders(
      <PromptPreviewModal agent={null} isOpen={false} onClose={() => {}} />,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(getPreviewMock).not.toHaveBeenCalled()
  })

  it('开态拉取并呈现 system_message 与动态变量消息', async () => {
    getPreviewMock.mockResolvedValue({
      system_message: '你是组装后的提示词',
      dynamic_vars_message: { role: 'user', content: '- 会话: preview' },
      agent_id: 'a1',
    })
    renderWithProviders(
      <PromptPreviewModal agent={{ id: 'a1', name: '预览员' }} isOpen onClose={() => {}} />,
    )
    await waitFor(() => expect(screen.getByText('你是组装后的提示词')).toBeInTheDocument())
    expect(screen.getByText(/动态变量消息/)).toBeInTheDocument()
    expect(screen.getByText(/- 会话: preview/)).toBeInTheDocument()
  })

  it('无动态变量时不渲染动态变量节', async () => {
    getPreviewMock.mockResolvedValue({
      system_message: '只有主消息',
      dynamic_vars_message: null,
      agent_id: 'a1',
    })
    renderWithProviders(<PromptPreviewModal agent={{ id: 'a1' }} isOpen onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('只有主消息')).toBeInTheDocument())
    expect(screen.queryByText(/动态变量消息/)).not.toBeInTheDocument()
  })

  it('失败呈现错误与重试，重试重新拉取', async () => {
    getPreviewMock.mockRejectedValueOnce(new Error('502 unavailable'))
    renderWithProviders(<PromptPreviewModal agent={{ id: 'a1' }} isOpen onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText(/获取组装提示词失败/)).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(getPreviewMock).toHaveBeenCalledTimes(2)
  })
})

describe('AgentManagerPage — 查看提示词入口', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('点击眼睛按钮打开预览并加载组装结果', async () => {
    getAgentsMock.mockResolvedValue({
      items: [
        { id: 'a1', name: '预览员', agent_type: 'main', status: 'active', model: 'glm' },
      ],
      total: 1,
    })
    getPreviewMock.mockResolvedValue({
      system_message: '页面集成组装结果',
      dynamic_vars_message: null,
      agent_id: 'a1',
    })
    renderWithProviders(<AgentManagerPage />)
    await waitFor(() => expect(screen.getByText('预览员')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: '查看提示词 预览员' }))
    await waitFor(() => expect(screen.getByText('页面集成组装结果')).toBeInTheDocument())
  })
})
