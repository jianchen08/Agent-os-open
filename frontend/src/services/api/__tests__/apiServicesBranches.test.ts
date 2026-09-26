/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * API 服务分支补测（files / pipelines / memory / auth）
 *
 * 既有测试只覆盖各服务的部分出口；本文件补齐：
 * - files：uploadFile（FormData 与可选 model_name）、getModelCapabilities 的
 *   404 降级与其它错误上抛；
 * - pipelines：runs/state 列表查询与查询串拼装、catalog 两接口 join 全分支、
 *   pending 输入队列四个动作；
 * - memory：getEpisodes/getSemanticMemory/getMemoryStats/searchHindsight
 *   的字段归一化与缺省分支；
 * - auth：changePassword 验旧口令 + 新口令校验 + 请求体；logout 的
 *   refresh_token/logout_all 组合。
 *
 * apiClient 是外部依赖（网络层），用可控 mock 替换并断言请求契约；
 * 返回值映射断言可观察输出。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getMock = vi.fn()
const postMock = vi.fn()
const putMock = vi.fn()
const patchMock = vi.fn()
const deleteMock = vi.fn()

vi.mock('../client', () => {
  const mockClient = {
    get: (...args: unknown[]) => getMock(...args),
    post: (...args: unknown[]) => postMock(...args),
    put: (...args: unknown[]) => putMock(...args),
    patch: (...args: unknown[]) => patchMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  }
  return { default: mockClient, apiClient: mockClient }
})

import { API_ENDPOINTS } from '@/constants/api'
import * as authApi from '../auth'
import { ARTIFACTS_ENDPOINTS, HINDSIGHT_MEMORY_SERVICE_ENDPOINTS, MULTIMODAL_SERVICE_ENDPOINTS } from '../endpoints.generated'
import * as filesApi from '../files'
import * as memoryApi from '../memory'
import * as pipelinesApi from '../pipelines'

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ── files.ts ────────────────────────────────────────────────

describe('files API — uploadFile', () => {
  const uploadResponse = {
    file_id: 'f1',
    filename: 'a.txt',
    mime_type: 'text/plain',
    media_type: 'document',
    size: 3,
    url: '/uploads/a.txt',
  }

  it('无 modelName 时 FormData 只含 file，且走 artifacts 端点 + 60s 超时', async () => {
    postMock.mockResolvedValue({ data: uploadResponse })
    const file = new File(['abc'], 'a.txt', { type: 'text/plain' })

    const res = await filesApi.uploadFile(file)

    expect(res).toEqual(uploadResponse)
    const [url, body, config] = postMock.mock.calls[0]
    expect(url).toBe(ARTIFACTS_ENDPOINTS.artifacts_upload)
    expect(body).toBeInstanceOf(FormData)
    expect((body as FormData).get('file')).toBe(file)
    expect((body as FormData).get('model_name')).toBeNull()
    expect(config).toMatchObject({ timeout: 60000 })
  })

  it('带 modelName 时 FormData 追加 model_name 字段', async () => {
    postMock.mockResolvedValue({ data: uploadResponse })
    const file = new File(['abc'], 'a.txt', { type: 'text/plain' })

    await filesApi.uploadFile(file, 'glm-5.2')

    const body = postMock.mock.calls[0][1] as FormData
    expect(body.get('model_name')).toBe('glm-5.2')
    expect(body.get('file')).toBe(file)
  })

  it('上传失败时错误向上传播（不吞异常）', async () => {
    postMock.mockRejectedValue(new Error('413 too large'))
    await expect(
      filesApi.uploadFile(new File(['x'], 'x.txt', { type: 'text/plain' })),
    ).rejects.toThrow('413 too large')
  })
})

