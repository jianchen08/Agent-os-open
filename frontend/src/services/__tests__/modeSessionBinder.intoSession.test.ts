/** @feature FP-T12 前端适配(B8 管道标签) | @ci: frontend-test */
/**
 * openModeSession intoSession 分支（B8 管道标签）——端点代理/子 Tab/绑定/首条帧。
 * mock 仅外部依赖（apiClient/Tab store/WS/registry）；快照与绑定全真实。
 */
import { vi } from 'vitest'
const { openSubTabMock, sendUserInputMock, apiGetMock, apiPostMock } = vi.hoisted(() => ({
  openSubTabMock: vi.fn(),
  sendUserInputMock: vi.fn(),
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
}))
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: { getState: () => ({ openSubAgentTab: openSubTabMock }) },
}))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { sendUserInput: sendUserInputMock },
}))
vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: (...args: unknown[]) => apiGetMock(...args),
    post: (...args: unknown[]) => apiPostMock(...args),
  },
}))
vi.mock('@/stores/notificationStore', () => ({
  useNotificationStore: { getState: () => ({ addNotification: vi.fn() }) },
}))
vi.mock('@/services/workspacePanelOpener', () => ({
  openPluginPage: vi.fn(),
}))

import { openModeSession } from '../modeSessionBinder'
import { loadPipelineBinding } from '../pipelineExecutionOptions'

/** registry 声明（roleplay：对话链管道 + persona 接管） */
const ROLEPLAY_DECL = {
  mode: 'roleplay',
  name: '角色扮演模式',
  pipelines: [{ name: 'roleplay', context: 'conversation' }],
  presenter: { source: 'data_cards' },
  tool_card: 'collapse',
  plugin_id: 'mode_roleplay',
  persona: { replace: true, from: 'roleplay_persona' },
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  apiGetMock.mockResolvedValue({
    data: { modes: [ROLEPLAY_DECL], total: 1, errors: [] },
  })
})

describe('openModeSession intoSession — B8 管道标签', () => {
  it('端点代理开通 → 子 Tab 激活 + 管道级绑定落盘 + 首条帧带新管道', async () => {
    apiPostMock.mockResolvedValue({
      data: { pipeline_id: 'pipe-new', thread_id: 'th-main', pipeline_config_id: 'roleplay' },
    })

    const receipt = await openModeSession('roleplay', {
      intoSessionId: 'th-main',
      agentId: 'mode_roleplay/luna',
      agentName: '月见',
      firstMessage: '（开始）',
    })

    // 端点调用参数（模式插件自己的接口）
    expect(apiPostMock).toHaveBeenCalledWith(
      '/ext/mode_roleplay/pipeline/open',
      expect.objectContaining({
        session_id: 'th-main',
        mode: 'roleplay',
        agent_id: 'mode_roleplay/luna',
        first_message: '（开始）',
      }),
    )
    // 子 Tab 新增并激活（主 Tab 不动）
    expect(openSubTabMock).toHaveBeenCalledWith(
      expect.objectContaining({
        agentName: '月见',
        pipelineId: 'pipe-new',
        parentRecordId: 'th-main',
        setActive: true,
      }),
    )
    // 管道级绑定落盘（发送链优先序来源）
    expect(loadPipelineBinding('pipe-new')).toEqual({
      mode: 'roleplay',
      pipelineConfigId: 'roleplay',
      agentId: 'mode_roleplay/luna',
    })
    // 首条帧投给新管道（agent_id/pipeline_config_id 帧参数同口径）
    expect(sendUserInputMock).toHaveBeenCalledWith(
      'th-main',
      '（开始）',
      expect.objectContaining({
        pipelineId: 'pipe-new',
        agentId: 'mode_roleplay/luna',
        pipelineConfigId: 'roleplay',
      }),
    )
    expect(receipt.sessionId).toBe('th-main')
  })

  it('既有会话快照不被改写（出生语义不动：会话键无 modeBinding 写入）', async () => {
    localStorage.setItem(
      'session-exec-options:th-main',
      JSON.stringify({ values: {}, agentId: 'agentos' }),
    )
    apiPostMock.mockResolvedValue({
      data: { pipeline_id: 'pipe-x', thread_id: 'th-main', pipeline_config_id: 'roleplay' },
    })
    await openModeSession('roleplay', { intoSessionId: 'th-main' })
    const snap = JSON.parse(localStorage.getItem('session-exec-options:th-main') ?? '{}')
    expect(snap.agentId).toBe('agentos')
    expect(snap.modeBinding).toBeUndefined()
  })

  it('端点失败 → 异常传播（零 Tab 零绑定）', async () => {
    apiPostMock.mockRejectedValue(new Error('端点 502'))
    await expect(
      openModeSession('roleplay', { intoSessionId: 'th-main' }),
    ).rejects.toThrow('端点 502')
    expect(openSubTabMock).not.toHaveBeenCalled()
  })
})
