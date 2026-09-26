/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * roleplayContinue 服务测试 — 扮演会话绑定（roleplay.continue 桥宿主侧落地）
 *
 * 核验：
 * - parseRoleplayContinuePayload：card_id/name 非空 fail-closed（非法整包 null）；
 *   可选 greeting/personaText（会话化开演档）缺省合法、在场须非空字符串；
 * - createRoleplaySession：store.createSession 建会话（扮演·<卡名>）→ 该会话
 *   执行选项写入 agentId/agentName + 开演档快照键（调用序：创建先于快照保存）
 *   → 自动首条触发消息（pipelineId=会话主管道、execution_context 复用发送链
 *   并入形态、agentId 随帧）→ 回执含 sessionId；
 * - readSessionAgent / clearSessionAgent：读出回退尾段名、清理只动绑定键。
 *
 * mock 仅外部依赖：sessionListStore（会话创建走内核 API 网络）与 globalWS
 * （WS 帧出站）；快照存取走真实 localStorage（jsdom 内建，sessionExecutionOptions
 * 车道同口径）。
 */
import { vi } from 'vitest'
const { createSessionMock, sendUserInputMock } = vi.hoisted(() => ({
  createSessionMock: vi.fn(),
  sendUserInputMock: vi.fn(),
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: { getState: () => ({ createSession: createSessionMock }) },
}))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { sendUserInput: sendUserInputMock },
}))
import {
  clearSessionAgent,
  createRoleplaySession,
  parseRoleplayContinuePayload,
  readSessionAgent,
} from '../roleplayContinue'
import { loadSessionExecutionOptions } from '../sessionExecutionOptions'

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('parseRoleplayContinuePayload — 载荷 fail-closed', () => {
  it('card_id/name 非空 → 通过（avatar 装饰位不参与校验）', () => {
    expect(parseRoleplayContinuePayload({ card_id: 'card_luna', name: '月见' })).toEqual({
      card_id: 'card_luna',
      name: '月见',
    })
    expect(
      parseRoleplayContinuePayload({ card_id: 'card_luna', name: '月见', avatar: '🌙' }),
    ).toEqual({ card_id: 'card_luna', name: '月见' })
  })

  it('card_id/name 缺失、空串、非字符串、载荷非对象 → null（零宽松）', () => {
    expect(parseRoleplayContinuePayload(null)).toBeNull()
    expect(parseRoleplayContinuePayload('card_luna')).toBeNull()
    expect(parseRoleplayContinuePayload({ name: '月见' })).toBeNull()
    expect(parseRoleplayContinuePayload({ card_id: 'card_luna' })).toBeNull()
    expect(parseRoleplayContinuePayload({ card_id: '  ', name: '月见' })).toBeNull()
    expect(parseRoleplayContinuePayload({ card_id: 'card_luna', name: '' })).toBeNull()
    expect(parseRoleplayContinuePayload({ card_id: 42, name: '月见' })).toBeNull()
  })

  it('开演档键（会话化开演）缺省合法，在场非空字符串随载荷通过', () => {
    expect(
      parseRoleplayContinuePayload({
        card_id: 'card_luna',
        name: '月见',
        greeting: '「欢迎光临！」',
        personaText: '北地来的佣兵。',
      }),
    ).toEqual({
      card_id: 'card_luna',
      name: '月见',
      greeting: '「欢迎光临！」',
      personaText: '北地来的佣兵。',
    })
    // 转续演档：两键缺省
    expect(parseRoleplayContinuePayload({ card_id: 'card_rin', name: '凛' })).toEqual({
      card_id: 'card_rin',
      name: '凛',
    })
    // 在场但空串/纯空白/非字符串 → 整包丢弃（fail-closed 同一 posture）
    expect(
      parseRoleplayContinuePayload({ card_id: 'card_luna', name: '月见', greeting: '' }),
    ).toBeNull()
    expect(
      parseRoleplayContinuePayload({ card_id: 'card_luna', name: '月见', greeting: '   ' }),
    ).toBeNull()
    expect(
      parseRoleplayContinuePayload({ card_id: 'card_luna', name: '月见', personaText: 42 }),
    ).toBeNull()
  })
})