describe('files API — getModelCapabilities', () => {
  it('成功时原样返回能力声明', async () => {
    const caps = {
      model_name: 'glm-5.2',
      supports_image: true,
      supported_image_types: ['image/png'],
      max_image_size: 1024,
    }
    getMock.mockResolvedValue({ data: caps })

    const res = await filesApi.getModelCapabilities('glm-5.2')

    expect(res).toEqual(caps)
    expect(getMock).toHaveBeenCalledWith(
      MULTIMODAL_SERVICE_ENDPOINTS.mm_files_capabilities,
      { params: { model_name: 'glm-5.2' } },
    )
  })

  it('404 降级为全 False 能力（端点不存在不阻断文本附件）', async () => {
    const notFound = Object.assign(new Error('Not Found'), { response: { status: 404 } })
    getMock.mockRejectedValue(notFound)

    const res = await filesApi.getModelCapabilities('unknown-model')

    expect(res).toMatchObject({
      model_name: 'unknown-model',
      supports_image: false,
      supported_image_types: [],
      max_image_size: 0,
      supports_audio: false,
      supports_video: false,
      is_multimodal: false,
    })
  })

  it.each([500, 400, 401])('非 404 状态码（%i）原样抛出', async (status) => {
    const err = Object.assign(new Error(`HTTP ${status}`), { response: { status } })
    getMock.mockRejectedValue(err)
    await expect(filesApi.getModelCapabilities('m')).rejects.toThrow(`HTTP ${status}`)
  })

  it('无 response 字段的错误（网络中断）原样抛出', async () => {
    getMock.mockRejectedValue(new Error('network down'))
    await expect(filesApi.getModelCapabilities('m')).rejects.toThrow('network down')
  })
})

// ── pipelines.ts ────────────────────────────────────────────

describe('files API — validateFile 多模态视频分支', () => {
  const makeFile = (name: string, type: string): File => new File(['x'], name, { type })
  const videoCap = {
    modelName: 'glm-5.2',
    supportsImage: false,
    supportedImageTypes: [],
    maxImageSize: 0,
    supportsAudio: false,
    supportedAudioTypes: [],
    maxAudioSize: 0,
    supportsVideo: true,
    supportedVideoTypes: ['video/mp4'],
    maxVideoSize: 10 * 1024 * 1024,
    isMultimodal: true,
  }

  it('模型不支持视频时拒绝视频文件', () => {
    const res = filesApi.validateFile(makeFile('a.mp4', 'video/mp4'), {
      ...videoCap,
      supportsVideo: false,
    })
    expect(res).toMatchObject({ valid: false, error: '当前模型不支持视频输入' })
  })

  it('视频 MIME 不在声明清单内时拒绝（带具体 MIME）', () => {
    const res = filesApi.validateFile(makeFile('a.avi', 'video/x-msvideo'), videoCap)
    expect(res.valid).toBe(false)
    expect(res.error).toContain('不支持的视频类型: video/x-msvideo')
  })

  it('视频 MIME 在清单内且未超限时放行', () => {
    expect(filesApi.validateFile(makeFile('a.mp4', 'video/mp4'), videoCap).valid).toBe(true)
  })

  it('音频 MIME 不在声明清单内时拒绝（音频类型分支）', () => {
    const audioCap = { ...videoCap, supportsAudio: true, supportedAudioTypes: ['audio/mpeg'] }
    const res = filesApi.validateFile(makeFile('a.wav', 'audio/wav'), audioCap)
    expect(res.valid).toBe(false)
    expect(res.error).toContain('不支持的音频类型: audio/wav')
  })

  it('maxSize 为 0 时不限体积（跳过大小校验）', () => {
    const noLimit = { ...videoCap, maxVideoSize: 0 }
    const big = makeFile('big.mp4', 'video/mp4')
    Object.defineProperty(big, 'size', { value: 999 * 1024 * 1024, configurable: true })
    expect(filesApi.validateFile(big, noLimit).valid).toBe(true)
  })
})

