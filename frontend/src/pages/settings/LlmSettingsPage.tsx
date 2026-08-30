/**
 * LLM 模型配置页面
 *
 * 两个 Tab：
 * 1. 提供商与密钥（默认）——预置提供者分组展示，填入 API Key 即可用
 *    （自动写入 .env、热生效免重启）；支持拉取远端模型列表、勾选或
 *    自定义输入模型名直接写入配置；可编辑每提供者的并发/RPM（KeyPool 消费）。
 * 2. 模型——默认模型选择（chat/tiers/embedding）、模型列表管理、
 *    每模型采样参数编辑（default_params，PUT /llm/models/{id}）。
 */

import { useState, useEffect, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, Loader2, Plus, RefreshCw, Search, Trash2, X } from '@/assets/icons'
import { PageShell } from '@/components/shared/PageShell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Modal } from '@/components/ui/Modal'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { toast } from '@/components/ui/sonner'
import { queryKeys } from '@/services/query/queryKeys'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import {
  getLLMConfig,
  getProviderTypes,
  getRemoteModels,
  addModel,
  updateModel,
  deleteModel,
  addProvider,
  deleteProvider,
  updateProviderConfig,
  saveDefaults,
  type LLMConfigResponse,
  type ModelConfig,
  type ProviderConfig,
  type RemoteModel,
  type LLMDefaults,
} from '@/services/api/config'


/**
 * 从被 reject 的对象中提取后端错误消息。
 *
 * apiClient 的拦截器构造的是普通 ApiError 对象（非 Error 实例），
 * 因此不能用 instanceof Error，需直接读 .message 字段。
 */
const getApiMsg = (e: unknown, fallback = '操作失败'): string =>
  (e as { message?: string })?.message ?? fallback

/** 预置提供者分组（与 config/models/llm.yaml 预置清单对应；未列出的归「自定义」组） */
const PROVIDER_GROUP_DEFS: { label: string; providers: [string, string][] }[] = [
  {
    label: '国内',
    providers: [
      ['qwen', '通义千问'],
      ['moonshot', 'Kimi'],
      ['doubao', '豆包'],
      ['hunyuan', '混元'],
      ['qianfan', '千帆'],
      ['stepfun', '阶跃星辰'],
      ['spark', '讯飞星火'],
      ['modelscope', '魔搭'],
      ['huawei', '华为云 MaaS'],
      ['deepseek', 'DeepSeek'],
      ['minimax', 'MiniMax'],
      ['zhipu', '智谱'],
      ['zhipu_coding', '智谱 Coding'],
      ['siliconflow', '硅基流动'],
    ],
  },
  {
    label: '订阅 / 聚合',
    providers: [
      ['opencode', 'OpenCode Zen/Go'],
      ['openrouter', 'OpenRouter'],
    ],
  },
  {
    label: '国际',
    providers: [
      ['openai', 'OpenAI'],
      ['anthropic', 'Anthropic'],
      ['gemini', 'Gemini'],
      ['xai', 'xAI Grok'],
      ['mistral', 'Mistral'],
      ['groq', 'Groq'],
      ['perplexity', 'Perplexity'],
      ['cohere', 'Cohere'],
      ['together', 'Together'],
      ['fireworks', 'Fireworks'],
      ['deepinfra', 'DeepInfra'],
      ['cerebras', 'Cerebras'],
    ],
  },
  {
    label: '本地 / 测试',
    providers: [
      ['ollama', 'Ollama'],
      ['mock_llm', 'Mock LLM'],
    ],
  },
]

const PRESET_PROVIDER_IDS = new Set(PROVIDER_GROUP_DEFS.flatMap((g) => g.providers.map(([id]) => id)))

/** 常用类型置顶（其余来自 litellm 动态目录 getProviderTypes） */
const COMMON_PROVIDER_TYPES = ['openai', 'anthropic', 'deepseek', 'zai', 'minimax']

/**
 * LLM 配置页面组件
 */
