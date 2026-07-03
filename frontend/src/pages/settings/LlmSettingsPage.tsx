/**
 * LLM 模型配置页面
 *
 * 配置大语言模型参数：默认模型选择、Temperature、Max Tokens、Fallback 模型、模型列表管理
 */

import { Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import {
  getLLMConfig,
  getDefaults,
  saveLLMDefaults,
  addModel,
  deleteModel,
  addProvider,
  deleteProvider,
  updateProviderConfig,
  type LLMConfigResponse,
  type ModelConfig,
  type ProviderConfig,
  type LLMDefaults,
} from '@/services/api/config'

type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/** 模型参数（可编辑的默认参数） */
interface ModelParams {
  temperature: number
  max_tokens: number
  top_p: number
  frequency_penalty: number
  presence_penalty: number
}

const DEFAULT_PARAMS: ModelParams = {
  temperature: 0.7,
  max_tokens: 4096,
  top_p: 1.0,
  frequency_penalty: 0,
  presence_penalty: 0,
}

/**
 * LLM 配置页面组件
 */
export function LlmSettingsPage() {
  const [config, setConfig] = useState<LLMConfigResponse | null>(null)
  const [defaults, setDefaults] = useState<LLMDefaults | null>(null)
  const [params, setParams] = useState<ModelParams>(DEFAULT_PARAMS)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [activeTab, setActiveTab] = useState('defaults')

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

  // 加载配置。
  // apiClient 用绝对 baseURL 绕过 Vite 代理；生产环境前后端须同源或后端配置 CORS 头。
  const loadConfig = useCallback(() => {
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)

    Promise.all([getLLMConfig(), getDefaults()])
      .then(([llmConfig, defaultsData]) => {
        if (cancelled) return
        setConfig(llmConfig)
        setDefaults(defaultsData)

        // 从第一个模型提取默认参数
        const firstModel = Object.values(llmConfig.models)[0]
        if (firstModel?.default_params) {
          setParams({
            temperature: firstModel.default_params.temperature ?? DEFAULT_PARAMS.temperature,
            max_tokens: firstModel.default_params.max_tokens ?? DEFAULT_PARAMS.max_tokens,
            top_p: firstModel.default_params.top_p ?? DEFAULT_PARAMS.top_p,
            frequency_penalty:
              firstModel.default_params.frequency_penalty ?? DEFAULT_PARAMS.frequency_penalty,
            presence_penalty:
              firstModel.default_params.presence_penalty ?? DEFAULT_PARAMS.presence_penalty,
          })
        }
      })
      .catch((err) => {
        if (!cancelled) {
          console.error('[LlmSettingsPage] Failed to load LLM config:', err)
          setLoadError('无法连接服务器，请检查网络后重试')
          setDefaults({ chat: '', embedding: '', tiers: {} })
          setConfig({
            models: {},
            providers: {},
            defaults: { chat: '', embedding: '', tiers: {} },
          })
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const cleanup = loadConfig()
    return cleanup
  }, [loadConfig])

  const modelIds = config ? Object.keys(config.models) : []
  const providerIds = config ? Object.keys(config.providers) : []

  // 保存默认模型选择
  const handleSaveDefaults = useCallback(async () => {
    if (!defaults) return
    setSaveState('saving')
    try {
      const saved = await saveLLMDefaults(defaults)
      setDefaults(saved)
      setSaveState('saved')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch {
      setSaveState('error')
    }
  }, [defaults])

  // 添加新模型
  const handleAddModel = useCallback(async () => {
    if (!newModelId.trim()) return
    try {
      const models = await addModel(newModelId.trim(), newModelConfig)
      setConfig((prev) => (prev ? { ...prev, models } : prev))
      setNewModelId('')
      setNewModelConfig({ provider: '', model_name: '', display_name: '' })
    } catch {
      // 静默处理
    }
  }, [newModelId, newModelConfig])

  // 删除模型
  const handleDeleteModel = useCallback(async (modelId: string) => {
    try {
      const models = await deleteModel(modelId)
      setConfig((prev) => (prev ? { ...prev, models } : prev))
    } catch {
      // 静默处理
    }
  }, [])

  // 更新提供商 API Key（更新 keys[0].api_key）
  const handleUpdateApiKey = useCallback(
    async (providerId: string, apiKey: string, provider: ProviderConfig) => {
      try {
        const firstKey = provider.keys?.[0] ?? { id: `${providerId}_main` }
        const updatedKeys = [{ ...firstKey, api_key: apiKey }]
        const providers = await updateProviderConfig(providerId, { keys: updatedKeys })
        setConfig((prev) => (prev ? { ...prev, providers } : prev))
      } catch {
        // 静默处理
      }
    },
    [],
  )

  // 添加提供商
  const handleAddProvider = useCallback(async () => {
    if (!newProviderId.trim()) return
    try {
      const config: { type: string; api_base?: string; api_key?: string } = {
        type: newProviderType,
      }
      if (newProviderApiBase.trim()) config.api_base = newProviderApiBase.trim()
      if (newProviderApiKey.trim()) config.api_key = newProviderApiKey.trim()
      const providers = await addProvider(newProviderId.trim(), config)
      setConfig((prev) => (prev ? { ...prev, providers } : prev))
      setNewProviderId('')
      setNewProviderType('openai')
      setNewProviderApiBase('')
      setNewProviderApiKey('')
    } catch {
      // 静默处理
    }
  }, [newProviderId, newProviderType, newProviderApiBase, newProviderApiKey])

  // 删除提供商
  const handleDeleteProvider = useCallback(async (providerId: string) => {
    try {
      const providers = await deleteProvider(providerId)
      setConfig((prev) => (prev ? { ...prev, providers } : prev))
    } catch {
      // 静默处理
    }
  }, [])

  if (isLoading) {
    return (
      <PageShell title="LLM 模型配置" description="配置大语言模型参数">
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <div className="border-primary mr-2 h-5 w-5 animate-spin rounded-full border-2 border-t-transparent" />
          加载配置...
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell title="LLM 模型配置" description="配置大语言模型参数">
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
            onClick={loadConfig}
            className="ml-4 shrink-0 border-destructive/30 text-destructive hover:bg-destructive/10"
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            重试
          </Button>
        </div>
      )}

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="defaults">默认模型</TabsTrigger>
          <TabsTrigger value="params">模型参数</TabsTrigger>
          <TabsTrigger value="models">模型管理</TabsTrigger>
          <TabsTrigger value="providers">提供商</TabsTrigger>
        </TabsList>

        {/* 默认模型选择 */}
        <TabsContent value="defaults">
          <div className="mt-4 space-y-4">
            <FieldRow label="聊天模型" htmlFor="default-chat">
              <Select
                value={defaults?.chat ?? ''}
                onValueChange={(v) => setDefaults((prev) => (prev ? { ...prev, chat: v } : prev))}
              >
                <SelectTrigger id="default-chat">
                  <SelectValue placeholder="选择聊天模型" />
                </SelectTrigger>
                <SelectContent>
                  {modelIds.map((id) => (
                    <SelectItem key={id} value={id}>
                      {id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FieldRow>

            <FieldRow label="模型分级 (Large)" htmlFor="tier-large">
              <Select
                value={defaults?.tiers?.large ?? ''}
                onValueChange={(v) =>
                  setDefaults((prev) =>
                    prev ? { ...prev, tiers: { ...prev.tiers, large: v } } : prev
                  )
                }
              >
                <SelectTrigger id="tier-large">
                  <SelectValue placeholder="大型任务模型" />
                </SelectTrigger>
                <SelectContent>
                  {modelIds.map((id) => (
                    <SelectItem key={id} value={id}>
                      {id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FieldRow>

            <FieldRow label="模型分级 (Medium)" htmlFor="tier-medium">
              <Select
                value={defaults?.tiers?.medium ?? ''}
                onValueChange={(v) =>
                  setDefaults((prev) =>
                    prev ? { ...prev, tiers: { ...prev.tiers, medium: v } } : prev
                  )
                }
              >
                <SelectTrigger id="tier-medium">
                  <SelectValue placeholder="中型任务模型" />
                </SelectTrigger>
                <SelectContent>
                  {modelIds.map((id) => (
                    <SelectItem key={id} value={id}>
                      {id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FieldRow>

            <FieldRow label="模型分级 (Small)" htmlFor="tier-small">
              <Select
                value={defaults?.tiers?.small ?? ''}
                onValueChange={(v) =>
                  setDefaults((prev) =>
                    prev ? { ...prev, tiers: { ...prev.tiers, small: v } } : prev
                  )
                }
              >
                <SelectTrigger id="tier-small">
                  <SelectValue placeholder="小型任务模型" />
                </SelectTrigger>
                <SelectContent>
                  {modelIds.map((id) => (
                    <SelectItem key={id} value={id}>
                      {id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FieldRow>

            <FieldRow label="嵌入模型" htmlFor="default-embedding">
              <Select
                value={defaults?.embedding ?? ''}
                onValueChange={(v) =>
                  setDefaults((prev) => (prev ? { ...prev, embedding: v } : prev))
                }
              >
                <SelectTrigger id="default-embedding">
                  <SelectValue placeholder="选择嵌入模型" />
                </SelectTrigger>
                <SelectContent>
                  {modelIds.map((id) => (
                    <SelectItem key={id} value={id}>
                      {id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FieldRow>

            <div className="flex items-center gap-3 pt-2">
              <Button onClick={handleSaveDefaults} disabled={saveState === 'saving'}>
                {saveState === 'saving' ? (
                  <>
                    <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    保存中...
                  </>
                ) : (
                  '保存默认配置'
                )}
              </Button>
              {saveState === 'saved' && <span className="text-xs text-status-success" role="status">已保存</span>}
              {saveState === 'error' && <span className="text-xs text-status-error" role="alert">保存失败</span>}
            </div>
          </div>
        </TabsContent>

        {/* 模型参数 */}
        <TabsContent value="params">
          <div className="mt-4 space-y-4">
            <FieldRow label="Temperature" htmlFor="param-temp">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  id="param-temp"
                  min={0}
                  max={2}
                  step={0.1}
                  value={params.temperature}
                  onChange={(e) =>
                    setParams((prev) => ({ ...prev, temperature: Number(e.target.value) }))
                  }
                  className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
                />
                <span className="text-foreground min-w-[40px] text-right text-sm">
                  {params.temperature}
                </span>
              </div>
            </FieldRow>

            <FieldRow label="Max Tokens" htmlFor="param-tokens">
              <Input
                id="param-tokens"
                type="number"
                min={1}
                max={128000}
                value={params.max_tokens}
                onChange={(e) =>
                  setParams((prev) => ({ ...prev, max_tokens: Number(e.target.value) }))
                }
              />
            </FieldRow>

            <FieldRow label="Top P" htmlFor="param-topp">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  id="param-topp"
                  min={0}
                  max={1}
                  step={0.05}
                  value={params.top_p}
                  onChange={(e) =>
                    setParams((prev) => ({ ...prev, top_p: Number(e.target.value) }))
                  }
                  className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
                />
                <span className="text-foreground min-w-[40px] text-right text-sm">
                  {params.top_p}
                </span>
              </div>
            </FieldRow>

            <FieldRow label="Frequency Penalty" htmlFor="param-fp">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  id="param-fp"
                  min={-2}
                  max={2}
                  step={0.1}
                  value={params.frequency_penalty}
                  onChange={(e) =>
                    setParams((prev) => ({ ...prev, frequency_penalty: Number(e.target.value) }))
                  }
                  className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
                />
                <span className="text-foreground min-w-[40px] text-right text-sm">
                  {params.frequency_penalty}
                </span>
              </div>
            </FieldRow>

            <FieldRow label="Presence Penalty" htmlFor="param-pp">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  id="param-pp"
                  min={-2}
                  max={2}
                  step={0.1}
                  value={params.presence_penalty}
                  onChange={(e) =>
                    setParams((prev) => ({ ...prev, presence_penalty: Number(e.target.value) }))
                  }
                  className="bg-border accent-primary h-2 flex-1 appearance-none rounded-full"
                />
                <span className="text-foreground min-w-[40px] text-right text-sm">
                  {params.presence_penalty}
                </span>
              </div>
            </FieldRow>
          </div>
        </TabsContent>

        {/* 模型管理 */}
        <TabsContent value="models">
          <div className="mt-4 space-y-4">
            <h3 className="text-sm font-semibold">已注册模型 ({modelIds.length})</h3>
            {modelIds.length === 0 ? (
              <div className="text-muted-foreground py-4 text-center text-sm">暂无模型</div>
            ) : (
              <div className="space-y-2">
                {modelIds.map((id) => {
                  const model = config!.models[id]
                  return (
                    <div
                      key={id}
                      className="bg-card flex items-center gap-3 rounded-lg border px-3 py-2"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">
                          {model.display_name || id}
                        </div>
                        <div className="text-muted-foreground text-xs">
                          {model.provider} / {model.model_name}
                        </div>
                      </div>
                      <Button variant="destructive" size="xs" onClick={() => handleDeleteModel(id)}>
                        删除
                      </Button>
                    </div>
                  )
                })}
              </div>
            )}

            <div className="mt-4 border-t pt-4">
              <h3 className="mb-3 text-sm font-semibold">添加模型</h3>
              <div className="space-y-2">
                <FieldRow label="模型 ID" htmlFor="new-model-id">
                  <Input
                    id="new-model-id"
                    value={newModelId}
                    onChange={(e) => setNewModelId(e.target.value)}
                    placeholder="如: gpt-4o"
                  />
                </FieldRow>
                <FieldRow label="提供商" htmlFor="new-model-provider">
                  <Input
                    id="new-model-provider"
                    value={newModelConfig.provider}
                    onChange={(e) =>
                      setNewModelConfig((prev) => ({ ...prev, provider: e.target.value }))
                    }
                    placeholder="如: openai"
                  />
                </FieldRow>
                <FieldRow label="模型名称" htmlFor="new-model-name">
                  <Input
                    id="new-model-name"
                    value={newModelConfig.model_name}
                    onChange={(e) =>
                      setNewModelConfig((prev) => ({ ...prev, model_name: e.target.value }))
                    }
                    placeholder="如: gpt-4o-2024-08-06"
                  />
                </FieldRow>
                <FieldRow label="显示名称" htmlFor="new-model-display">
                  <Input
                    id="new-model-display"
                    value={newModelConfig.display_name}
                    onChange={(e) =>
                      setNewModelConfig((prev) => ({ ...prev, display_name: e.target.value }))
                    }
                    placeholder="如: GPT-4o"
                  />
                </FieldRow>
                <FieldRow label="API Base" htmlFor="new-model-apibase">
                  <Input
                    id="new-model-apibase"
                    value={newModelConfig.api_base ?? ''}
                    onChange={(e) =>
                      setNewModelConfig((prev) => ({ ...prev, api_base: e.target.value || undefined }))
                    }
                    placeholder="可选，留空则使用 provider 的 api_base"
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
                <Button size="sm" onClick={handleAddModel} disabled={!newModelId.trim()}>
                  添加模型
                </Button>
              </div>
            </div>
          </div>
        </TabsContent>

        {/* 提供商管理 */}
        <TabsContent value="providers">
          <div className="mt-4 space-y-4">
            <h3 className="text-sm font-semibold">已配置提供商 ({providerIds.length})</h3>
            {providerIds.length === 0 ? (
              <div className="text-muted-foreground py-4 text-center text-sm">暂无提供商</div>
            ) : (
              <div className="space-y-3">
                {providerIds.map((id) => {
                  const provider = config!.providers[id]
                  return (
                    <ProviderCard
                      key={id}
                      providerId={id}
                      provider={provider}
                      onUpdateKey={handleUpdateApiKey}
                      onDelete={handleDeleteProvider}
                    />
                  )
                })}
              </div>
            )}

            <div className="mt-4 border-t pt-4">
              <h3 className="mb-3 text-sm font-semibold">添加提供商</h3>
              <div className="space-y-2">
                <FieldRow label="提供商 ID" htmlFor="new-provider-id">
                  <Input
                    id="new-provider-id"
                    value={newProviderId}
                    onChange={(e) => setNewProviderId(e.target.value)}
                    placeholder="如: deepseek"
                  />
                </FieldRow>
                <FieldRow label="类型" htmlFor="new-provider-type">
                  <Select value={newProviderType} onValueChange={setNewProviderType}>
                    <SelectTrigger id="new-provider-type">
                      <SelectValue placeholder="选择类型" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="openai">openai</SelectItem>
                      <SelectItem value="deepseek">deepseek</SelectItem>
                      <SelectItem value="zai">zai</SelectItem>
                      <SelectItem value="minimax">minimax</SelectItem>
                    </SelectContent>
                  </Select>
                </FieldRow>
                <FieldRow label="API Base" htmlFor="new-provider-apibase">
                  <Input
                    id="new-provider-apibase"
                    value={newProviderApiBase}
                    onChange={(e) => setNewProviderApiBase(e.target.value)}
                    placeholder="如: https://api.deepseek.com/v1"
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
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </PageShell>
  )
}

/** 提供商卡片 */
function ProviderCard({
  providerId,
  provider,
  onUpdateKey,
  onDelete,
}: {
  providerId: string
  provider: ProviderConfig
  onUpdateKey: (id: string, key: string, provider: ProviderConfig) => void
  onDelete: (id: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [apiKey, setApiKey] = useState('')

  // 后端返回的 api_key 已脱敏，直接取第一个 key 的值用于展示
  const firstKey = provider.keys?.[0]
  const hasKey = Boolean(firstKey?.api_key)
  const maskedKey = firstKey?.api_key ?? '未设置'

  return (
    <div className="bg-card space-y-2 rounded-lg border px-4 py-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">{providerId}</span>
          {provider.type && (
            <span className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 text-[10px] font-mono">
              {provider.type}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`rounded px-2 py-0.5 text-xs ${hasKey ? 'bg-status-success/10 text-status-success' : 'bg-status-error/10 text-status-error'}`}
          >
            {hasKey ? '已配置' : '未配置'}
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
      <div className="text-muted-foreground font-mono text-xs">Key: {editing ? '' : maskedKey}</div>
      {editing ? (
        <div className="flex items-center gap-2">
          <Input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="输入新的 API Key"
            className="text-xs"
          />
          <Button
            size="xs"
            onClick={() => {
              onUpdateKey(providerId, apiKey, provider)
              setEditing(false)
            }}
          >
            保存
          </Button>
          <Button size="xs" variant="outline" onClick={() => setEditing(false)}>
            取消
          </Button>
        </div>
      ) : (
        <Button size="xs" variant="outline" onClick={() => setEditing(true)}>
          更新 Key
        </Button>
      )}
    </div>
  )
}

/* ============================================ */
/* 共享子组件 (与 ApiSettingsPage 相同模式)       */
/* ============================================ */

function PageShell({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: React.ReactNode
}) {
  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/settings" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 设置
        </a>
        <h1 className="ml-4 text-base font-semibold">{title}</h1>
        <span className="text-muted-foreground ml-2 text-xs">{description}</span>
      </header>
      <main className="max-w-3xl flex-1 overflow-y-auto p-3 sm:p-6" role="form" aria-label="LLM模型配置表单">{children}</main>
    </div>
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