describe('pipelines API — fetchPipelineRuns', () => {
  it('无参数时不拼查询串（URL 无 ?）', async () => {
    getMock.mockResolvedValue({ data: { items: [{ id: 'r1' }] } })
    const res = await pipelinesApi.fetchPipelineRuns()
    expect(res).toEqual([{ id: 'r1' }])
    expect(getMock.mock.calls[0][0]).toBe(API_ENDPOINTS.PIPELINES.RUNS)
  })

  it('status + limit 同时给出时查询串含两者', async () => {
    getMock.mockResolvedValue({ data: { items: [] } })
    await pipelinesApi.fetchPipelineRuns({ status: 'running', limit: 25 })
    const url = getMock.mock.calls[0][0] as string
    expect(url).toContain(`${API_ENDPOINTS.PIPELINES.RUNS}?`)
    expect(url).toContain('status=running')
    expect(url).toContain('limit=25')
  })

  it('仅 status 时不含 limit 参数', async () => {
    getMock.mockResolvedValue({ data: { items: [] } })
    await pipelinesApi.fetchPipelineRuns({ status: 'failed' })
    const url = getMock.mock.calls[0][0] as string
    expect(url).toContain('status=failed')
    expect(url).not.toContain('limit')
  })

  it('响应缺 items 字段时返回空数组（不抛 TypeError）', async () => {
    getMock.mockResolvedValue({ data: {} })
    await expect(pipelinesApi.fetchPipelineRuns()).resolves.toEqual([])
  })
})

describe('pipelines API — state 摘要映射', () => {
  it('fetchPipelineStates 缺 items 时返回空数组', async () => {
    getMock.mockResolvedValue({ data: {} })
    await expect(pipelinesApi.fetchPipelineStates()).resolves.toEqual([])
  })

  it('fetchPipelineStates 走 state 端点并返回条目', async () => {
    getMock.mockResolvedValue({
      data: { items: [{ pipeline_id: 'p1', source: 'memory', state: { run_status: 'running' } }] },
    })
    const res = await pipelinesApi.fetchPipelineStates()
    expect(getMock.mock.calls[0][0]).toBe(API_ENDPOINTS.PIPELINES.STATE)
    expect(res[0].pipeline_id).toBe('p1')
  })

  it.each(['running', 'suspended', 'completed', 'failed', 'cancelled'] as const)(
    'run_status=%s 直映',
    (status) => {
      expect(pipelinesApi.resolvePipelineRunStatus({ run_status: status })).toBe(status)
    },
  )

  it('未知 run_status 回退 raw_error → failed', () => {
    expect(
      pipelinesApi.resolvePipelineRunStatus({ run_status: 'weird' as never, raw_error: 'boom' }),
    ).toBe('failed')
  })

  it('未知 run_status 且已结束 → completed', () => {
    expect(pipelinesApi.resolvePipelineRunStatus({ run_status: 'weird' as never, ended: true })).toBe(
      'completed',
    )
  })

  it('无 run_status/raw_error/ended 时推断为 running', () => {
    expect(pipelinesApi.resolvePipelineRunStatus({})).toBe('running')
  })

  it('ended=false 不被当作 completed（严格等值判定）', () => {
    expect(pipelinesApi.resolvePipelineRunStatus({ ended: false })).toBe('running')
  })

  it('视图模型：工作区路径按 task.ws_meta > ws_meta > workspace 优先级', () => {
    const all = pipelinesApi.mapStateSummaryToViewModel({
      'task.ws_meta': { path: '/task-ws' },
      ws_meta: { path: '/ws-meta' },
      workspace: '/plain',
    })
    expect(all.workspacePath).toBe('/task-ws')

    const mid = pipelinesApi.mapStateSummaryToViewModel({
      ws_meta: { path: '/ws-meta' },
      workspace: '/plain',
    })
    expect(mid.workspacePath).toBe('/ws-meta')

    const last = pipelinesApi.mapStateSummaryToViewModel({ workspace: '/plain' })
    expect(last.workspacePath).toBe('/plain')
  })

  it('视图模型：空串字段归一为 undefined（不伪造空值语义）', () => {
    const vm = pipelinesApi.mapStateSummaryToViewModel({
      'task.status': '',
      'lineage.origin_session_id': '',
      llm_model: '',
      display_name: '',
      name: '',
      workspace: '',
    })
    expect(vm.taskStatus).toBeUndefined()
    expect(vm.originSessionId).toBeUndefined()
    expect(vm.llmModel).toBeUndefined()
    expect(vm.displayName).toBeUndefined()
    expect(vm.name).toBeUndefined()
    expect(vm.workspacePath).toBeUndefined()
  })

  it('视图模型：contextWindow 仅在 number 时透出；ended 仅 true 时透出', () => {
    const vm = pipelinesApi.mapStateSummaryToViewModel({
      context_window: 128000,
      ended: true,
      raw_error: 'err',
      current_phase: 'main',
      message_count: 7,
      'track.llm_usage': { total_tokens: 100 },
    })
    expect(vm).toMatchObject({
      contextWindow: 128000,
      ended: true,
      rawError: 'err',
      currentPhase: 'main',
      messageCount: 7,
    })
    expect(vm.llmUsage).toEqual({ total_tokens: 100 })

    const noNum = pipelinesApi.mapStateSummaryToViewModel({
      context_window: '128000' as never,
      ended: false,
    })
    expect(noNum.contextWindow).toBeUndefined()
    expect(noNum.ended).toBeUndefined()
  })

  it('mapStateInfoToViewModel 带上条目级坐标并展开摘要', () => {
    const vm = pipelinesApi.mapStateInfoToViewModel({
      pipeline_id: 'p1',
      thread_id: 'th1',
      source: 'checkpoint',
      state: { run_status: 'completed', display_name: '显示名' },
    })
    expect(vm).toMatchObject({
      pipelineId: 'p1',
      threadId: 'th1',
      status: 'completed',
      displayName: '显示名',
    })
  })
})

