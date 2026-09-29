/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modeSessionBinder 服务测试 — 模式会话出生通道（选择器/面板桥共用，批 G④）
 *
 * 核验：
 * - openModeSession：store.createSession 建会话（标题=registry 模式名）→ 该会话
 *   执行选项写入 modeBinding（出生解析一次：mode + 对话链条目管道名）+ 扩展位
 *   （agentId/extraContext 快照）→ D8 人设接管提示（persona.replace 声明两态）
 *   → 面板页声明随开（声明驱动）→ 可选首条触发消息（帧形态与发送链一致）；
 * - 降级：registry 不可达 → 会话照常创建、modeBinding 仅 mode 键、无提示；
 * - 默认出生（mode 空串）→ 无 modeBinding；
 * - readSessionAgentBinding / clearSessionAgentBinding：读出回退尾段名、清理只动
 *   绑定键。
 *
 * mock 仅外部依赖：sessionListStore（会话创建走内核 API 网络）、globalWS
 * （WS 帧出站）、apiClient（modes registry 声明拉取）、openPluginPage（工作区
 * 面板导航副作用）；快照存取走真实 localStorage（jsdom 内建）。
 */
import { vi } from 'vitest'
const { createSessionMock, sendUserInputMock, openPanelMock } = vi.hoisted(() => ({
  createSessionMock: vi.fn(),
  sendUserInputMock: vi.fn(),
  openPanelMock: vi.fn(),
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: { getState: () => ({ createSession: createSessionMock }) },
}))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { sendUserInput: sendUserInputMock },
}))
vi.mock('@/services/workspacePanelOpener', () => ({
  openPluginPage: openPanelMock,
}))
const apiGetMock = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({
  apiClient: { get: (...args: unknown[]) => apiGetMock(...args) },
}))
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import {
  clearSessionAgentBinding,
  ensureModeTheme,
  openModeSession,
  readSessionAgentBinding,
  syncModeThemeForSession,
} from '../modeSessionBinder'
import { loadSessionExecutionOptions } from '../sessionExecutionOptions'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionThemeStore } from '@/stores/sessionThemeStore'
import { queryClient } from '@/services/query/queryClient'
import type { ModesRegistryResponse } from '@/services/api/modes'

/** 合成 registry：roleplay（persona 接管+对话链+面板页+theme 未声明）与 writing（零声明形态） */
function registryOf(modes: ModesRegistryResponse['modes']): void {
  apiGetMock.mockImplementation((url: string) =>
    url === '/ext/agent_manager/modes'
      ? Promise.resolve({ data: { modes, total: modes.length, errors: [] } })
      : Promise.reject(new Error(`unexpected get: ${url}`)),
  )
}

const ROLEPLAY_DECL = {
  mode: 'roleplay',
  name: '角色扮演模式',
  description: '卡驱动对话',
  pipelines: [{ name: 'roleplay', context: 'conversation' as const }],
  presenter: { source: 'data_cards' as const },
  tool_card: 'collapse' as const,
  icon: '🎭',
  theme: null,
  persona: { replace: true, from: 'roleplay_persona' },
  plugin_id: 'mode_roleplay',
}

const WRITING_DECL = {
  mode: 'writing',
  name: '写作模式',
  pipelines: [],
  presenter: { source: 'none' as const },
  tool_card: 'native' as const,
  icon: '✍️',
  theme: null,
  persona: null,
  plugin_id: 'mode_writing',
}

