/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * PromptPreviewModal 复制入口补测：点「复制系统提示词」→ 写入剪贴板并给出
 * 已复制反馈（按钮 title 切换），随后自动复位。
 *
 * mock 边界：剪贴板（浏览器 API）、预览 API（HTTP）。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as agentsApi from '@/services/api/agents'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PromptPreviewModal } from '../PromptPreviewModal'

const getPreviewMock = vi.mocked(agentsApi.getAgentPromptPreview)

vi.mock('@/services/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/api/agents')>()),
  getAgents: vi.fn(),
  getAgentPromptPreview: vi.fn(),
}))

const writeTextMock = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  writeTextMock.mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: writeTextMock },
    configurable: true,
  })
  getPreviewMock.mockResolvedValue({
    system_message: '你是组装后的提示词',
    dynamic_vars_message: null,
    agent_id: 'a1',
  })
})

describe('PromptPreviewModal — 复制系统提示词', () => {
  it('点击复制 → 写入剪贴板并切换为「已复制」反馈', async () => {
    renderWithProviders(
      <PromptPreviewModal agent={{ id: 'a1', name: '预览员' }} isOpen onClose={() => {}} />,
    )
    await waitFor(() => expect(screen.getByText('你是组装后的提示词')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: '复制系统提示词' }))

    expect(writeTextMock).toHaveBeenCalledWith('你是组装后的提示词')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '复制系统提示词' })).toHaveAttribute(
        'title',
        '已复制',
      ),
    )
  })

  it('未取得预览时不写剪贴板（空载不产生副作用）', async () => {
    renderWithProviders(
      <PromptPreviewModal agent={{ id: 'a1' }} isOpen={false} onClose={() => {}} />,
    )

    expect(writeTextMock).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