describe('pipelines API — fetchPipelinePluginCatalog（两接口 join）', () => {
  it('仅 catalog 有的插件：enabled 为 null（三态未知，不默认已启用）', async () => {
    getMock
      .mockResolvedValueOnce({
        data: [{ id: 'p_only', name: '仅目录', version: '1.0', role: 'core', host_type: 'python' }],
      })
      .mockResolvedValueOnce({ data: [] })

    const res = await pipelinesApi.fetchPipelinePluginCatalog()

    expect(res).toEqual([
      {
        id: 'p_only',
        name: '仅目录',
        role: 'core',
        hostType: 'python',
        version: '1.0',
        enabled: null,
        configFiles: [],
      },
    ])
  })

  it('仅 status 有的 pipeline 插件：用 status 侧字段补位', async () => {
    getMock
      .mockResolvedValueOnce({ data: [] })
      .mockResolvedValueOnce({
        data: [
          {
            plugin_id: 'p_status',
            name: '仅状态',
            config_type: 'pipeline',
            host_type: 'python',
            version: null,
            enabled: false,
            config_files: [{ id: 'c1', label: 'C1', path: '/c1.yaml' }],
          },
        ],
      })

    const res = await pipelinesApi.fetchPipelinePluginCatalog()

    expect(res[0]).toMatchObject({
      id: 'p_status',
      name: '仅状态',
      role: null,
      hostType: 'python',
      version: null,
      enabled: false,
      configFiles: [{ id: 'c1', label: 'C1', path: '/c1.yaml' }],
    })
  })

  it('status 侧非 pipeline 类型（config_type=tool）被过滤', async () => {
    getMock
      .mockResolvedValueOnce({ data: [] })
      .mockResolvedValueOnce({
        data: [
          {
            plugin_id: 'tool_x',
            name: 'X',
            config_type: 'tool',
            host_type: 'python',
            version: '1',
            enabled: true,
            config_files: [],
          },
        ],
      })

    await expect(pipelinesApi.fetchPipelinePluginCatalog()).resolves.toEqual([])
  })

  it('两接口都有：catalog 优先提供 role/name/version，status 提供 enabled/config_files', async () => {
    getMock
      .mockResolvedValueOnce({
        data: [{ id: 'both', name: '目录名', version: '2.0', role: 'input', host_type: 'rust' }],
      })
      .mockResolvedValueOnce({
        data: [
          {
            plugin_id: 'both',
            name: '状态名',
            config_type: 'pipeline',
            host_type: 'python',
            version: '1.0',
            enabled: true,
            config_files: [{ id: 'cf', label: 'CF', path: '/cf.yaml' }],
          },
        ],
      })

    const res = await pipelinesApi.fetchPipelinePluginCatalog()

    expect(res).toEqual([
      {
        id: 'both',
        name: '目录名',
        role: 'input',
        hostType: 'rust',
        version: '2.0',
        enabled: true,
        configFiles: [{ id: 'cf', label: 'CF', path: '/cf.yaml' }],
      },
    ])
  })

  it('结果按 id 字典序排序（与注册顺序无关）', async () => {
    getMock
      .mockResolvedValueOnce({
        data: [
          { id: 'zzz', name: 'Z', version: null, role: null, host_type: 'python' },
          { id: 'aaa', name: 'A', version: null, role: null, host_type: 'python' },
        ],
      })
      .mockResolvedValueOnce({ data: [] })

    const res = await pipelinesApi.fetchPipelinePluginCatalog()
    expect(res.map((e) => e.id)).toEqual(['aaa', 'zzz'])
  })

  it('name 三级回退：catalog → status → id', async () => {
    getMock
      .mockResolvedValueOnce({ data: [{ id: 'no-name', version: null, role: null, host_type: 'h' }] })
      .mockResolvedValueOnce({
        data: [
          {
            plugin_id: 'no-name',
            name: undefined as unknown as string,
            config_type: 'pipeline',
            host_type: 'h2',
            version: null,
            enabled: true,
            config_files: [],
          },
        ],
      })

    const res = await pipelinesApi.fetchPipelinePluginCatalog()
    expect(res[0].name).toBe('no-name')
  })

  it('catalog 缺失时 hostType 取 status 侧，均缺则空串', async () => {
    getMock
      .mockResolvedValueOnce({ data: [] })
      .mockResolvedValueOnce({
        data: [
          {
            plugin_id: 'x',
            name: 'X',
            config_type: 'pipeline',
            host_type: undefined as unknown as string,
            version: null,
            enabled: true,
            config_files: [],
          },
        ],
      })

    const res = await pipelinesApi.fetchPipelinePluginCatalog()
    expect(res[0].hostType).toBe('')
  })

  it('任一接口失败即抛错（调用方降级为目录不可用）', async () => {
    getMock.mockResolvedValueOnce({ data: [] }).mockRejectedValueOnce(new Error('plugins down'))
    await expect(pipelinesApi.fetchPipelinePluginCatalog()).rejects.toThrow('plugins down')
  })
})