/** 种子 roleplay 面板页声明（getModePanelTarget 声明驱动面） */
function seedRoleplayPanelPage(): void {
  contributionRegistry.loadFromSchema({
    plugin_contributes: [
      {
        plugin_id: 'mode_roleplay',
        plugin_name: 'ModeRoleplay',
        contributes: {
          pages: [
            {
              id: 'roleplay_studio',
              title: '角色扮演工作台',
              space: 'workspace',
              slot: 'tab',
              widget: 'webview',
              mode: 'roleplay',
            },
          ],
        },
      },
    ],
    plugin_configs: [],
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  queryClient.clear()
  contributionRegistry.clear()
  seedRoleplayPanelPage()
  useNotificationStore.setState({ notifications: [] })
  useSessionThemeStore.setState({ stacks: {} })
  registryOf([ROLEPLAY_DECL, WRITING_DECL])
  createSessionMock.mockImplementation(async (title?: string) => ({
    id: `th-${title ?? 'default'}`,
    title,
    pipelineIds: [`pipe-${title ?? 'default'}`],
  }))
})

describe('openModeSession — 建会话 + modeBinding 出生 + D8 提示 + 面板随开', () => {
  it('模式出生：标题=registry 模式名、快照写 modeBinding（mode+对话链管道）', async () => {
    const receipt = await openModeSession('roleplay')

    expect(createSessionMock).toHaveBeenCalledWith('角色扮演模式')
    expect(receipt).toEqual({
      sessionId: 'th-角色扮演模式',
      modeBinding: { mode: 'roleplay', pipelineConfigId: 'roleplay' },
    })
    expect(loadSessionExecutionOptions('th-角色扮演模式')).toMatchObject({
      values: {},
      modeBinding: { mode: 'roleplay', pipelineConfigId: 'roleplay' },
    })
    // 面板页声明随开（声明驱动）
    expect(openPanelMock).toHaveBeenCalledTimes(1)
  })

  it('D8 会话侧提示两态：persona.replace 声明 → 提示接管人设缓存代价；未声明零提示', async () => {
    // 通知中心按 title+message 指纹短窗去重——D8 断言用独立合成模式，
    // 不与首例 roleplay 出生通知撞指纹
    registryOf([
      {
        mode: 'groupchat',
        name: '群聊模式',
        pipelines: [{ name: 'groupchat', context: 'conversation' as const }],
        presenter: { source: 'none' as const },
        tool_card: 'native' as const,
        icon: null,
        theme: null,
        persona: { replace: true, from: 'groupchat_persona' },
        plugin_id: 'mode_groupchat',
      },
      WRITING_DECL,
    ])
    await openModeSession('groupchat')
    const notified = useNotificationStore.getState().notifications
    expect(notified).toHaveLength(1)
    expect(notified[0].title).toContain('群聊模式')
    expect(notified[0].message).toContain('接管人设')
    expect(notified[0].message).toContain('缓存命中')

    useNotificationStore.setState({ notifications: [] })
    await openModeSession('writing')
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    // 零面板声明模式：面板不随开
    expect(openPanelMock).toHaveBeenCalledTimes(0)
  })

  it('多管声明取对话链条目（非首项也命中），任务链不参与会话路由', async () => {
    registryOf([
      {
        ...ROLEPLAY_DECL,
        pipelines: [
          { name: 'roleplay_tasks', context: 'task' as const },
          { name: 'roleplay', context: 'conversation' as const },
        ],
      },
    ])
    const receipt = await openModeSession('roleplay')
    expect(receipt.modeBinding).toEqual({ mode: 'roleplay', pipelineConfigId: 'roleplay' })
  })

  it('registry 不可达 → 降级：会话照常创建、modeBinding 仅 mode 键、无提示无管道', async () => {
    apiGetMock.mockRejectedValue(new Error('registry down'))

    const receipt = await openModeSession('roleplay')

    expect(receipt.sessionId).toBe('th-roleplay') // 标题回退 mode 键
    expect(receipt.modeBinding).toEqual({ mode: 'roleplay' })
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    expect(sendUserInputMock).not.toHaveBeenCalled()
  })

  it('扩展位：agentId/agentName/extraContext 落快照 + 首条触发消息帧形态', async () => {
    await openModeSession('roleplay', {
      agentId: 'mode_roleplay/card_luna',
      agentName: '月见',
      title: '扮演·月见',
      extraContext: { roleplay_greeting: '「欢迎光临！」' },
      firstMessage: '（开始）',
    })

    expect(createSessionMock).toHaveBeenCalledWith('扮演·月见')
    expect(loadSessionExecutionOptions('th-扮演·月见')).toMatchObject({
      agentId: 'mode_roleplay/card_luna',
      agentName: '月见',
      extraContext: { roleplay_greeting: '「欢迎光临！」' },
      modeBinding: { mode: 'roleplay', pipelineConfigId: 'roleplay' },
    })
    expect(sendUserInputMock).toHaveBeenCalledTimes(1)
    const [threadId, content, opts] = sendUserInputMock.mock.calls[0]
    expect(threadId).toBe('th-扮演·月见')
    expect(content).toBe('（开始）')
    expect(opts.pipelineId).toBe('pipe-扮演·月见')
    expect(opts.agentId).toBe('mode_roleplay/card_luna')
    expect(opts.pipelineConfigId).toBe('roleplay')
    expect(opts.clientMessageId).toBeTruthy()
    expect(opts.executionContext).toEqual({ mode: 'roleplay', roleplay_greeting: '「欢迎光临！」' })
  })

  it('默认出生（mode 空串）→ 无 modeBinding、无提示、宿主默认标题', async () => {
    const receipt = await openModeSession('')

    expect(receipt.modeBinding).toBeUndefined()
    expect(createSessionMock).toHaveBeenCalledWith(undefined)
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    expect(openPanelMock).not.toHaveBeenCalled()
  })

  it('快照写入不覆盖既有会话键（重绑极端路径保留 values/executionContext）', async () => {
    localStorage.setItem(
      'session-exec-options:th-角色扮演模式',
      JSON.stringify({
        values: { conversation_mode: 'plan' },
        executionContext: { isolation: { level: 'high' } },
      }),
    )
    await openModeSession('roleplay')
    expect(loadSessionExecutionOptions('th-角色扮演模式')).toMatchObject({
      values: { conversation_mode: 'plan' },
      executionContext: { isolation: { level: 'high' } },
      modeBinding: { mode: 'roleplay', pipelineConfigId: 'roleplay' },
    })
  })

  it('创建失败（网络）→ 异常向上传播（零发送零快照）', async () => {
    createSessionMock.mockRejectedValue(new Error('创建失败'))
    await expect(openModeSession('roleplay')).rejects.toThrow('创建失败')
    expect(sendUserInputMock).not.toHaveBeenCalled()
  })
})

describe('模式主题通道（批 G⑤：decl.theme → 会话主题 override）', () => {
  /** 合成带 theme 声明的模式（icon/theme 两字段齐备——注册面合成口径） */
  const THEMED_DECL = {
    ...WRITING_DECL,
    mode: 'immersive',
    name: '沉浸模式',
    theme: 'deep-space',
  }

  it('出生两态：声明 theme（预设 id）→ 会话栈压入该档；未声明零动作', async () => {
    registryOf([THEMED_DECL, WRITING_DECL])
    const themed = await openModeSession('immersive')
    expect(useSessionThemeStore.getState().stacks[themed.sessionId]).toHaveLength(1)
    expect(useSessionThemeStore.getState().stacks[themed.sessionId]?.[0].id).toBe('deep-space')

    const plain = await openModeSession('writing')
    expect(useSessionThemeStore.getState().stacks[plain.sessionId]).toBeUndefined()
  })

  it('theme id 非预设（不可解析）→ 诚实降级零动作', async () => {
    registryOf([{ ...THEMED_DECL, theme: 'no_such_theme' }])
    const created = await openModeSession('immersive')
    expect(useSessionThemeStore.getState().stacks[created.sessionId]).toBeUndefined()
  })

  it('ensureModeTheme 幂等：同 id 已在栈不重压（面板 theme.apply 档不被顶掉）', () => {
    expect(ensureModeTheme('th-t', 'deep-space')).toBe(true)
    expect(ensureModeTheme('th-t', 'deep-space')).toBe(false)
    expect(useSessionThemeStore.getState().stacks['th-t']).toHaveLength(1)
  })

  it('syncModeThemeForSession：快照 modeBinding + registry 声明 → 补同步；无 mode/registry 不可达零动作', async () => {
    registryOf([THEMED_DECL, WRITING_DECL])
    // 无快照 → 零动作
    await syncModeThemeForSession('th-none')
    expect(useSessionThemeStore.getState().stacks['th-none']).toBeUndefined()
    // 快照有 modeBinding 但 registry 不可达 → 零动作
    localStorage.setItem(
      'session-exec-options:th-t',
      JSON.stringify({ values: {}, modeBinding: { mode: 'immersive' } }),
    )
    apiGetMock.mockRejectedValue(new Error('registry down'))
    await syncModeThemeForSession('th-t')
    expect(useSessionThemeStore.getState().stacks['th-t']).toBeUndefined()
    // 声明齐备 → 入栈；重复调用幂等（模拟重载后再激活）
    registryOf([THEMED_DECL, WRITING_DECL])
    await syncModeThemeForSession('th-t')
    expect(useSessionThemeStore.getState().stacks['th-t']).toHaveLength(1)
    await syncModeThemeForSession('th-t')
    expect(useSessionThemeStore.getState().stacks['th-t']).toHaveLength(1)
    // 模式无 theme 声明 → 零动作
    localStorage.setItem(
      'session-exec-options:th-p',
      JSON.stringify({ values: {}, modeBinding: { mode: 'writing' } }),
    )
    await syncModeThemeForSession('th-p')
    expect(useSessionThemeStore.getState().stacks['th-p']).toBeUndefined()
  })
})

describe('readSessionAgentBinding / clearSessionAgentBinding — 绑定读清', () => {
  it('读出绑定；agentName 缺席回退 agentId 尾段；无绑定 null', () => {
    localStorage.setItem(
      'session-exec-options:th-1',
      JSON.stringify({ values: {}, agentId: 'mode_roleplay/card_luna', agentName: '月见' }),
    )
    localStorage.setItem(
      'session-exec-options:th-2',
      JSON.stringify({ values: {}, agentId: 'mode_roleplay/card_rin' }),
    )
    expect(readSessionAgentBinding('th-1')).toEqual({ agentId: 'mode_roleplay/card_luna', name: '月见' })
    expect(readSessionAgentBinding('th-2')).toEqual({ agentId: 'mode_roleplay/card_rin', name: 'card_rin' })
    expect(readSessionAgentBinding('th-none')).toBeNull()
  })

  it('清理只摘绑定键，其余快照键原样；无快照幂等', () => {
    localStorage.setItem(
      'session-exec-options:th-1',
      JSON.stringify({
        values: { conversation_mode: 'plan' },
        executionContext: { workspace: { source_path: '/w' } },
        agentId: 'mode_roleplay/card_luna',
        agentName: '月见',
        extraContext: { roleplay_greeting: '开场白' },
      }),
    )
    clearSessionAgentBinding('th-1')
    expect(loadSessionExecutionOptions('th-1')).toEqual({
      values: { conversation_mode: 'plan' },
      executionContext: { workspace: { source_path: '/w' } },
      extraContext: { roleplay_greeting: '开场白' },
    })
    expect(readSessionAgentBinding('th-1')).toBeNull()
    expect(() => clearSessionAgentBinding('th-none')).not.toThrow()
  })
})
