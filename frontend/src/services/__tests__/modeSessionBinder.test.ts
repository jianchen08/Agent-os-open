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
import { presetThemes } from '@/config/themes'
import { queryClient } from '@/services/query/queryClient'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useNotificationStore } from '@/stores/notificationStore'
import { getActiveSessionTheme, useSessionThemeStore } from '@/stores/sessionThemeStore'
import {
  applyDefaultModeBinding,
  clearSessionAgentBinding,
  ensureModeTheme,
  openModeSession,
  parseModeSessionPayload,
  readSessionAgentBinding,
  syncModeThemeForSession,
} from '../modeSessionBinder'
import { loadSessionExecutionOptions } from '../sessionExecutionOptions'
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
      modeBinding: { mode: ['roleplay'], pipelineConfigId: 'roleplay' },
      // 出生钉事实位：无卡主题键 + 模式无 theme 声明 → 如实 false
      themePinned: false,
    })
    expect(loadSessionExecutionOptions('th-角色扮演模式')).toMatchObject({
      values: {},
      modeBinding: { mode: ['roleplay'], pipelineConfigId: 'roleplay' },
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
    expect(receipt.modeBinding).toEqual({ mode: ['roleplay'], pipelineConfigId: 'roleplay' })
  })

  it('registry 不可达 → 降级：会话照常创建、modeBinding 仅 mode 键、无提示无管道', async () => {
    apiGetMock.mockRejectedValue(new Error('registry down'))

    const receipt = await openModeSession('roleplay')

    expect(receipt.sessionId).toBe('th-roleplay') // 标题回退 mode 键
    expect(receipt.modeBinding).toEqual({ mode: ['roleplay'] })
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

    // X13-F1：agentId 随出生请求落服务端（完整模式键），快照面同刀保留
    expect(createSessionMock).toHaveBeenCalledWith('扮演·月见', {
      agentId: 'mode_roleplay/card_luna',
    })
    expect(loadSessionExecutionOptions('th-扮演·月见')).toMatchObject({
      agentId: 'mode_roleplay/card_luna',
      agentName: '月见',
      extraContext: { roleplay_greeting: '「欢迎光临！」' },
      modeBinding: { mode: ['roleplay'], pipelineConfigId: 'roleplay' },
    })
    expect(sendUserInputMock).toHaveBeenCalledTimes(1)
    const [threadId, content, opts] = sendUserInputMock.mock.calls[0]
    expect(threadId).toBe('th-扮演·月见')
    expect(content).toBe('（开始）')
    expect(opts.pipelineId).toBe('pipe-扮演·月见')
    expect(opts.agentId).toBe('mode_roleplay/card_luna')
    expect(opts.pipelineConfigId).toBe('roleplay')
    expect(opts.clientMessageId).toBeTruthy()
    expect(opts.executionContext).toEqual({ mode: ['roleplay'], roleplay_greeting: '「欢迎光临！」' })
  })

  it('默认出生（mode 空串）→ registry 无 default 声明 = 无 modeBinding、无提示、宿主默认标题', async () => {
    const receipt = await openModeSession('')

    expect(receipt.modeBinding).toBeUndefined()
    expect(createSessionMock).toHaveBeenCalledWith(undefined)
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    expect(openPanelMock).not.toHaveBeenCalled()
  })

  it('M1 默认出生：mode 空串 → 绑 default 声明模式（含对话链管道，标题=模式名）', async () => {
    registryOf([
      { ...ROLEPLAY_DECL, default: false },
      {
        ...WRITING_DECL,
        mode: 'personal',
        name: '个人助理模式',
        default: true,
        pipelines: [],
        plugin_id: 'mode_personal',
      },
    ])

    const receipt = await openModeSession('')

    // 出生即定：绑 personal（无 conversation 管道声明 → 不带 pipelineConfigId）
    expect(receipt.modeBinding).toEqual({ mode: ['personal'] })
    expect(createSessionMock).toHaveBeenCalledWith('个人助理模式')
    expect(loadSessionExecutionOptions(receipt.sessionId)).toMatchObject({
      modeBinding: { mode: ['personal'] },
    })
  })

  it('M1 默认出生：default 模式带对话链 → pipelineConfigId 随绑；显式选择不受影响', async () => {
    registryOf([
      ROLEPLAY_DECL,
      {
        ...WRITING_DECL,
        mode: 'personal',
        name: '个人助理模式',
        default: true,
        icon: '🌱',
        pipelines: [{ name: 'personal_main', context: 'conversation' as const }],
        plugin_id: 'mode_personal',
      },
    ])
    const defaulted = await openModeSession('')
    expect(defaulted.modeBinding).toEqual({ mode: ['personal'], pipelineConfigId: 'personal_main' })

    // 显式选择（非空串）路径行为不变：仍按所选模式出生
    const explicit = await openModeSession('roleplay')
    expect(explicit.modeBinding).toEqual({ mode: ['roleplay'], pipelineConfigId: 'roleplay' })
  })

  it('M1 默认出生降级两态：registry 不可达 / 无 default 声明 = 不带绑定', async () => {
    apiGetMock.mockRejectedValue(new Error('registry down'))
    const unreachable = await openModeSession('')
    expect(unreachable.modeBinding).toBeUndefined()

    registryOf([ROLEPLAY_DECL, WRITING_DECL]) // 七包无一声明 default
    const noDefault = await openModeSession('')
    expect(noDefault.modeBinding).toBeUndefined()
  })

  it('默认出生不随开面板：空串解析到 default 声明模式且有面板页 → 不弹（环境模式不抢焦点）', async () => {
    registryOf([
      ROLEPLAY_DECL,
      {
        ...WRITING_DECL,
        mode: 'personal',
        name: '个人助理模式',
        default: true,
        pipelines: [],
        plugin_id: 'mode_personal',
      },
    ])
    contributionRegistry.register({
      type: 'pages', id: 'pim_today', title: '今日',
      space: 'workspace', slot: 'tab', path: '/p/agentos_pim_today', mode: 'personal',
      pluginId: 'mode_personal',
    })

    await openModeSession('')

    // modeBinding 照常出生（default 解析面不动），只有面板不随开
    expect(openPanelMock).not.toHaveBeenCalled()
  })

  it('显式出生不受页面 autoOpen:false 选退约束：点名模式 = 进入其工作面', async () => {
    contributionRegistry.register({
      type: 'pages', id: 'roleplay_studio2', title: '角色扮演工作台',
      space: 'workspace', slot: 'tab', path: '/p/roleplay_studio', mode: 'roleplay',
      autoOpen: false, pluginId: 'mode_roleplay',
    })

    await openModeSession('roleplay')

    expect(openPanelMock).toHaveBeenCalledTimes(1)
  })

  it('applyDefaultModeBinding：无绑定会话补挂 default；已有绑定不覆盖；无声明零动作', async () => {
    // 无声明 → 零动作
    await applyDefaultModeBinding('th-plain')
    expect(loadSessionExecutionOptions('th-plain')).toBeNull()

    registryOf([
      {
        ...WRITING_DECL,
        mode: 'personal',
        name: '个人助理模式',
        default: true,
        pipelines: [],
        plugin_id: 'mode_personal',
      },
    ])
    queryClient.clear() // registry 合成已换：清 5min 共享缓存再走 fetchQuery
    // 无绑定 → 补挂（其余快照键保留）
    localStorage.setItem(
      'session-exec-options:th-new',
      JSON.stringify({ values: { conversation_mode: 'plan' } }),
    )
    await applyDefaultModeBinding('th-new')
    expect(loadSessionExecutionOptions('th-new')).toMatchObject({
      values: { conversation_mode: 'plan' },
      modeBinding: { mode: ['personal'] },
    })

    // 已有绑定（显式选择）→ 不覆盖
    localStorage.setItem(
      'session-exec-options:th-bound',
      JSON.stringify({ values: {}, modeBinding: { mode: 'roleplay' } }),
    )
    await applyDefaultModeBinding('th-bound')
    expect(loadSessionExecutionOptions('th-bound')).toMatchObject({
      modeBinding: { mode: 'roleplay' },
    })
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
      modeBinding: { mode: ['roleplay'], pipelineConfigId: 'roleplay' },
    })
  })

  it('创建失败（网络）→ 异常向上传播（零发送零快照）', async () => {
    createSessionMock.mockRejectedValue(new Error('创建失败'))
    await expect(openModeSession('roleplay')).rejects.toThrow('创建失败')
    expect(sendUserInputMock).not.toHaveBeenCalled()
  })
})