describe('pipelines API — pending 输入队列', () => {
  const item = {
    id: 'i1',
    pipeline_id: 'p1',
    content: 'c',
    source: 'user' as const,
    created_at: '2026-09-14T00:00:00Z',
  }

  it('fetchPendingInputs 走管道派生端点，缺 items 返回空数组', async () => {
    getMock.mockResolvedValueOnce({ data: { items: [item] } })
    await expect(pipelinesApi.fetchPendingInputs('p1')).resolves.toEqual([item])
    expect(getMock.mock.calls[0][0]).toBe(API_ENDPOINTS.PIPELINES.PENDING_INPUTS('p1'))

    getMock.mockResolvedValueOnce({ data: {} })
    await expect(pipelinesApi.fetchPendingInputs('p2')).resolves.toEqual([])
  })

  it('updatePendingInput 以 PUT 提交 {content} 到单条端点', async () => {
    putMock.mockResolvedValue({ data: undefined })
    await pipelinesApi.updatePendingInput('p1', 'i1', '新内容')
    expect(putMock).toHaveBeenCalledWith(API_ENDPOINTS.PIPELINES.PENDING_INPUT('p1', 'i1'), {
      content: '新内容',
    })
  })

  it('deletePendingInput 删除单条端点', async () => {
    deleteMock.mockResolvedValue({ data: undefined })
    await pipelinesApi.deletePendingInput('p1', 'i1')
    expect(deleteMock).toHaveBeenCalledWith(API_ENDPOINTS.PIPELINES.PENDING_INPUT('p1', 'i1'))
  })

  it('clearPendingInputs 删除集合端点', async () => {
    deleteMock.mockResolvedValue({ data: undefined })
    await pipelinesApi.clearPendingInputs('p1')
    expect(deleteMock).toHaveBeenCalledWith(API_ENDPOINTS.PIPELINES.PENDING_INPUTS('p1'))
  })

  it('删除失败时错误向上传播', async () => {
    deleteMock.mockRejectedValue(new Error('409 busy'))
    await expect(pipelinesApi.clearPendingInputs('p1')).rejects.toThrow('409 busy')
  })
})

