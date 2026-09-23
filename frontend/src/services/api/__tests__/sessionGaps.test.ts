// @feature: FP-T12 补测 | @ci: frontend-test
/** @feature 覆盖率冲刺批九（前端 API 层） | @ci frontend-test */
/**
 * session.ts 覆盖缺口补测（axios adapter 层 mock，链路真实）：
 * - getThreadSchema / createSession / updateSession / updateSessionAgent 此前零覆盖
 * - getMessages 过滤参数映射、信封透传兜底（transient_states / total / messages）
 * - 消息映射分支：系统消息、思考内容、toolCalls 归一、tool 结果消息投影
 * - mergeConsecutiveAssistantMessages 空合并组与 toolCallId 无匹配分支
 *
 * mock 边界：仅 axios 传输层（axios-mock-adapter），映射与校验全部真实链路。
 */
import MockAdapter from 'axios-mock-adapter'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import {
  createSession,
  getMessages,
  getSessions,
  getThreadSchema,
  mapBackendMessageToMessage,
  mergeConsecutiveAssistantMessages,
  updateSession,
  updateSessionAgent,
  type BackendMessageResponse,
} from '@/services/api/session'
import type { Message } from '@/types/models'

const mockAxios = new MockAdapter(apiClient)

const TS = '2026-09-13T00:00:00Z'

function backendMessage(overrides: Partial<BackendMessageResponse>): BackendMessageResponse {
  return {
    id: 'msg-1',
    thread_id: 's1',
    role: 'user',
    content: 'hello',
    timestamp: TS,
    ...overrides,
  }
}

function assistantMsg(id: string, content: string, parts?: Message['parts']): Message {
  return {
    id,
    sessionId: 's1',
    role: 'assistant',
    content,
    timestamp: TS,
    ...(parts ? { parts } : {}),
  } as Message
}