export function LlmSettingsPage({ embedded = false }: { embedded?: boolean }) {
  const [config, setConfig] = useState<LLMConfigResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState('providers')

  // 拉取模型对话框的目标提供者
  const [fetchTarget, setFetchTarget] = useState<string | null>(null)

  // 默认模型草稿（Tab2「默认模型」段，随 config.defaults 同步）
  const [defaultsDraft, setDefaultsDraft] = useState<LLMDefaults>({
    chat: '',
    embedding: '',
    tiers: {},
  })

  // 新模型表单
  const [newModelId, setNewModelId] = useState('')
  const [newModelConfig, setNewModelConfig] = useState<ModelConfig>({
    provider: '',
    model_name: '',
    display_name: '',
  })

  // 新提供商表单
  const [newProviderId, setNewProviderId] = useState('')
  const [newProviderType, setNewProviderType] = useState('openai')
  const [newProviderApiBase, setNewProviderApiBase] = useState('')
  const [newProviderApiKey, setNewProviderApiKey] = useState('')
  // litellm 动态类型目录（展开自定义提供商表单时懒加载）
  const [providerTypes, setProviderTypes] = useState<string[]>(COMMON_PROVIDER_TYPES)

  // 加载配置（query 化）：重进设置页缓存秒开；apiClient 用绝对 baseURL 绕过
  // Vite 代理；生产环境前后端须同源或后端配置 CORS 头。
  const configQuery = useQuery({
    queryKey: queryKeys.llmConfig,
    queryFn: () => getLLMConfig(),
    staleTime: 60_000,
  })

  useEffect(() => {
    if (configQuery.data) {
      setConfig(configQuery.data)
    }
  }, [configQuery.data])

  useEffect(() => {
    if (configQuery.isPending) return
    setIsLoading(false)
    if (configQuery.isError) {
      console.error('[LlmSettingsPage] Failed to load LLM config:', configQuery.error)
      setLoadError('无法连接服务器，请检查网络后重试')
      setConfig({
        models: {},
        providers: {},
        defaults: { chat: '', embedding: '', tiers: {} },
      })
    }
  }, [configQuery.isPending, configQuery.isError, configQuery.error])

  // 默认模型草稿随配置同步（保存/增删模型后回显服务器最新值）
  useEffect(() => {
    if (!config) return
    const d = config.defaults ?? { chat: '', embedding: '', tiers: {} }
    setDefaultsDraft({
      chat: d.chat ?? '',
      embedding: d.embedding ?? '',
      tiers: d.tiers ?? {},
    })
  }, [config])

  const modelIds = config ? Object.keys(config.models ?? {}) : []
  const providerIds = config ? Object.keys(config.providers ?? {}) : []

  // 保存默认模型选择

  // 保存默认模型选择（chat/embedding/tiers 部分更新，PUT /llm/defaults）
  const handleSaveDefaults = useCallback(async () => {
    try {
      const defaults = await saveDefaults(defaultsDraft)
      setConfig((prev) => (prev ? { ...prev, defaults } : prev))
      toast.success('默认模型已保存')
    } catch (e) {
      toast.error('保存默认模型失败', { description: getApiMsg(e, '保存默认模型失败') })
    }
  }, [defaultsDraft])

  // 添加新模型
  const handleAddModel = useCallback(async () => {
    if (!newModelId.trim()) return
    try {
      const models = await addModel(newModelId.trim(), {
        ...newModelConfig,
        model_name: newModelConfig.model_name || newModelId.trim(),
      })
      setConfig((prev) => (prev ? { ...prev, models } : prev))
      setNewModelId('')
      setNewModelConfig({ provider: '', model_name: '', display_name: '' })
    } catch (e) {
      toast.error('添加模型失败', { description: getApiMsg(e, '添加模型失败') })
    }
  }, [newModelId, newModelConfig])

  // 删除模型
  const handleDeleteModel = useCallback(async (modelId: string) => {
    try {
      const models = await deleteModel(modelId)
      setConfig((prev) => (prev ? { ...prev, models } : prev))
    } catch (e) {
      toast.error('删除模型失败', { description: getApiMsg(e, '删除模型失败') })
    }
  }, [])

  // 保存模型设置（上下文窗口/推理标记/default_params——拉取或自定义添加的
  // 模型初始只有最小字段，上下文与 think 参数都靠这里补全）
  const handleSaveModelSettings = useCallback(
    async (modelId: string, settings: Partial<ModelConfig>) => {
      try {
        const models = await updateModel(modelId, settings)
        setConfig((prev) => (prev ? { ...prev, models } : prev))
        toast.success(`已保存 ${modelId} 的模型设置`)
      } catch (e) {
        toast.error('保存模型设置失败', { description: getApiMsg(e, '保存模型设置失败') })
      }
    },
    [],
  )

  // 填写 / 更新提供商 API Key（明文经后端写入 .env，yaml 保持 ${VAR} 占位）
  const handleUpdateApiKey = useCallback(
    async (providerId: string, apiKey: string, provider: ProviderConfig) => {
      if (!apiKey.trim()) return
      try {
        const firstKey = provider.keys?.[0]
        const entry: Record<string, unknown> = {
          id: firstKey?.id ?? `${providerId}_main`,
          api_key: apiKey.trim(),
        }
        const providers = await updateProviderConfig(providerId, { keys: [entry] })
        setConfig((prev) => (prev ? { ...prev, providers } : prev))
        toast.success(`已保存 ${providerId} 的 Key（写入 .env，立即生效）`)
      } catch (e) {
        toast.error('保存密钥失败', { description: getApiMsg(e, '保存密钥失败') })
      }
    },
    [],
  )

  // 保存提供者并发 / RPM（KeyPool 消费；不带 api_key，后端按条目合并保留占位符）
  const handleSaveConcurrency = useCallback(
    async (providerId: string, maxConcurrent: number, rpm: number) => {
      try {
        const firstKey = config?.providers[providerId]?.keys?.[0]
        const entry: Record<string, unknown> = {
          id: firstKey?.id ?? `${providerId}_main`,
          max_concurrent: maxConcurrent,
          rpm,
        }
        const providers = await updateProviderConfig(providerId, { keys: [entry] })
        setConfig((prev) => (prev ? { ...prev, providers } : prev))
        toast.success(`已保存 ${providerId} 的并发设置`)
      } catch (e) {
        toast.error('保存并发设置失败', { description: getApiMsg(e, '保存并发设置失败') })
      }
    },
    [config],
  )

  // 添加提供商
  const handleAddProvider = useCallback(async () => {
    if (!newProviderId.trim()) return
    try {
      const providerConfig: { type: string; api_base?: string; api_key?: string } = {
        type: newProviderType,
      }
      if (newProviderApiBase.trim()) providerConfig.api_base = newProviderApiBase.trim()
      if (newProviderApiKey.trim()) providerConfig.api_key = newProviderApiKey.trim()
      const providers = await addProvider(newProviderId.trim(), providerConfig)
      setConfig((prev) => (prev ? { ...prev, providers } : prev))
      setNewProviderId('')
      setNewProviderType('openai')
      setNewProviderApiBase('')
      setNewProviderApiKey('')
    } catch (e) {
      toast.error('添加提供商失败', { description: getApiMsg(e, '添加提供商失败') })
    }
  }, [newProviderId, newProviderType, newProviderApiBase, newProviderApiKey])

  // 删除提供商
  const handleDeleteProvider = useCallback(async (providerId: string) => {
    try {
      const providers = await deleteProvider(providerId)
      setConfig((prev) => (prev ? { ...prev, providers } : prev))
    } catch (e) {
      toast.error('删除提供商失败', { description: getApiMsg(e, '删除提供商失败') })
    }
  }, [])

  // 拉取模型对话框：添加所选（勾选 + 自定义输入）
  const handleAddRemoteModels = useCallback(
    async (providerId: string, modelNames: string[]) => {
      let lastModels: Record<string, ModelConfig> | null = null
      const failed: string[] = []
      for (const name of modelNames) {
        try {
          const models = await addModel(name, {
            provider: providerId,
            model_name: name,
            display_name: name,
          })
          lastModels = models
        } catch {
          failed.push(name)
        }
      }
      if (lastModels) setConfig((prev) => (prev ? { ...prev, models: lastModels! } : prev))
      if (failed.length > 0) {
        toast.warning(`已添加 ${modelNames.length - failed.length} 个模型`, {
          description: `添加失败（可能已存在）: ${failed.join(', ')}`,
        })
      } else {
        toast.success(`已添加 ${modelNames.length} 个模型到 ${providerId}`)
      }
    },
    [],
  )

  // 展开自定义提供商表单时懒加载 litellm 动态类型目录
  const handleCustomFormToggle = useCallback((open: boolean) => {
    if (!open || providerTypes.length > COMMON_PROVIDER_TYPES.length) return
    getProviderTypes()
      .then(({ types }) => setProviderTypes(types))
      .catch(() => setProviderTypes(COMMON_PROVIDER_TYPES))
  }, [providerTypes.length])

  if (isLoading) {
    return (
      <PageShell title="模型设置" description="配置大语言模型提供商与模型" embedded={embedded} backHref="/settings" backLabel="设置" maxWidth="max-w-3xl">
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <div className="border-primary mr-2 h-5 w-5 animate-spin rounded-full border-2 border-t-transparent" />
          加载配置...
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell title="模型设置" description="配置大语言模型提供商与模型" embedded={embedded} mainLabel="模型设置表单">
      {loadError && (
        <div className="mb-4 flex items-center justify-between rounded-lg bg-destructive/10 px-4 py-3">
          <div>
            <p className="text-sm font-medium text-destructive">{loadError}</p>
            <p className="mt-0.5 text-xs text-destructive/80">
              模型列表为空，下拉选项将无可用内容。请重试或检查后端服务是否正常运行。
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void configQuery.refetch()}
            className="ml-4 shrink-0 border-destructive/30 text-destructive hover:bg-destructive/10"
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            重试
          </Button>
        </div>
      )}

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="providers">提供商与密钥</TabsTrigger>
          <TabsTrigger value="models">模型</TabsTrigger>
        </TabsList>

        {/* ── Tab 1：提供商与密钥 ── */}
        <TabsContent value="providers">
          <div className="mt-4 space-y-5">
            <p className="text-muted-foreground text-xs">
              预置提供者只需填入 API Key 即可使用——Key 自动写入 .env 并热生效（无需重启）。
              填好后可「拉取模型」获取该 Key 可用的模型列表，勾选或手动输入模型名即可添加。
            </p>

            {providerIds.length === 0 ? (
              <div className="text-muted-foreground py-4 text-center text-sm">暂无提供商</div>
            ) : (
              (() => {
                const entries = Object.entries(config!.providers ?? {})
                // 未配置 Key 的排前面，引导先完成配置
                const byId = new Map(entries)
                const sortEntries = (list: [string, ProviderConfig][]) =>
                  [...list].sort((a, b) => Number(a[1].has_key ?? true) - Number(b[1].has_key ?? true))
                const groups = [
                  ...PROVIDER_GROUP_DEFS.map((g) => ({
                    label: g.label,
                    nameOf: Object.fromEntries(g.providers) as Record<string, string>,
                    entries: sortEntries(g.providers.map(([id]) => [id, byId.get(id)!] as [string, ProviderConfig]).filter(([, p]) => p)),
                  })),
                  {
                    label: '自定义',
                    nameOf: {},
                    entries: sortEntries(entries.filter(([id]) => !PRESET_PROVIDER_IDS.has(id))),
                  },
                ].filter((g) => g.entries.length > 0)

                return groups.map((group) => (
                  <section key={group.label}>
                    <h3 className="text-muted-foreground mb-2 text-xs font-semibold uppercase tracking-wide">
                      {group.label}（{group.entries.length}）
                    </h3>
                    <div className="space-y-3">
                      {group.entries.map(([id, provider]) => (
                        <ProviderCard
                          key={id}
                          providerId={id}
                          displayName={group.nameOf[id]}
                          provider={provider}
                          onUpdateKey={handleUpdateApiKey}
                          onDelete={handleDeleteProvider}
                          onFetchModels={setFetchTarget}
                          onSaveConcurrency={handleSaveConcurrency}
                        />
                      ))}
                    </div>
                  </section>
                ))
              })()
            )}

            {/* 添加自定义提供商 */}
            <div className="mt-2 border-t pt-4">
              <details onToggle={(e) => handleCustomFormToggle((e.target as HTMLDetailsElement).open)}>
                <summary className="cursor-pointer text-sm font-semibold select-none">
                  添加自定义提供商（类型来自 litellm，升级 litellm 后自动出现新提供者）
                </summary>
                <div className="mt-3 space-y-2">
                  <FieldRow label="提供商 ID" htmlFor="new-provider-id">
                    <Input
                      id="new-provider-id"
                      value={newProviderId}
                      onChange={(e) => setNewProviderId(e.target.value)}
                      placeholder="如: myproxy"
                    />
                  </FieldRow>
                  <FieldRow label="类型" htmlFor="new-provider-type">
                    <Select value={newProviderType} onValueChange={setNewProviderType}>
                      <SelectTrigger id="new-provider-type">
                        <SelectValue placeholder="选择类型" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          <SelectLabel>常用</SelectLabel>
                          {COMMON_PROVIDER_TYPES.map((t) => (
                            <SelectItem key={t} value={t}>{t}</SelectItem>
                          ))}
                        </SelectGroup>
                        <SelectGroup>
                          <SelectLabel>litellm 全部（{providerTypes.length}）</SelectLabel>
                          {providerTypes
                            .filter((t) => !COMMON_PROVIDER_TYPES.includes(t))
                            .map((t) => (
                              <SelectItem key={t} value={t}>{t}</SelectItem>
                            ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </FieldRow>
                  <FieldRow label="API Base" htmlFor="new-provider-apibase">
                    <Input
                      id="new-provider-apibase"
                      value={newProviderApiBase}
                      onChange={(e) => setNewProviderApiBase(e.target.value)}
                      placeholder="选填，留空使用该类型默认端点（OpenAI 兼容端点必填）"
                    />
                  </FieldRow>
                  <FieldRow label="API Key" htmlFor="new-provider-apikey">
                    <Input
                      id="new-provider-apikey"
                      type="password"
                      value={newProviderApiKey}
                      onChange={(e) => setNewProviderApiKey(e.target.value)}
                      placeholder="输入 API Key（自动写入 .env）"
                    />
                  </FieldRow>
                  <Button size="sm" onClick={handleAddProvider} disabled={!newProviderId.trim()}>
                    <Plus className="mr-1.5 h-3.5 w-3.5" />
                    添加提供商
                  </Button>
                </div>
              </details>
            </div>
          </div>
        </TabsContent>

        {/* ── Tab 2：模型 ── */}
        <TabsContent value="models">
          <div className="mt-4 space-y-6">
            {/* 默认模型：chat/tiers/embedding 选择，保存写 llm.yaml defaults 段 */}
            <section className="rounded-lg border p-4">
              <h3 className="mb-1 text-sm font-semibold">默认模型</h3>
              <p className="text-muted-foreground mb-3 text-xs leading-relaxed">
                默认对话/分级/向量模型——模型须已在下文「已注册模型」列表中；被删模型在保存前保留原值不覆盖。
              </p>
              <div className="space-y-2">
                <DefaultModelSelect
                  id="default-chat"
                  label="默认对话模型"
                  value={defaultsDraft.chat}
                  models={modelIds}
                  onChange={(v) => setDefaultsDraft((p) => ({ ...p, chat: v }))}
                />
                {['large', 'medium', 'small'].map((tier) => (
                  <DefaultModelSelect
                    key={tier}
                    id={`default-tier-${tier}`}
                    label={`${tier} 档位`}
                    value={defaultsDraft.tiers[tier] ?? ''}
                    models={modelIds}
                    onChange={(v) =>
                      setDefaultsDraft((p) => ({ ...p, tiers: { ...p.tiers, [tier]: v } }))
                    }
                  />
                ))}
                <DefaultModelSelect
                  id="default-embedding"
                  label="默认向量模型"
                  value={defaultsDraft.embedding}
                  models={modelIds}
                  onChange={(v) => setDefaultsDraft((p) => ({ ...p, embedding: v }))}
                />
                <div className="flex items-center gap-2 pt-1">
                  <Button size="sm" onClick={handleSaveDefaults}>
                    保存默认模型
                  </Button>
                </div>
              </div>
            </section>

            {/* 模型列表 */}
            <section className="border-t pt-4">
              <h3 className="mb-3 text-sm font-semibold">已注册模型 ({modelIds.length})</h3>
              <p className="text-muted-foreground mb-3 text-xs">
                展开「参数」可设置上下文窗口、最大输出 tokens、temperature/top_p 与 think 参数（思考模式/推理力度）。
              </p>
              {modelIds.length === 0 ? (
                <div className="text-muted-foreground py-4 text-center text-sm">
                  暂无模型——到「提供商与密钥」页拉取或手动添加
                </div>
              ) : (
                <div className="space-y-2">
                  {modelIds.map((id) => (
                    <ModelRow
                      key={id}
                      modelId={id}
                      model={config!.models[id]}
                      onDelete={handleDeleteModel}
                      onSaveSettings={handleSaveModelSettings}
                    />
                  ))}
                </div>
              )}
            </section>

            {/* 手动添加模型 */}
            <section className="border-t pt-4">
              <h3 className="mb-3 text-sm font-semibold">添加模型</h3>
              <div className="space-y-2">
                <FieldRow label="模型 ID" htmlFor="new-model-id">
                  <Input
                    id="new-model-id"
                    value={newModelId}
                    onChange={(e) => setNewModelId(e.target.value)}
                    placeholder="如: gpt-5.1"
                  />
                </FieldRow>
                <FieldRow label="提供商" htmlFor="new-model-provider">
                  <Select
                    value={newModelConfig.provider}
                    onValueChange={(v) =>
                      setNewModelConfig((prev) => ({ ...prev, provider: v }))
                    }
                  >
                    <SelectTrigger id="new-model-provider">
                      <SelectValue placeholder="选择提供商" />
                    </SelectTrigger>
                    <SelectContent>
                      {providerIds.map((id) => (
                        <SelectItem key={id} value={id}>{id}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FieldRow>
                <FieldRow label="模型名称" htmlFor="new-model-name">
                  <Input
                    id="new-model-name"
                    value={newModelConfig.model_name}
                    onChange={(e) =>
                      setNewModelConfig((prev) => ({ ...prev, model_name: e.target.value }))
                    }
                    placeholder="留空则与模型 ID 相同"
                  />
                </FieldRow>
                <FieldRow label="显示名称" htmlFor="new-model-display">
                  <Input
                    id="new-model-display"
                    value={newModelConfig.display_name}
                    onChange={(e) =>
                      setNewModelConfig((prev) => ({ ...prev, display_name: e.target.value }))
                    }
                    placeholder="如: GPT-5.1"
                  />
                </FieldRow>
                <FieldRow label="上下文窗口" htmlFor="new-model-ctx">
                  <Input
                    id="new-model-ctx"
                    type="number"
                    min={0}
                    value={newModelConfig.context_window ?? ''}
                    onChange={(e) =>
                      setNewModelConfig((prev) => ({
                        ...prev,
                        context_window: e.target.value ? Number(e.target.value) : undefined,
                      }))
                    }
                    placeholder="如: 128000"
                  />
                </FieldRow>
                <FieldRow label="推理模型" htmlFor="new-model-reasoning">
                  <div className="flex items-center pt-2">
                    <input
                      id="new-model-reasoning"
                      type="checkbox"
                      checked={newModelConfig.reasoning_model ?? false}
                      onChange={(e) =>
                        setNewModelConfig((prev) => ({ ...prev, reasoning_model: e.target.checked }))
                      }
                      className="border-border h-4 w-4 rounded"
                    />
                    <span className="text-muted-foreground ml-2 text-xs">
                      勾选表示该模型支持 thinking/reasoning 能力
                    </span>
                  </div>
                </FieldRow>
                <Button size="sm" onClick={handleAddModel} disabled={!newModelId.trim() || !newModelConfig.provider}>
                  添加模型
                </Button>
              </div>
            </section>
          </div>
        </TabsContent>
      </Tabs>

      {/* 拉取模型对话框 */}
      <FetchModelsModal
        open={fetchTarget !== null}
        providerId={fetchTarget}
        onClose={() => setFetchTarget(null)}
        onAdd={handleAddRemoteModels}
      />
    </PageShell>
  )
}

/** 提供商卡片：填 Key / 更新 Key / 拉取模型 / 并发设置 / 删除 */
function ProviderCard({
  providerId,
  displayName,
  provider,
  onUpdateKey,
  onDelete,
  onFetchModels,
  onSaveConcurrency,
}: {
  providerId: string
  displayName?: string
  provider: ProviderConfig
  onUpdateKey: (id: string, key: string, provider: ProviderConfig) => void
  onDelete: (id: string) => void
  onFetchModels: (id: string) => void
  onSaveConcurrency: (id: string, maxConcurrent: number, rpm: number) => void
}) {
  const [apiKey, setApiKey] = useState('')
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [maxConcurrent, setMaxConcurrent] = useState(provider.keys?.[0]?.max_concurrent ?? 6)
  const [rpm, setRpm] = useState(provider.keys?.[0]?.rpm ?? 10)

  // has_key 由后端按 ${VAR} 能否解析出真实 key 判定；后端未提供该字段时退回非空判断
  const hasKey = provider.has_key ?? Boolean(provider.keys?.[0]?.api_key)
  const firstKey = provider.keys?.[0]
  const maskedKey = firstKey?.api_key ?? '未设置'
  const needsNoKey = providerId === 'ollama'

  return (
    <div className="bg-card space-y-2 rounded-lg border px-4 py-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-semibold">
            {displayName ?? providerId}
          </span>
          {displayName && (
            <span className="text-muted-foreground truncate font-mono text-xs">{providerId}</span>
          )}
          {provider.type && (
            <span className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 text-[10px] font-mono">
              {provider.type}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span
            className={`rounded px-2 py-0.5 text-xs ${hasKey || needsNoKey ? 'bg-status-success/10 text-status-success' : 'bg-status-warning/10 text-status-warning'}`}
          >
            {needsNoKey ? '无需 Key' : hasKey ? '已配置' : '未配置'}
          </span>
          <Button variant="destructive" size="xs" onClick={() => onDelete(providerId)}>
            <Trash2 className="mr-1 h-3 w-3" />
            删除
          </Button>
        </div>
      </div>
      {provider.api_base && (
        <div className="text-muted-foreground text-xs">Base URL: {provider.api_base}</div>
      )}

      {/* Key 配置：未配置时直接内嵌输入；已配置显示掩码 + 更新入口 */}
      {needsNoKey ? null : hasKey ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-muted-foreground font-mono text-xs">Key: {maskedKey}</span>
          <Input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="输入新的 API Key"
            className="h-7 w-56 text-xs"
          />
          <Button
            size="xs"
            disabled={!apiKey.trim()}
            onClick={() => {
              onUpdateKey(providerId, apiKey, provider)
              setApiKey('')
            }}
          >
            更新 Key
          </Button>
        </div>
      ) : (
        <div className="space-y-1.5">
          {provider.env_var && (
            <p className="text-muted-foreground text-xs">
              在 .env 设置 <code className="bg-muted rounded px-1 font-mono">{provider.env_var}</code>，或直接填入：
            </p>
          )}
          <div className="flex items-center gap-2">
            <Input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="填入 API Key（自动写入 .env，立即生效）"
              className="h-7 flex-1 text-xs"
              aria-label={`填写 ${providerId} 的 API Key`}
            />
            <Button
              size="xs"
              disabled={!apiKey.trim()}
              onClick={() => {
                onUpdateKey(providerId, apiKey, provider)
                setApiKey('')
              }}
            >
              保存
            </Button>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="xs"
          variant="outline"
          onClick={() => onFetchModels(providerId)}
          disabled={!hasKey && !needsNoKey}
        >
          <RefreshCw className="mr-1 h-3 w-3" />
          拉取模型
        </Button>
        <Button size="xs" variant="ghost" onClick={() => setShowAdvanced((v) => !v)}>
          <ChevronDown className={`mr-1 h-3 w-3 transition-transform ${showAdvanced ? 'rotate-180' : ''}`} />
          并发设置
        </Button>
      </div>

      {showAdvanced && (
        <div className="border-t pt-2">
          <div className="flex flex-wrap items-end gap-3">
            <label className="text-muted-foreground flex flex-col gap-1 text-xs">
              最大并发
              <Input
                type="number"
                min={1}
                value={maxConcurrent}
                onChange={(e) => setMaxConcurrent(Number(e.target.value))}
                className="h-7 w-24 text-xs"
              />
            </label>
            <label className="text-muted-foreground flex flex-col gap-1 text-xs">
              RPM（每分钟请求数）
              <Input
                type="number"
                min={0}
                value={rpm}
                onChange={(e) => setRpm(Number(e.target.value))}
                className="h-7 w-24 text-xs"
              />
            </label>
            <Button size="xs" onClick={() => onSaveConcurrency(providerId, maxConcurrent, rpm)}>
              保存并发设置
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

/** 拉取模型对话框：远端列表勾选 + 自定义输入，批量写入 llm.yaml */
function FetchModelsModal({
  open,
  providerId,
  onClose,
  onAdd,
}: {
  open: boolean
  providerId: string | null
  onClose: () => void
  onAdd: (providerId: string, modelNames: string[]) => Promise<void>
}) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [models, setModels] = useState<RemoteModel[]>([])
  const [search, setSearch] = useState('')
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [customs, setCustoms] = useState<string[]>([])
  const [customInput, setCustomInput] = useState('')
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    if (!open || !providerId) return
    setLoading(true)
    setError(null)
    setModels([])
    setSearch('')
    setChecked(new Set())
    setCustoms([])
    setCustomInput('')
    getRemoteModels(providerId)
      .then(({ models: remote }) => setModels(remote))
      .catch((e) => setError(getApiMsg(e, '拉取模型列表失败')))
      .finally(() => setLoading(false))
  }, [open, providerId])

  const filtered = models.filter((m) => m.id.toLowerCase().includes(search.toLowerCase()))
  const pendingCount = checked.size + customs.length

  const toggle = (id: string) => {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const addCustom = () => {
    const v = customInput.trim()
    if (!v) return
    if (!customs.includes(v) && !checked.has(v)) setCustoms((prev) => [...prev, v])
    setCustomInput('')
  }

  const handleAdd = async () => {
    if (!providerId || pendingCount === 0) return
    setAdding(true)
    try {
      await onAdd(providerId, [...checked, ...customs])
      onClose()
    } finally {
      setAdding(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={`拉取模型 — ${providerId ?? ''}`} maxWidth="lg">
      <div className="space-y-3">
        {loading && (
          <div className="text-muted-foreground flex items-center justify-center py-8 text-sm">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            正在从提供者 API 拉取模型列表...
          </div>
        )}
        {error && (
          <div className="rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
            {error}
            <p className="text-muted-foreground mt-1">仍可在下方手动输入模型名添加。</p>
          </div>
        )}

        {!loading && !error && (
          <>
            <div className="relative">
              <Search className="text-muted-foreground absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={`搜索 ${models.length} 个模型...`}
                className="h-8 pl-8 text-xs"
              />
            </div>
            <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg border p-2">
              {filtered.length === 0 ? (
                <div className="text-muted-foreground py-4 text-center text-xs">
                  没有匹配的模型，可在下方手动输入
                </div>
              ) : (
                filtered.map((m) => (
                  <label
                    key={m.id}
                    className="hover:bg-muted/50 flex cursor-pointer items-center gap-2 rounded px-2 py-1"
                  >
                    <input
                      type="checkbox"
                      checked={checked.has(m.id)}
                      onChange={() => toggle(m.id)}
                      className="border-border h-3.5 w-3.5"
                    />
                    <span className="text-xs">{m.id}</span>
                    {m.owned_by && (
                      <span className="text-muted-foreground ml-auto text-[10px]">{m.owned_by}</span>
                    )}
                  </label>
                ))
              )}
            </div>
          </>
        )}

        {/* 自定义输入：列表里没有的模型手动加 */}
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <Input
              value={customInput}
              onChange={(e) => setCustomInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  addCustom()
                }
              }}
              placeholder="自定义模型名（回车添加，适用于列表未包含的新模型）"
              className="h-8 text-xs"
            />
            <Button size="xs" variant="outline" onClick={addCustom} disabled={!customInput.trim()}>
              加入
            </Button>
          </div>
          {customs.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {customs.map((name) => (
                <span
                  key={name}
                  className="bg-muted flex items-center gap-1 rounded px-2 py-0.5 font-mono text-xs"
                >
                  {name}
                  <button
                    type="button"
                    onClick={() => setCustoms((prev) => prev.filter((n) => n !== name))}
                    className="text-muted-foreground hover:text-foreground"
                    aria-label={`移除 ${name}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t pt-3">
          <span className="text-muted-foreground mr-auto text-xs">
            待添加 {pendingCount} 个；添加后可在「模型」页展开参数设置上下文/think
          </span>
          <Button size="sm" variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button size="sm" onClick={handleAdd} disabled={pendingCount === 0 || adding}>
            {adding ? (
              <>
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                添加中...
              </>
            ) : (
              `添加所选 (${pendingCount})`
            )}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

/** 自定义参数值类型解析：true/false → 布尔，数字 → 数值，其余原样字符串 */
const parseCustomValue = (v: string): unknown => {
  if (v === 'true') return true
  if (v === 'false') return false
  if (v !== '' && !Number.isNaN(Number(v))) return Number(v)
  return v
}

/**
 * 模型行：展示 + 删除 + 模型设置编辑。
 *
 * 覆盖 llm.yaml 中模型条目的常用字段——拉取/自定义添加的模型初始只有
 * 最小配置（provider/model_name/display_name），上下文窗口、输出上限、
 * think 参数都在这里补全：
 * - 模型级：context_window（上下文窗口）、reasoning_model（推理模型标记）
 * - default_params：temperature / max_tokens / top_p / thinking.type /
 *   reasoning_effort
 * - 「保持原样」的选项不写入该字段（保留 yaml 原值或维持未设置）
 */
function ModelRow({
  modelId,
  model,
  onDelete,
  onSaveSettings,
}: {
  modelId: string
  model: ModelConfig
  onDelete: (id: string) => void
  onSaveSettings: (id: string, settings: Partial<ModelConfig>) => Promise<void>
}) {
  const [expanded, setExpanded] = useState(false)
  const params = (model.default_params ?? {}) as Record<string, unknown>
  const currentThinking = (params.thinking as { type?: string } | undefined)?.type ?? ''
  const currentEffort = typeof params.reasoning_effort === 'string' ? params.reasoning_effort : ''

  // 上下文窗口用 string state（空串=未设置，允许清空展示）
  const [contextWindow, setContextWindow] = useState(
    model.context_window != null ? String(model.context_window) : '',
  )
  const [temperature, setTemperature] = useState(
    (params.temperature as number) ?? 0.7,
  )
  const [maxTokens, setMaxTokens] = useState((params.max_tokens as number) ?? 4096)
  const [topP, setTopP] = useState((params.top_p as number) ?? 1)
  const [reasoningModel, setReasoningModel] = useState(model.reasoning_model ?? false)
  const [thinkingType, setThinkingType] = useState(currentThinking)
  const [effort, setEffort] = useState(currentEffort)

  // 自定义参数（key/value 多条，保存时合并进 default_params）
  const [customKey, setCustomKey] = useState('')
  const [customValue, setCustomValue] = useState('')
  const [customParams, setCustomParams] = useState<{ key: string; value: string }[]>([])

  const addCustomParam = () => {
    const k = customKey.trim()
    if (!k) return
    // 同名覆盖：允许修改已加入的自定义参数
    setCustomParams((prev) => [...prev.filter((p) => p.key !== k), { key: k, value: customValue.trim() }])
    setCustomKey('')
    setCustomValue('')
  }

  const handleSave = () => {
    const settings: Partial<ModelConfig> = {}
    if (contextWindow.trim() !== '') settings.context_window = Number(contextWindow)
    settings.reasoning_model = reasoningModel

    // 合并保留 default_params 中的其他字段（extra_body / reasoning_retention /
    // thinking_strength_params 在模型级，不受影响）
    const nextParams: Record<string, unknown> = {
      ...params,
      temperature,
      max_tokens: maxTokens,
      top_p: topP,
    }
    if (thinkingType) {
      nextParams.thinking = { ...((params.thinking as Record<string, unknown>) ?? {}), type: thinkingType }
    }
    if (effort) nextParams.reasoning_effort = effort
    // 自定义参数最后合并（覆盖同名字段——即「自定义」的意义）
    for (const p of customParams) {
      nextParams[p.key] = parseCustomValue(p.value)
    }
    settings.default_params = nextParams

    onSaveSettings(modelId, settings)
  }

  return (
    <div className="bg-card rounded-lg border px-3 py-2">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{model.display_name || modelId}</div>
          <div className="text-muted-foreground text-xs">
            {model.provider} / {model.model_name}
          </div>
        </div>
        <Button variant="outline" size="xs" onClick={() => setExpanded((v) => !v)}>
          <ChevronDown className={`mr-1 h-3 w-3 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          参数
        </Button>
        <Button variant="destructive" size="xs" onClick={() => onDelete(modelId)}>
          删除
        </Button>
      </div>
      {expanded && (
        <div className="mt-2 space-y-3 border-t pt-2">
          {/* 基础参数 */}
          <div>
            <h4 className="text-muted-foreground mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
              基础
            </h4>
            <div className="flex flex-wrap items-end gap-3">
              <label className="text-muted-foreground flex flex-col gap-1 text-xs">
                上下文窗口 (tokens)
                <Input
                  type="number"
                  min={0}
                  value={contextWindow}
                  onChange={(e) => setContextWindow(e.target.value)}
                  placeholder="未设置"
                  className="h-7 w-32 text-xs"
                />
              </label>
              <label className="text-muted-foreground flex flex-col gap-1 text-xs">
                最大输出 (max_tokens)
                <Input
                  type="number"
                  min={1}
                  value={maxTokens}
                  onChange={(e) => setMaxTokens(Number(e.target.value))}
                  className="h-7 w-28 text-xs"
                />
              </label>
              <label className="text-muted-foreground flex flex-col gap-1 text-xs">
                Temperature
                <Input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  value={temperature}
                  onChange={(e) => setTemperature(Number(e.target.value))}
                  className="h-7 w-24 text-xs"
                />
              </label>
              <label className="text-muted-foreground flex flex-col gap-1 text-xs">
                Top P
                <Input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={topP}
                  onChange={(e) => setTopP(Number(e.target.value))}
                  className="h-7 w-24 text-xs"
                />
              </label>
            </div>
          </div>

          {/* 推理参数（think 类模型） */}
          <div>
            <h4 className="text-muted-foreground mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
              推理 (thinking)
            </h4>
            <div className="flex flex-wrap items-end gap-3">
              <label className="text-muted-foreground flex items-center gap-1.5 pb-1.5 text-xs">
                <input
                  type="checkbox"
                  checked={reasoningModel}
                  onChange={(e) => setReasoningModel(e.target.checked)}
                  className="border-border h-3.5 w-3.5"
                />
                推理模型
              </label>
              <label className="text-muted-foreground flex flex-col gap-1 text-xs">
                思考模式
                <select
                  value={thinkingType}
                  onChange={(e) => setThinkingType(e.target.value)}
                  aria-label="思考模式"
                  className="border-border bg-background h-7 w-28 rounded px-1.5 text-xs"
                >
                  <option value="">保持原样</option>
                  <option value="enabled">开启 (enabled)</option>
                  <option value="adaptive">自适应 (adaptive)</option>
                  <option value="disabled">关闭 (disabled)</option>
                </select>
              </label>
              <label className="text-muted-foreground flex flex-col gap-1 text-xs">
                推理力度
                <select
                  value={effort}
                  onChange={(e) => setEffort(e.target.value)}
                  aria-label="推理力度"
                  className="border-border bg-background h-7 w-28 rounded px-1.5 text-xs"
                >
                  <option value="">保持原样</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                  <option value="max">max</option>
                </select>
              </label>
            </div>
          </div>

          {/* 自定义参数：合并进 default_params，随请求发送 */}
          <div>
            <h4 className="text-muted-foreground mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
              自定义参数
            </h4>
            <div className="flex items-center gap-2">
              <Input
                value={customKey}
                onChange={(e) => setCustomKey(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    addCustomParam()
                  }
                }}
                placeholder="参数名，如 extra_body"
                className="h-7 w-40 text-xs"
                aria-label="自定义参数名"
              />
              <span className="text-muted-foreground text-xs">=</span>
              <Input
                value={customValue}
                onChange={(e) => setCustomValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    addCustomParam()
                  }
                }}
                placeholder="值（数字/true/false 自动识别类型）"
                className="h-7 flex-1 text-xs"
                aria-label="自定义参数值"
              />
              <Button size="xs" variant="outline" onClick={addCustomParam} disabled={!customKey.trim()}>
                加入
              </Button>
            </div>
            {customParams.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {customParams.map((p) => (
                  <span
                    key={p.key}
                    className="bg-muted flex items-center gap-1 rounded px-2 py-0.5 font-mono text-xs"
                  >
                    {p.key}={p.value || "''"}
                    <button
                      type="button"
                      onClick={() => setCustomParams((prev) => prev.filter((x) => x.key !== p.key))}
                      className="text-muted-foreground hover:text-foreground"
                      aria-label={`移除自定义参数 ${p.key}`}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <p className="text-muted-foreground mt-1 text-[10px]">
              保存时合并进 default_params 随请求发送；同名覆盖既有字段
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Button size="xs" onClick={handleSave}>
              保存设置
            </Button>
            <p className="text-muted-foreground text-[10px]">
              「保持原样」不改动对应字段；未自定义的高级参数维持 yaml 原值
            </p>
          </div>
        </div>
      )}
    </div>
  )
}

/* ============================================ */
/* 共享子组件 (与 ApiSettingsPage 相同模式)       */
/* ============================================ */

/** 默认模型下拉：选项来自已注册模型；当前值已不在列表中时仍展示（防保存前误丢） */
function DefaultModelSelect({
  id,
  label,
  value,
  models,
  onChange,
}: {
  id: string
  label: string
  value: string
  models: string[]
  onChange: (v: string) => void
}) {
  const options = models.includes(value) ? models : [value, ...models].filter(Boolean)
  return (
    <FieldRow label={label} htmlFor={id}>
      <Select value={value || undefined} onValueChange={onChange}>
        <SelectTrigger id={id}>
          <SelectValue placeholder="未设置" />
        </SelectTrigger>
        <SelectContent>
          {options.map((m) => (
            <SelectItem key={m} value={m}>{m}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </FieldRow>
  )
}

function FieldRow({
  label,
  htmlFor,
  children,
}: {
  label: string
  htmlFor: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-4">
      <label
        htmlFor={htmlFor}
        className="text-muted-foreground text-sm sm:min-w-[120px] sm:shrink-0 sm:pt-2 sm:text-right"
      >
        {label}
      </label>
      <div className="flex-1">{children}</div>
    </div>
  )
}