// ── memory.ts ───────────────────────────────────────────────

describe('memory API — 列表与统计', () => {
  it('getEpisodes 默认 page=1/page_size=20 并透传响应', async () => {
    const payload = { items: [], total: 0, page: 1, page_size: 20 }
    getMock.mockResolvedValue({ data: payload })

    await expect(memoryApi.getEpisodes()).resolves.toEqual(payload)
    expect(getMock).toHaveBeenCalledWith(API_ENDPOINTS.MEMORY.EPISODES, {
      params: { page: 1, page_size: 20 },
    })
  })

  it('getEpisodes 自定义分页参数被采用', async () => {
    getMock.mockResolvedValue({ data: { items: [], total: 0, page: 3, page_size: 5 } })
    await memoryApi.getEpisodes(3, 5)
    expect(getMock).toHaveBeenCalledWith(API_ENDPOINTS.MEMORY.EPISODES, {
      params: { page: 3, page_size: 5 },
    })
  })

  it('getSemanticMemory 透传 items/total', async () => {
    const payload = { items: [{ id: 's1', content: 'c' }], total: 1 }
    getMock.mockResolvedValue({ data: payload })
    await expect(memoryApi.getSemanticMemory()).resolves.toEqual(payload)
    expect(getMock).toHaveBeenCalledWith(API_ENDPOINTS.MEMORY.SEMANTIC)
  })

  it('getMemoryStats 透传统计体', async () => {
    const stats = { episode_count: 1, knowledge_count: 2, total_count: 3, last_updated: 'x' }
    getMock.mockResolvedValue({ data: stats })
    await expect(memoryApi.getMemoryStats()).resolves.toEqual(stats)
    expect(getMock).toHaveBeenCalledWith(API_ENDPOINTS.MEMORY.STATS)
  })

  it('失败时错误向上传播（requestWithRetry 不吞）', async () => {
    getMock.mockRejectedValue(new Error('503 unavailable'))
    await expect(memoryApi.getMemoryStats()).rejects.toThrow('503 unavailable')
  })
})