describe('session API 覆盖缺口（批九）', () => {
  beforeEach(() => {
    mockAxios.reset()
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  describe('getSessions 响应形状', () => {
    it('裸数组响应（无 threads 信封）直接映射；非数组无 threads 字段回退空列表', async () => {
      mockAxios
        .onGet(API_ENDPOINTS.SESSIONS.LIST)
        .reply(200, [
          {
            thread_id: 'th-bare',
            current_state: 'idle',
            intent: '裸数组',
            created_at: TS,
            updated_at: TS,
          },
        ])
      const sessions = await getSessions()
      expect(sessions).toHaveLength(1)
      expect(sessions[0]).toMatchObject({ id: 'th-bare', title: '裸数组' })

      mockAxios.onGet(API_ENDPOINTS.SESSIONS.LIST).reply(200, {})
      await expect(getSessions()).resolves.toEqual([])
    })
  })

  describe('getThreadSchema', () => {
    it('返回后端 fields 数组（原样透传字段声明）', async () => {
      const fields = [
        { name: 'workspace', type: 'string', label: '工作空间', required: true },
        {
          name: 'mode',
          type: 'select',
          options: [{ label: '容器', value: 'isolated' }],
          x_metadata_key: 'isolation_mode',
        },
      ]
      mockAxios.onGet(API_ENDPOINTS.SESSIONS.SCHEMA).reply(200, { fields })

      await expect(getThreadSchema()).resolves.toEqual(fields)
    })

    it('fields 缺失或非数组时回退空数组（不炸、不猜）', async () => {
      mockAxios.onGet(API_ENDPOINTS.SESSIONS.SCHEMA).reply(200, {})
      await expect(getThreadSchema()).resolves.toEqual([])

      mockAxios.onGet(API_ENDPOINTS.SESSIONS.SCHEMA).reply(200, { fields: 'not-an-array' })
      await expect(getThreadSchema()).resolves.toEqual([])
    })
  })

  describe('createSession', () => {
    it('完整选项：POST 体携带 title/intent/agent_id/metadata 与主 Agent 请求头，响应映射为 Session', async () => {
      mockAxios.onPost(API_ENDPOINTS.SESSIONS.CREATE).reply(200, {
        thread_id: 'th-1',
        created_at: TS,
        current_state: 'idle',
        intent: '修复登录',
        updated_at: '2026-09-13T00:01:00Z',
        agent_id: 'ag-1',
        pipeline_ids: ['p-main', 'p-sub'],
        active_pipeline_id: 'p-main',
      })

      const session = await createSession({
        title: '修复登录',
        agentId: 'ag-1',
        fieldMetadata: { isolation_mode: 'isolated' },
      })

      expect(mockAxios.history.post).toHaveLength(1)
      const req = mockAxios.history.post[0]
      expect(req.headers['X-Main-Agent-Request']).toBe('true')
      expect(JSON.parse(req.data)).toEqual({
        title: '修复登录',
        intent: '修复登录',
        agent_id: 'ag-1',
        metadata: { isolation_mode: 'isolated' },
      })
      expect(session).toMatchObject({
        id: 'th-1',
        title: '修复登录',
        status: 'idle',
        agentId: 'ag-1',
        pipelineIds: ['p-main', 'p-sub'],
      })
      expect(session.createdAt).toBe(TS)
      expect(session.updatedAt).toBe('2026-09-13T00:01:00Z')
    })

    it('缺省选项：body 为空对象（无多余键）；最小响应走兜底映射；空 fieldMetadata 不写 metadata', async () => {
      mockAxios.onPost(API_ENDPOINTS.SESSIONS.CREATE).reply(200, {
        thread_id: 'th-2',
        created_at: TS,
      })

      const session = await createSession()

      const body = JSON.parse(mockAxios.history.post[0].data)
      expect(body).toEqual({})

      expect(session).toMatchObject({
        id: 'th-2',
        title: '未命名会话',
        status: 'created',
        agentId: null,
        pipelineIds: [],
      })
      expect(session.updatedAt).toBe(TS)
      expect(session.intent).toBeUndefined()

      // 空对象 fieldMetadata 与缺省等效：不产生 metadata 键
      await createSession({ fieldMetadata: {} })
      expect(JSON.parse(mockAxios.history.post[1].data)).toEqual({})
    })
  })

  describe('updateSessionAgent', () => {
    it('PATCH agent 端点：body 携带 agent_id，响应映射为 Session', async () => {
      mockAxios.onPatch(API_ENDPOINTS.SESSIONS.UPDATE_AGENT('s9')).reply(200, {
        thread_id: 's9',
        current_state: 'running',
        intent: '标题',
        created_at: TS,
        updated_at: TS,
        agent_id: 'agent-9',
      })

      const session = await updateSessionAgent('s9', 'agent-9')

      expect(JSON.parse(mockAxios.history.patch[0].data)).toEqual({ agent_id: 'agent-9' })
      expect(session).toMatchObject({ id: 's9', agentId: 'agent-9', status: 'running' })
    })

    it('agentId null：body 显式 agent_id null，映射 agentId null；空会话 ID 拒绝', async () => {
      mockAxios.onPatch(API_ENDPOINTS.SESSIONS.UPDATE_AGENT('s9')).reply(200, {
        thread_id: 's9',
        current_state: 'idle',
        intent: null,
        created_at: TS,
        updated_at: TS,
      })

      const session = await updateSessionAgent('s9', null)

      expect(JSON.parse(mockAxios.history.patch[0].data)).toEqual({ agent_id: null })
      expect(session.agentId).toBeNull()

      await expect(updateSessionAgent('', 'agent-1')).rejects.toThrow('会话ID不能为空')
      await expect(updateSessionAgent('   ', null)).rejects.toThrow('会话ID不能为空')
    })
  })

  describe('updateSession', () => {
    it('title/agentId/metadata 全量选项：body 映射为 intent/agent_id/metadata，响应映射回 Session', async () => {
      mockAxios.onPatch(API_ENDPOINTS.SESSIONS.UPDATE('s1')).reply(200, {
        thread_id: 's1',
        current_state: 'idle',
        intent: '新标题',
        created_at: TS,
        updated_at: TS,
        agent_id: 'ag-2',
      })

      const session = await updateSession('s1', {
        title: '新标题',
        agentId: 'ag-2',
        metadata: { pinned: true },
      })

      expect(JSON.parse(mockAxios.history.patch[0].data)).toEqual({
        intent: '新标题',
        agent_id: 'ag-2',
        metadata: { pinned: true },
      })
      expect(session).toMatchObject({ id: 's1', title: '新标题', agentId: 'ag-2' })
    })

    it('响应缺 thread_id 回退 sessionId、缺 agent_id 回退 null；缺省选项不产多余键；agentId null 显式写入', async () => {
      mockAxios.onPatch(API_ENDPOINTS.SESSIONS.UPDATE('s1')).reply(200, {
        current_state: 'idle',
        intent: 't',
        created_at: TS,
        updated_at: TS,
      })

      const session = await updateSession('s1', { title: 't' })
      expect(session.id).toBe('s1')
      expect(session.agentId).toBeNull()

      const body = JSON.parse(mockAxios.history.patch[0].data)
      expect(body).toEqual({ intent: 't' })

      // agentId 为 null 是显式解绑，与缺省（键不存在）不同
      await updateSession('s1', { agentId: null })
      expect(JSON.parse(mockAxios.history.patch[1].data)).toEqual({ agent_id: null })
    })

    it('空会话 ID 拒绝且不发请求', async () => {
      await expect(updateSession('', { title: 'x' })).rejects.toThrow('会话ID不能为空')
      await expect(updateSession('  ', {})).rejects.toThrow('会话ID不能为空')
      expect(mockAxios.history.patch).toHaveLength(0)
    })
  })

  describe('getMessages 过滤参数映射', () => {
    beforeEach(() => {
      mockAxios.onGet(API_ENDPOINTS.MESSAGES.LIST('s1')).reply(200, {
        messages: [],
        total: 0,
        has_more: false,
      })
    })

    it('全量过滤器按 snake_case 键名映射进查询参数', async () => {
      await getMessages('s1', {
        agentId: 'ag',
        parentId: 'p1',
        pipelineRunId: 'pr',
        depth: 2,
        executorType: 'tool',
        skip: 3,
        limit: 7,
        before_sequence: 10,
        after_sequence: 1,
      })

      expect(mockAxios.history.get[0].params).toEqual({
        agent_id: 'ag',
        parent_id: 'p1',
        pipeline_run_id: 'pr',
        depth: 2,
        executor_type: 'tool',
        skip: 3,
        limit: 7,
        before_sequence: 10,
        after_sequence: 1,
      })
    })

    it('零值数字过滤保留（!== undefined 口径）、空串过滤剔除；无过滤器时 params 为空', async () => {
      await getMessages('s1', {
        agentId: '',
        depth: 0,
        skip: 0,
        limit: 0,
        before_sequence: 0,
        after_sequence: 0,
      })
      expect(mockAxios.history.get[0].params).toEqual({
        depth: 0,
        skip: 0,
        limit: 0,
        before_sequence: 0,
        after_sequence: 0,
      })

      await getMessages('s1')
      expect(mockAxios.history.get[1].params).toEqual({})
    })
  })

  describe('getMessages 信封兜底与透传', () => {
    const userMsg: BackendMessageResponse = backendMessage({ id: 'm1' })

    it('transient_states 数组透传给调用方（刷新恢复中间态）', async () => {
      const transientStates = [{ key: 'chunk:m1', value: { text_len: 3 } }]
      mockAxios.onGet(API_ENDPOINTS.MESSAGES.LIST('s1')).reply(200, {
        messages: [userMsg],
        total: 1,
        has_more: true,
        transient_states: transientStates,
      })

      const result = await getMessages('s1')
      expect(result.has_more).toBe(true)
      expect(result.transient_states).toEqual([{ key: 'chunk:m1', value: { text_len: 3 } }])
    })

    it('无 transient_states 时字段缺省（不虚构空数组）', async () => {
      mockAxios.onGet(API_ENDPOINTS.MESSAGES.LIST('s1')).reply(200, {
        messages: [userMsg],
        total: 1,
        has_more: false,
      })

      const result = await getMessages('s1')
      expect(result.transient_states).toBeUndefined()
    })

    it('信封为字符串（网关噪音）→ 抛出带会话标识的明确错误', async () => {
      mockAxios.onGet(API_ENDPOINTS.MESSAGES.LIST('s1')).reply(200, 'gateway noise')
      await expect(getMessages('s1')).rejects.toThrow('响应缺少数据体 (session=s1')
    })

    it('缺 messages 键回退空列表但 total 透传；缺 total 回退消息条数', async () => {
      mockAxios.onGet(API_ENDPOINTS.MESSAGES.LIST('s1')).reply(200, { total: 4, has_more: true })
      let result = await getMessages('s1')
      expect(result.messages).toEqual([])
      expect(result.total).toBe(4)

      mockAxios.onGet(API_ENDPOINTS.MESSAGES.LIST('s1')).reply(200, {
        messages: [userMsg],
        has_more: false,
      })
      result = await getMessages('s1')
      expect(result.messages).toHaveLength(1)
      expect(result.total).toBe(1)
    })
  })

  describe('mapBackendMessageToMessage：tool 结果消息投影', () => {
    it('缺省与兜底：sequence 0、status completed、toolResult 回退 content、null 结果归一 undefined、error 优先 toolError、耗时回退链', () => {
      const msg = mapBackendMessageToMessage(
        backendMessage({
          id: 't1',
          role: 'tool',
          content: '执行结果文本',
          // sequence/status 缺省、toolResult 缺省、toolResultData null、
          // error 与 toolDurationMs 缺省（走 toolError/durationMs 兜底）
          toolCallId: 'call-1',
          toolName: 'fs',
          toolArgs: { path: 'x' },
          toolResultData: null,
          toolError: '旧字段错误',
          durationMs: 55,
        }),
        's1',
      )

      expect(msg.role).toBe('tool')
      expect(msg.sequence).toBe(0)
      expect(msg.status).toBe('completed')
      expect(msg.toolCallId).toBe('call-1')
      expect(msg.toolResult).toBe('执行结果文本')
      expect(msg.toolResultData).toBeUndefined()
      expect(msg.toolError).toBe('旧字段错误')
      expect(msg.durationMs).toBe(55)
    })

    it('显式值优先：error 压过 toolError、toolDurationMs 压过 durationMs、toolResult/toolResultData 透传', () => {
      const resultData = { rows: [{ a: 1 }], diff: { added: 2 } }
      const msg = mapBackendMessageToMessage(
        backendMessage({
          id: 't2',
          role: 'tool',
          content: 'content 不参与结果',
          sequence: 5,
          status: 'failed',
          toolCallId: 'call-2',
          toolResult: '结构化结果',
          toolResultData: resultData,
          error: '新字段错误',
          toolError: '不应被采用',
          toolDurationMs: 77,
          durationMs: 1,
          containerTaskId: 'ct-1',
        }),
        's1',
      )

      expect(msg.sequence).toBe(5)
      expect(msg.status).toBe('failed')
      expect(msg.toolResult).toBe('结构化结果')
      expect(msg.toolResultData).toEqual(resultData)
      expect(msg.toolError).toBe('新字段错误')
      expect(msg.durationMs).toBe(77)
      expect(msg.containerTaskId).toBe('ct-1')
    })
  })

  describe('mapBackendMessageToMessage：非 tool 消息组装', () => {
    it('metadata.record_type=system 的消息修正为 system 角色：system part + level/notificationType 兜底', () => {
      const msg = mapBackendMessageToMessage(
        backendMessage({ role: 'user', metadata: { record_type: 'system' } }),
        's1',
      )

      expect(msg.role).toBe('system')
      expect(msg.parts).toHaveLength(1)
      expect(msg.parts?.[0]).toMatchObject({
        type: 'system',
        content: 'hello',
        level: 'info',
        notificationType: 'task_notification',
        sequence: 0,
      })
    })

    it('role=system 且显式 notification 元数据：level/notificationType 透传', () => {
      const msg = mapBackendMessageToMessage(
        backendMessage({
          role: 'system',
          metadata: { notification_level: 'warning', notification_type: 'quota_exceeded' },
        }),
        's1',
      )

      expect(msg.parts?.[0]).toMatchObject({
        type: 'system',
        level: 'warning',
        notificationType: 'quota_exceeded',
      })
    })

    it('agentName 并入 metadata；无 agentName 不产键', () => {
      const withName = mapBackendMessageToMessage(
        backendMessage({ role: 'assistant', agentName: 'Coder' }),
        's1',
      )
      expect(withName.metadata?.agentName).toBe('Coder')

      const withoutName = mapBackendMessageToMessage(backendMessage({ role: 'assistant' }), 's1')
      expect('agentName' in (withoutName.metadata ?? {})).toBe(false)
    })

    it('toolCalls 归一：id/callId 兜底、function.arguments JSON 解析、非法 JSON 保留原值、缺参回空对象、error 决定 part 状态', () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      const msg = mapBackendMessageToMessage(
        backendMessage({
          role: 'assistant',
          content: '执行工具',
          toolCalls: [
            // OpenAI 形态：name/arguments 嵌套在 function 下
            { id: 'call-a', function: { name: 'fs', arguments: '{"path":"a.txt"}' } },
            { callId: 'call-b', toolName: 'shell', error: 'boom' },
            { callId: 'call-c', toolName: 'shell' },
            { toolName: 'broken', function: { arguments: '{invalid json' } },
            { callId: 'call-e' },
            // ToolCallItem 形态：toolArgs 直接为对象
            { callId: 'call-f', toolName: 'db', toolArgs: { table: 'users' } },
          ],
        }),
        's1',
      )

      const toolParts = msg.parts?.filter((p) => p.type === 'tool_call') ?? []
      expect(toolParts).toHaveLength(6)
      const [a, b, c, d, e, f] = toolParts as Array<Record<string, any>>

      expect(a).toMatchObject({ callId: 'call-a', name: 'fs', state: 'done' })
      expect(a.args).toEqual({ path: 'a.txt' })

      expect(b.state).toBe('error')
      expect(b.error).toBe('boom')

      expect(c.state).toBe('done')

      expect(d.args).toBe('{invalid json')

      expect(e).toMatchObject({ callId: 'call-e', name: '', args: {} })
      expect(f).toMatchObject({ callId: 'call-f', name: 'db' })
      expect(f.args).toEqual({ table: 'users' })
      expect(warnSpy).not.toHaveBeenCalled()
    })

    it('未知 toolCall 状态告警一次（已知状态不告警），模块级同值去重', () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      const unknown = 'weird_status_gap_9'
      const mk = (status: unknown, callId: string) =>
        mapBackendMessageToMessage(
          backendMessage({
            id: `m-${callId}`,
            role: 'assistant',
            content: '',
            toolCalls: [{ callId, toolName: 'fs', status }],
          }),
          's1',
        )

      // 已知状态：不告警
      mk('completed', 'call-known')
      expect(warnSpy).not.toHaveBeenCalled()

      // 未知状态：告警一次；同值再次出现不重复告警（模块级集合去重）
      const first = mk(unknown, 'call-u1')
      const second = mk(unknown, 'call-u2')

      expect(first.parts?.some((p) => p.type === 'tool_call')).toBe(true)
      expect(warnSpy).toHaveBeenCalledTimes(1)
      expect(warnSpy).toHaveBeenCalledWith(
        `[session] 未知工具调用状态 "${unknown}"，按 pending 渲染`,
      )
      expect(second.parts?.length).toBeGreaterThan(0)
      expect(warnSpy).toHaveBeenCalledTimes(1)
    })

    it('思考内容：reasoningContent 优先，metadata.thinkingContent 兜底，空白思考不产 part', () => {
      const viaReasoning = mapBackendMessageToMessage(
        backendMessage({ role: 'assistant', content: '答', reasoningContent: '思考中' }),
        's1',
      )
      expect(viaReasoning.thinking).toEqual({ content: '思考中', isThinking: false })
      expect(viaReasoning.parts?.[0]).toMatchObject({ type: 'thinking', sequence: 0 })
      expect(viaReasoning.parts?.[1]).toMatchObject({ type: 'text', content: '答', sequence: 1 })

      const viaMetadata = mapBackendMessageToMessage(
        backendMessage({
          role: 'assistant',
          content: '答',
          metadata: { thinkingContent: '元数据思考' },
        }),
        's1',
      )
      expect(viaMetadata.thinking).toEqual({ content: '元数据思考', isThinking: false })

      const blank = mapBackendMessageToMessage(
        backendMessage({ role: 'assistant', content: '答', reasoningContent: '   ' }),
        's1',
      )
      expect(blank.parts?.some((p) => p.type === 'thinking')).toBe(false)
    })

    it('空内容且无思考无工具调用的消息：parts 缺省（不产空数组）', () => {
      const msg = mapBackendMessageToMessage(
        backendMessage({ role: 'assistant', content: '' }),
        's1',
      )
      expect(msg.parts).toBeUndefined()
      expect(msg.content).toBe('')
    })
  })

  describe('mergeConsecutiveAssistantMessages 边界分支', () => {
    it('合并组内全部为空（无内容无 parts）→ 原样保留各条消息', () => {
      const a1 = assistantMsg('a1', '')
      const a2 = assistantMsg('a2', '')

      const result = mergeConsecutiveAssistantMessages([a1, a2])

      expect(result).toEqual([a1, a2])
    })

    it('toolCallId 无匹配 tool_call part / 缺 toolCallId：不注入也不炸，组仍合并为一个气泡', () => {
      const assistant = assistantMsg('a1', '回答', [
        { type: 'text', content: '回答', state: 'done', sequence: 0 },
      ])
      const toolNoMatch = {
        id: 't1',
        sessionId: 's1',
        role: 'tool' as const,
        content: '结果',
        timestamp: TS,
        toolCallId: 'call-not-exist',
      }
      const toolNoId = {
        id: 't2',
        sessionId: 's1',
        role: 'tool' as const,
        content: '结果2',
        timestamp: TS,
      }

      const merged = mergeConsecutiveAssistantMessages([assistant, toolNoMatch])
      expect(merged).toHaveLength(1)
      expect(merged[0].role).toBe('assistant')
      expect(merged[0].content).toBe('回答')
      expect(merged[0].parts).toHaveLength(1)

      const merged2 = mergeConsecutiveAssistantMessages([assistant, toolNoId])
      expect(merged2).toHaveLength(1)
      expect(merged2[0].role).toBe('assistant')
    })
  })
})