describe('createRoleplaySession — 建会话 + 绑定写面 + 自动首条', () => {
  it('先创建（扮演·<卡名>）后存快照；自动首条以会话主管道发送并带身份', async () => {
    const titles: string[] = []
    createSessionMock.mockImplementation(async (title: string) => {
      titles.push(title)
      return { id: `th-${titles.length}`, title, pipelineIds: [`pipe-${titles.length}`] }
    })

    const result = await createRoleplaySession({ card_id: 'card_luna', name: '月见' })

    expect(result).toEqual({ sessionId: 'th-1', agentId: 'mode_roleplay/card_luna', name: '月见' })
    expect(titles).toEqual(['扮演·月见'])
    // 调用序：快照写在新会话 id 名下（键来自创建回执，创建必先于保存）
    expect(loadSessionExecutionOptions(result.sessionId)).toMatchObject({
      values: {},
      agentId: 'mode_roleplay/card_luna',
      agentName: '月见',
    })
    // 自动首条（创建→绑定→切换→开演）：极短触发 + 主管道 + 身份帧
    expect(sendUserInputMock).toHaveBeenCalledTimes(1)
    const [threadId, content, opts] = sendUserInputMock.mock.calls[0]
    expect(threadId).toBe('th-1')
    expect(content).toBe('（开始）')
    expect(opts.pipelineId).toBe('pipe-1')
    expect(opts.agentId).toBe('mode_roleplay/card_luna')
    expect(opts.clientMessageId).toBeTruthy()
    expect(opts.executionContext).toEqual({ mode: 'roleplay' })
  })

  it('会话化开演档（greeting/personaText）落快照 + 并入首条 execution_context', async () => {
    createSessionMock.mockResolvedValue({ id: 'th-rp-9', title: '扮演·澪', pipelineIds: ['pipe-9'] })

    const result = await createRoleplaySession({
      card_id: 'card_mio',
      name: '澪',
      greeting: '「呀——欢迎光临！」',
      personaText: '北地来的佣兵，沉默寡言。',
    })

    expect(loadSessionExecutionOptions('th-rp-9')).toMatchObject({
      agentId: 'mode_roleplay/card_mio',
      agentName: '澪',
      roleplayGreeting: '「呀——欢迎光临！」',
      roleplayUserPersona: '北地来的佣兵，沉默寡言。',
    })
    expect(sendUserInputMock.mock.calls[0][2].executionContext).toEqual({
      mode: 'roleplay',
      roleplay_greeting: '「呀——欢迎光临！」',
      roleplay_user_persona: '北地来的佣兵，沉默寡言。',
    })
    expect(result.sessionId).toBe('th-rp-9')
  })

  it('无开演档（转续演档）→ 快照不带开演档键、首条仅 mode 键', async () => {
    createSessionMock.mockResolvedValue({ id: 'th-rp-2', title: '扮演·凛', pipelineIds: [] })
    await createRoleplaySession({ card_id: 'card_rin', name: '凛' })
    const snapshot = loadSessionExecutionOptions('th-rp-2')!
    expect(snapshot).toMatchObject({ agentId: 'mode_roleplay/card_rin', agentName: '凛' })
    expect(snapshot).not.toHaveProperty('roleplayGreeting')
    expect(snapshot).not.toHaveProperty('roleplayUserPersona')
    // 无主管道映射（pipelineIds 空）：pipelineId 缺省，内核按 thread 解析
    expect(sendUserInputMock.mock.calls[0][2].pipelineId).toBeUndefined()
    expect(sendUserInputMock.mock.calls[0][2].executionContext).toEqual({ mode: 'roleplay' })
  })

  it('快照写入既有会话键不覆盖其余键（无快照落最小包，有快照原样保留）', async () => {
    createSessionMock.mockResolvedValue({ id: 'th-rp-2', title: '扮演·凛', pipelineIds: [] })
    await createRoleplaySession({ card_id: 'card_rin', name: '凛' })
    expect(loadSessionExecutionOptions('th-rp-2')).toEqual({
      values: {},
      agentId: 'mode_roleplay/card_rin',
      agentName: '凛',
    })

    // 同会话重绑（极端路径：会话 id 撞已有快照）：values/executionContext 保留
    localStorage.setItem(
      'session-exec-options:th-rp-2',
      JSON.stringify({ values: { conversation_mode: 'plan' }, executionContext: { isolation: { level: 'high' } } }),
    )
    await createRoleplaySession({ card_id: 'card_mio', name: '澪' })
    expect(loadSessionExecutionOptions('th-rp-2')).toMatchObject({
      values: { conversation_mode: 'plan' },
      executionContext: { isolation: { level: 'high' } },
      agentId: 'mode_roleplay/card_mio',
      agentName: '澪',
    })
  })

  it('创建失败（网络）→ 异常向上传播（桥 catch 回 error，不假成功，零发送）', async () => {
    createSessionMock.mockRejectedValue(new Error('创建失败'))
    await expect(createRoleplaySession({ card_id: 'card_luna', name: '月见' })).rejects.toThrow('创建失败')
    expect(loadSessionExecutionOptions('th-any')).toBeNull()
    expect(sendUserInputMock).not.toHaveBeenCalled()
  })
})

describe('readSessionAgent / clearSessionAgent — 绑定读清', () => {
  it('读出绑定；agentName 缺席回退 agentId 尾段', () => {
    localStorage.setItem(
      'session-exec-options:th-1',
      JSON.stringify({ values: {}, agentId: 'mode_roleplay/card_luna', agentName: '月见' }),
    )
    localStorage.setItem(
      'session-exec-options:th-2',
      JSON.stringify({ values: {}, agentId: 'mode_roleplay/card_rin' }),
    )
    expect(readSessionAgent('th-1')).toEqual({ agentId: 'mode_roleplay/card_luna', name: '月见' })
    expect(readSessionAgent('th-2')).toEqual({ agentId: 'mode_roleplay/card_rin', name: 'card_rin' })
    expect(readSessionAgent('th-none')).toBeNull()
  })

  it('清理只摘绑定键，其余快照键原样；无快照幂等', () => {
    localStorage.setItem(
      'session-exec-options:th-1',
      JSON.stringify({
        values: { conversation_mode: 'plan' },
        executionContext: { workspace: { source_path: '/w' } },
        agentId: 'mode_roleplay/card_luna',
        agentName: '月见',
        roleplayGreeting: '开场白',
      }),
    )
    clearSessionAgent('th-1')
    const snapshot = loadSessionExecutionOptions('th-1')
    expect(snapshot).toEqual({
      values: { conversation_mode: 'plan' },
      executionContext: { workspace: { source_path: '/w' } },
      roleplayGreeting: '开场白',
    })
    expect(readSessionAgent('th-1')).toBeNull()
    expect(() => clearSessionAgent('th-none')).not.toThrow()
  })
})