describe('memory API — searchHindsight 字段归一化', () => {
  const recallUrl = `${HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.hindsight_recall}?query=q1&limit=10`

  it('query 与 limit 进查询串（URL 编码生效）', async () => {
    getMock.mockResolvedValue({ data: { results: [], total: 0 } })
    await memoryApi.searchHindsight('中文 空格', 3)
    const url = getMock.mock.calls[0][0] as string
    expect(url).toContain(`${HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.hindsight_recall}?`)
    expect(url).toContain(`query=${encodeURIComponent('中文 空格')}`)
    expect(url).toContain('limit=3')
  })

  it('content 优先取 content，缺省回退 text，均缺为空串', async () => {
    getMock.mockResolvedValue({
      data: {
        results: [{ id: 'r1', content: '正文' }, { id: 'r2', text: '文本' }, { id: 'r3' }],
        total: 3,
      },
    })

    const res = await memoryApi.searchHindsight('q1')

    expect(res.items.map((i) => i.content)).toEqual(['正文', '文本', ''])
    expect(res.query).toBe('q1')
    expect(res.total).toBe(3)
  })

  it('metadata.memory_type 缺省落 semantic，score 非数字落 0', async () => {
    getMock.mockResolvedValue({
      data: {
        results: [
          { id: 'r1', content: 'a', metadata: { memory_type: 'episodic' }, score: 0.9 },
          { id: 'r2', content: 'b' },
        ],
        total: 2,
      },
    })

    const res = await memoryApi.searchHindsight('q1')

    expect(res.items[0]).toMatchObject({ memory_type: 'episodic', score: 0.9 })
    expect(res.items[1]).toMatchObject({ memory_type: 'semantic', score: 0 })
    expect(res.items[1].metadata).toEqual({})
  })

  it('id 非字符串被 String() 归一；created_at 恒为空串（后端不供）', async () => {
    getMock.mockResolvedValue({ data: { results: [{ id: 42, content: 'c' }], total: 1 } })
    const res = await memoryApi.searchHindsight('q1')
    expect(res.items[0].id).toBe('42')
    expect(res.items[0].created_at).toBe('')
  })

  it('非对象结果条目被过滤（null/字符串）', async () => {
    getMock.mockResolvedValue({ data: { results: [null, 'oops', { id: 'ok', content: 'c' }], total: 3 } })
    const res = await memoryApi.searchHindsight('q1')
    expect(res.items).toHaveLength(1)
    expect(res.items[0].id).toBe('ok')
  })

  it('results 缺省时按空数组处理，total 回退 results.length', async () => {
    getMock.mockResolvedValue({ data: {} })
    const res = await memoryApi.searchHindsight('q1')
    expect(res.items).toEqual([])
    expect(res.total).toBe(0)
  })

  it('total 显式为 0 时保留 0（不因 falsy 被 length 覆盖）', async () => {
    getMock.mockResolvedValue({ data: { results: [{ id: 'x', content: 'c' }], total: 0 } })
    const res = await memoryApi.searchHindsight('q1')
    expect(res.total).toBe(0)
  })

  it('显式 total 大于 results 长度时以 total 为准', async () => {
    getMock.mockResolvedValue({ data: { results: [{ id: 'x', content: 'c' }], total: 99 } })
    const res = await memoryApi.searchHindsight('q1')
    expect(res.total).toBe(99)
  })

  it('top_k 缺省为 10', async () => {
    getMock.mockResolvedValue({ data: { results: [], total: 0 } })
    await memoryApi.searchHindsight('q1')
    expect(getMock.mock.calls[0][0]).toContain(`limit=10`)
    expect(getMock.mock.calls[0][0]).toBe(recallUrl.replace('query=q1', `query=q1`))
  })
})

// ── auth.ts ─────────────────────────────────────────────────

describe('auth API — changePassword', () => {
  it('旧口令为空抛 ValidationError（本地校验，不发请求）', async () => {
    await expect(authApi.changePassword('', 'new-password-1')).rejects.toThrow('旧口令不能为空')
    expect(postMock).not.toHaveBeenCalled()
  })

  it('新口令过短抛 ValidationError（复用 validatePassword）', async () => {
    await expect(authApi.changePassword('old-pass-1', 'short')).rejects.toThrow(
      '密码长度至少为8个字符',
    )
    expect(postMock).not.toHaveBeenCalled()
  })

  it('新口令为空白抛「密码不能为空」', async () => {
    await expect(authApi.changePassword('old-pass-1', '   ')).rejects.toThrow('密码不能为空')
  })

  it('合法输入 POST change-password 端点并带 old/new 字段，返回新 token 对', async () => {
    const resp = { access_token: 'at', refresh_token: 'rt', token_type: 'bearer' }
    postMock.mockResolvedValue({ data: resp })

    await expect(authApi.changePassword('old-pass-1', 'new-pass-1234')).resolves.toEqual(resp)

    expect(postMock).toHaveBeenCalledWith(API_ENDPOINTS.AUTH.CHANGE_PASSWORD, {
      old_password: 'old-pass-1',
      new_password: 'new-pass-1234',
    })
  })

  it('服务端拒绝时错误向上传播', async () => {
    postMock.mockRejectedValue(new Error('400 旧口令不正确'))
    await expect(authApi.changePassword('wrong-old1', 'new-pass-1234')).rejects.toThrow(
      '400 旧口令不正确',
    )
  })
})