describe('X13-F1 服务端出生绑定（面板桥会话换设备/清缓存不丢模式身份）', () => {
  // 缺陷基线（X5 静态走查）：openModeSession 的 createSession 只传 title，
  // agentId 只落浏览器快照 → 服务端 sessions.agent_id 空、state agent.id 回落
  // 默认 agentos（kernel 出生绑定 unwrap_or("agentos")）→ 换设备模式串味成普通
  // 会话。修法 = 出生请求补传完整模式键（kernel fail-closed 出生绑定的输入，
  // 弹窗轨 SessionEditModal 同口径）；本地快照面保留（主题/执行选项继续用）。
  it('绑定出生：createSession 收完整模式键 agent_id（服务端 fail-closed 出生绑定输入）', async () => {
    await openModeSession('roleplay', {
      agentId: 'mode_roleplay/card_luna',
      agentName: '月见',
    })
    // 值必须是完整模式键 mode_X/<stem>（非 config_id 裸 stem——SessionEditModal
    // 同款先例：裸 stem 丢前缀致人设配置装配落空）
    expect(createSessionMock).toHaveBeenCalledWith('角色扮演模式', {
      agentId: 'mode_roleplay/card_luna',
    })
  })

  it('清 localStorage 后身份仍可恢复：本地面可丢，服务端真值（出生请求键）恒在', async () => {
    const receipt = await openModeSession('roleplay', {
      agentId: 'mode_roleplay/card_rin',
      agentName: '凛',
    })
    localStorage.clear() // 换设备/清缓存：本地快照绑定面全丢
    expect(loadSessionExecutionOptions(receipt.sessionId)).toBeNull()
    // 服务端真值路径：mock 服务端捕获的出生请求仍持完整模式键——kernel 据此
    // fail-closed 落 state agent.id + sessions 投影，恢复链（setActiveSession
    // 读 session.agentId）从服务端回读身份
    expect(createSessionMock.mock.calls[0][1]).toEqual({ agentId: 'mode_roleplay/card_rin' })
  })

  it('无绑定出生不伪造服务端键：createSession 单参调用（不带 options）', async () => {
    await openModeSession('roleplay')
    expect(createSessionMock.mock.calls[0]).toHaveLength(1)
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

describe('mode.session 桥载荷 parse — pipeline_config_id 扩展位（批次一）', () => {
  it('合法登记名 → parsed.pipelineConfigId；空串/非字符串整包拒绝（与其余扩展位同 fail-closed 契约）', () => {
    expect(parseModeSessionPayload({ mode: 'roleplay', pipeline_config_id: 'triple_chain' })).toEqual({
      mode: 'roleplay',
      pipelineConfigId: 'triple_chain',
    })
    expect(parseModeSessionPayload({ mode: 'roleplay', pipeline_config_id: '' })).toBeNull()
    expect(parseModeSessionPayload({ mode: 'roleplay', pipeline_config_id: 7 })).toBeNull()
    // 键缺席 = 不伪造（现役载荷形态零新增）
    expect(parseModeSessionPayload({ mode: 'roleplay' })).toEqual({ mode: 'roleplay' })
  })
})

describe('openModeSession 玩法级管道绑定覆盖（批次一：缺省=mode 绑定管道，现役零变化）', () => {
  it('显式 pipelineConfigId 覆盖 mode 对话链管道：快照与回执同值、首条帧随覆盖管道', async () => {
    const receipt = await openModeSession('roleplay', {
      pipelineConfigId: 'triple_chain',
      firstMessage: '（开始）',
    })

    expect(receipt.modeBinding).toEqual({ mode: ['roleplay'], pipelineConfigId: 'triple_chain' })
    expect(loadSessionExecutionOptions('th-角色扮演模式')).toMatchObject({
      modeBinding: { mode: ['roleplay'], pipelineConfigId: 'triple_chain' },
    })
    expect(sendUserInputMock).toHaveBeenCalledWith(
      'th-角色扮演模式',
      '（开始）',
      expect.objectContaining({ pipelineConfigId: 'triple_chain' }),
    )
  })

  it('覆盖不依赖 registry 可达：声明不可达时显式绑定仍生效（玩法声明自带交互形态）', async () => {
    apiGetMock.mockRejectedValue(new Error('registry down'))
    const receipt = await openModeSession('roleplay', { pipelineConfigId: 'triple_chain' })
    expect(receipt.modeBinding).toEqual({ mode: ['roleplay'], pipelineConfigId: 'triple_chain' })
  })

  it('缺省回落：不携覆盖 = mode 绑定管道（现役行为零变化锚，与既有出生测试互证）', async () => {
    const receipt = await openModeSession('roleplay')
    expect(receipt.modeBinding).toEqual({ mode: ['roleplay'], pipelineConfigId: 'roleplay' })
  })
})

describe('mode.session 桥载荷 parse — card_theme 扩展位（卡主题随扮演会话出生钉）', () => {
  /** 卡库 /data/cards 条目 theme 字段形态（完整 ThemeConfig 档，出厂像素档） */
  const CARD_THEME = presetThemes['pixel-art']

  it('合法档 → parsed.cardTheme 原样透传；键缺席不伪造（现役载荷形态零新增）', () => {
    expect(parseModeSessionPayload({ mode: 'roleplay', card_theme: CARD_THEME })?.cardTheme).toEqual(
      CARD_THEME,
    )
    expect(parseModeSessionPayload({ mode: 'roleplay' })).toEqual({ mode: 'roleplay' })
  })

  // 非法档只丢该位不阻断出生（fail-closed 的粒度=扩展位，其余载荷照常解析）
  const INVALID_THEMES: unknown[] = [
    null,
    'not-a-theme',
    { id: 'broken', name: '坏档' }, // 缺 colors/components/effects/backgrounds
    { ...CARD_THEME, colors: 'not-a-map' },
  ]

  it.each(INVALID_THEMES.map((t, i) => [i, t] as const))(
    '非法档 #%i → 丢弃该位、其余扩展位与 mode 照常解析（出生零阻断）',
    (_i, bad) => {
      const parsed = parseModeSessionPayload({
        mode: 'roleplay',
        title: '扮演·月见',
        card_theme: bad,
      })
      expect(parsed).toEqual({ mode: 'roleplay', title: '扮演·月见' })
      expect(parsed?.cardTheme).toBeUndefined()
    },
  )
})

describe('openModeSession 卡主题出生钉入（出生即钉，与附身出生钉同构）', () => {
  /** 卡库 /data/cards 条目 theme 字段形态（出厂像素档：用户气泡描边可见） */
  const CARD_THEME = presetThemes['pixel-art']
  /** 带 theme 声明的合成模式（模式级主题通道的既有消费面） */
  const THEMED_DECL_FOR_CARD = {
    ...WRITING_DECL,
    mode: 'immersive',
    name: '沉浸模式',
    theme: null as string | null,
  }

  it('合法卡档 → 新会话栈顶=卡主题、回执 themePinned=true（气泡变量随会话 override 走）', async () => {
    const receipt = await openModeSession('roleplay', { cardTheme: CARD_THEME })

    const stack = useSessionThemeStore.getState().stacks[receipt.sessionId]
    expect(stack).toHaveLength(1)
    expect(stack?.[0].id).toBe(CARD_THEME.id)
    expect(receipt.themePinned).toBe(true)
    // 生效面断言：会话 override 顶档就是卡档（ChatContainer 经作用域消费）
    expect(getActiveSessionTheme(receipt.sessionId)?.colors.bubble.user_border).toBe(
      CARD_THEME.colors.bubble.user_border,
    )
  })

  it('无主题卡（键缺席）→ 出生行为与现役一致：栈零条目、themePinned=false', async () => {
    const receipt = await openModeSession('roleplay', { title: '扮演·月见' })

    expect(receipt.themePinned).toBe(false)
    expect(useSessionThemeStore.getState().stacks[receipt.sessionId]).toBeUndefined()
    // 现役出生面不变：会话建出 + modeBinding 快照照落
    expect(createSessionMock).toHaveBeenCalledWith('扮演·月见')
    expect(loadSessionExecutionOptions(receipt.sessionId)?.modeBinding).toEqual({
      mode: ['roleplay'],
      pipelineConfigId: 'roleplay',
    })
  })

  it('非法卡档（pushTheme 二次 fail-closed 拦下）→ 不钉、入栈零变更、出生零失败', async () => {
    // 通道契约：直调出生扩展位（桥 parse 已拦一道；此例钉住入栈位二次校验面）
    const receipt = await openModeSession('roleplay', {
      cardTheme: { id: 'broken', name: '坏档' } as unknown as typeof CARD_THEME,
    })

    expect(receipt.themePinned).toBe(false)
    expect(useSessionThemeStore.getState().stacks[receipt.sessionId]).toBeUndefined()
    expect(receipt.sessionId).toBe('th-角色扮演模式')
  })

  it('卡主题优先于 mode 声明主题（更具体者胜，缺省回落现役通道）', async () => {
    registryOf([
      { ...THEMED_DECL_FOR_CARD, theme: 'deep-space' },
      { ...THEMED_DECL_FOR_CARD, mode: 'immersive2', name: '沉浸模式二', theme: 'deep-space' },
      WRITING_DECL,
    ])
    const withCard = await openModeSession('immersive', { cardTheme: CARD_THEME })
    expect(useSessionThemeStore.getState().stacks[withCard.sessionId]?.map((c) => c.id)).toEqual([
      CARD_THEME.id,
    ])

    const withoutCard = await openModeSession('immersive2')
    expect(useSessionThemeStore.getState().stacks[withoutCard.sessionId]?.map((c) => c.id)).toEqual([
      'deep-space',
    ])
    expect(withoutCard.themePinned).toBe(true)
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