describe('auth API — logout 参数组合', () => {
  it.each([
    [undefined, false],
    ['rt-value', true],
  ])('refreshToken=%s / logoutAll=%s 原样进请求体', async (token, all) => {
    postMock.mockResolvedValue({ data: { success: true } })
    await authApi.logout(token as string | undefined, all as boolean)
    expect(postMock).toHaveBeenCalledWith(API_ENDPOINTS.AUTH.LOGOUT, {
      refresh_token: token,
      logout_all: all,
    })
  })

  it('默认参数：无 refresh token、单设备登出', async () => {
    postMock.mockResolvedValue({ data: { success: true } })
    await authApi.logout()
    expect(postMock).toHaveBeenCalledWith(API_ENDPOINTS.AUTH.LOGOUT, {
      refresh_token: undefined,
      logout_all: false,
    })
  })
})

describe('auth API — refreshToken 请求头契约', () => {
  it('Authorization 置空串（拦截器据此不带 Bearer，避免刷新被 401）', async () => {
    postMock.mockResolvedValue({ data: { access_token: 'a' } })
    await authApi.refreshToken('valid-refresh-token')
    expect(postMock).toHaveBeenCalledWith(
      API_ENDPOINTS.AUTH.REFRESH_TOKEN,
      { refresh_token: 'valid-refresh-token' },
      { headers: { Authorization: '' } },
    )
  })

  it('过短 refresh token 抛错且不发请求', async () => {
    await expect(authApi.refreshToken('short')).rejects.toThrow('Refresh token格式不正确')
    expect(postMock).not.toHaveBeenCalled()
  })

  it('空白 refresh token 抛「不能为空」', async () => {
    await expect(authApi.refreshToken('   ')).rejects.toThrow('Refresh token不能为空')
  })
})

describe('auth API — 用户名校验边界', () => {
  it.each(['', '  ', 'ab'])('非法用户名 %j 在登录前被拦下', async (username) => {
    await expect(authApi.login(username, 'password-1234')).rejects.toThrow()
    expect(postMock).not.toHaveBeenCalled()
  })

  it('登录成功后用户名 trim 后进请求体', async () => {
    postMock.mockResolvedValue({ data: { access_token: 'a' } })
    await authApi.login('  username  ', 'password-1234')
    expect(postMock).toHaveBeenCalledWith(API_ENDPOINTS.AUTH.LOGIN, {
      username: 'username',
      password: 'password-1234',
    })
  })

  it('邮箱格式非法的注册在本地被拦下', async () => {
    await expect(authApi.register('username', 'password-1234', 'bad-email')).rejects.toThrow(
      '邮箱格式不正确',
    )
    expect(postMock).not.toHaveBeenCalled()
  })

  it('注册成功时 email 原样进请求体（trim 在 requestData 处执行）', async () => {
    postMock.mockResolvedValue({ data: { access_token: 'a' } })
    await authApi.register('username', 'password-1234', 'a@b.com')
    expect(postMock).toHaveBeenCalledWith(API_ENDPOINTS.AUTH.REGISTER, {
      username: 'username',
      password: 'password-1234',
      email: 'a@b.com',
    })
  })

  it('前后带空白的合法邮箱当前被校验拦下（校验作用于未 trim 原值）', async () => {
    // 现状契约：validateEmail 在 trim 之前执行，故 ' a@b.com ' 被拒；
    // 若改为先 trim 校验，本用例应改为断言通过（见回报中的疑似缺陷）
    await expect(
      authApi.register('username', 'password-1234', '  a@b.com  '),
    ).rejects.toThrow('邮箱格式不正确')
    expect(postMock).not.toHaveBeenCalled()
  })

  it('getCurrentUser 走 /auth/me 并返回用户体', async () => {
    const user = { id: 'u1', username: 'u', role: 'admin' }
    getMock.mockResolvedValue({ data: user })
    await expect(authApi.getCurrentUser()).resolves.toEqual(user)
    expect(getMock).toHaveBeenCalledWith(API_ENDPOINTS.AUTH.ME)
  })
})
