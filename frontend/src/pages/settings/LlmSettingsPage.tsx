/**
 * LLM 模型设置页（单界面形态）
 *
 * 布局按主流软件惯用形态排列，一屏四段：
 * 1. 提供商——一个选择控件，内置提供商各占一个选项（原卡片即选项），末尾
 *    「自定义」选项；选中内置提供商无需填 URL/API Key（端点随配置自带，
 *    Key 仅未配置时给填写入口），选中「自定义」时同屏展开 Base URL 与
 *    API Key 等新增表单。
 * 2. 模型——与提供商同界面完成：可从提供商拉取模型列表勾选，也可手填
 *    自定义模型名；可选一并填写参数。
 * 3. 已注册模型——简洁条目逐行列出，注册一个即多一行。
 * 4. 默认模型——大/中/小档位与对话/向量模型的选择栏位与当前值（不展示
 *    已注册模型清单，模型清单在上方独立成段）。
 *
 * 数据面四态统一（OBS-R258-1）：主面（LLM 配置）加载失败显式 ErrorState +
 * 重试，不伪装空态；副面（预置声明）失败降级由数据源注释声明。
 */

import { useQueryClient } from '@tanstack/react-query'
import { useState, useEffect, useCallback, useMemo } from 'react'
import { Loader2, Plus } from '@/assets/icons'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
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
import { useLlmConfigQuery, useLlmPresetsQuery } from '@/hooks/queries/useLlmQueries'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import {
  getProviderTypes,
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
import { queryKeys } from '@/services/query/queryKeys'
import {
  buildModelFields,
  buildRemoteModelFields,
  emptyModelParamsDraft,
  DEFAULT_STRENGTH_LEVELS,
} from './modelParams'
import { ModelParamsEditor } from './ModelParamsEditor'
import { ModelPickerPanel } from './ModelPickerPanel'
import { ModelRegistryList } from './ModelRegistryList'
import { ProviderDetailPanel } from './ProviderDetailPanel'

/** 「自定义提供商」选项值（选中即展开新增表单，不指向任何已存在提供商） */
const CUSTOM_PROVIDER = '__custom__'

/** 大/中/小档位（llm.yaml defaults.tiers 的键，供 agent model_tier 解析） */
const TIER_ROWS: { key: string; label: string }[] = [
  { key: 'large', label: '大模型' },
  { key: 'medium', label: '中模型' },
  { key: 'small', label: '小模型' },
]

/**
 * 从被 reject 的对象中提取后端错误消息。
 *
 * apiClient 的拦截器构造的是普通 ApiError 对象（非 Error 实例），
 * 因此不能用 instanceof Error，需直接读 .message 字段。
 */
const getApiMsg = (e: unknown, fallback = '操作失败'): string =>
  (e as { message?: string })?.message ?? fallback

/**
 * LLM 配置页面组件
 */
export function LlmSettingsPage({ embedded = false }: { embedded?: boolean }) {
  const [config, setConfig] = useState<LLMConfigResponse | null>(null)

  /** 当前操作的提供商（内置提供商 ID 或 CUSTOM_PROVIDER） */
  const [selectedProvider, setSelectedProvider] = useState('')

  /** 进行中的保存动作键（per-action savingKey，await 窗口对应按钮 disabled+spinner
   *  防连点；失败 toast 已在各 handler 落点，状态复位可重试）。运行时态。 */
  const [savingAction, setSavingAction] = useState<string | null>(null)

  // 默认模型草稿（档位段，随 config.defaults 同步）
  const [defaultsDraft, setDefaultsDraft] = useState<LLMDefaults>({
    chat: '',
    embedding: '',
    tiers: {},
  })

  // 添加模型时可选一并填写的参数草稿（未触碰则不参与提交，远端事实上限不被覆盖）
  const [addParams, setAddParams] = useState(emptyModelParamsDraft)
  const [addParamsTouched, setAddParamsTouched] = useState(false)

  const [newProviderId, setNewProviderId] = useState('')
  const [newProviderType, setNewProviderType] = useState('openai')
  const [newProviderApiBase, setNewProviderApiBase] = useState('')
  const [newProviderApiKey, setNewProviderApiKey] = useState('')
  // litellm 动态类型目录（展开自定义提供商表单时懒加载）
  const [providerTypes, setProviderTypes] = useState<string[]>([])
  const [providerTypesLoaded, setProviderTypesLoaded] = useState(false)

  // 配置面预置声明（llm_service /ext 端点下发，声明驱动——前端零硬编码词典）：
  // provider 分组/显示名、常用类型置顶、思考强度档位白名单
  // 副面降级决策（OBS-R258-1 铁律 4）：声明拉取失败不阻断主配置面——
  // 分组归「其他已添加」、思考强度走内置缺省档位、类型目录回退常用类型
  const presetsResource = useAsyncResource(useLlmPresetsQuery(), {
    fallbackErrorText: '获取预置声明失败',
  })
  const presets = presetsResource.data
  const presetProviderIds = useMemo(
    () => new Set(presets?.provider_groups.flatMap((g) => g.providers.map(([id]) => id)) ?? []),
    [presets],
  )
  const strengthLevels = presets?.thinking_strength.levels ?? DEFAULT_STRENGTH_LEVELS

  // 声明到达前 providerTypes 空表：声明常用类型先落位（懒加载目录未拉时兜底）
  useEffect(() => {
    if (presets && !providerTypesLoaded) setProviderTypes(presets.common_provider_types)
  }, [presets, providerTypesLoaded])

  // 声明到达后把添加参数草稿的档位对齐到声明词汇（草稿初值走缺省档位）
  useEffect(() => {
    if (!presets) return
    setAddParams((prev) => {
      const levels = presets.thinking_strength.levels
      if (Object.keys(prev.strength).join(',') === levels.join(',')) return prev
      return emptyModelParamsDraft(levels)
    })
  }, [presets])

  // 加载配置（query 化）：重进设置页缓存秒开；apiClient 用绝对 baseURL 绕过
  // Vite 代理；生产环境前后端须同源或后端配置 CORS 头。
  // 主面四态统一（OBS-R258-1）：失败显式 ErrorState + 重试上屏，不伪造空配置；
  // apiClient 拒绝的是普通 ApiError 对象（非 Error 实例）→ 走 fallbackErrorText
  const configResource = useAsyncResource(useLlmConfigQuery(), {
    fallbackErrorText: '无法连接服务器，请检查网络后重试',
  })

  // 保存成功后把增量切片直写共享 query 缓存（queryKeys.llmConfig）：其他消费方
  // （ChatContainer 模型显示/思考强度映射）即时热更新。不走 invalidate——refetch
  // 以服务器全量态覆盖本地可编辑副本，会打断其他字段编辑；直写与下方 setConfig
  // 同一切片，缓存与本地副本保持一致。
  const queryClient = useQueryClient()
  const syncConfigCache = useCallback(
    (patch: Partial<LLMConfigResponse>) => {
      queryClient.setQueryData<LLMConfigResponse>(queryKeys.llmConfig, (prev) =>
        prev ? { ...prev, ...patch } : prev,
      )
    },
    [queryClient],
  )

  useEffect(() => {
    if (configResource.data) {
      setConfig(configResource.data)
    }
  }, [configResource.data])

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
  // 引用稳定：providers 直接取配置切片，避免 ?? {} 每次渲染产生新对象让下游 useMemo 失效
  const providers = useMemo(() => config?.providers ?? {}, [config])

  // 生效的选中项是派生值（不是回写 state 的副作用）：选中项失效（首次进入 / 被删除）
  // 时直接回落到第一个提供商，无提供商则落「自定义」——避免多一轮渲染窗口里
  // 界面短暂显示「暂无提供商」。
  const effectiveProvider = useMemo(() => {
    if (!config) return ''
    if (selectedProvider === CUSTOM_PROVIDER) return CUSTOM_PROVIDER
    if (selectedProvider && providers[selectedProvider]) return selectedProvider
    return Object.keys(providers)[0] ?? CUSTOM_PROVIDER
  }, [config, selectedProvider, providers])

  const isCustomProvider = effectiveProvider === CUSTOM_PROVIDER
  const currentProvider: ProviderConfig | undefined = isCustomProvider
    ? undefined
    : providers[effectiveProvider]

  // 选中「自定义」时懒加载 litellm 动态类型目录（失败回退声明常用类型，不阻塞表单）
  useEffect(() => {
    if (!isCustomProvider || providerTypesLoaded) return
    getProviderTypes()
      .then(({ types }) => {
        setProviderTypes(types)
        setProviderTypesLoaded(true)
      })
      .catch(() => {
        if (presets) setProviderTypes(presets.common_provider_types)
      })
  }, [isCustomProvider, providerTypesLoaded, presets])

  const currentDisplayName = useMemo(() => {
    for (const g of presets?.provider_groups ?? []) {
      const hit = g.providers.find(([id]) => id === effectiveProvider)
      if (hit) return hit[1]
    }
    return undefined
  }, [presets, effectiveProvider])

  // 提供商选项分组：预置声明分组（内置）→ 其余已添加（自定义添加过的）→ 自定义
  const providerOptionGroups = useMemo(() => {
    const groups = (presets?.provider_groups ?? []).map((g) => ({
      label: g.label,
      items: g.providers
        .filter(([id]) => providers[id])
        .map(([id, name]) => ({ id, name: name || id })),
    }))
    const rest = Object.keys(providers)
      .filter((id) => !presetProviderIds.has(id))
      .map((id) => ({ id, name: id }))
    return [...groups, { label: '其他已添加', items: rest }].filter((g) => g.items.length > 0)
  }, [presets, providers, presetProviderIds])

  // 保存默认模型选择（chat/embedding/tiers 部分更新，PUT /llm/defaults）
  const handleSaveDefaults = useCallback(async () => {
    setSavingAction('defaults')
    try {
      const defaults = await saveDefaults(defaultsDraft)
      setConfig((prev) => (prev ? { ...prev, defaults } : prev))
      syncConfigCache({ defaults })
      toast.success('默认模型已保存')
    } catch (e) {
      toast.error('保存默认模型失败', { description: getApiMsg(e, '保存默认模型失败') })
    } finally {
      setSavingAction(null)
    }
  }, [defaultsDraft, syncConfigCache])

  const handleDeleteModel = useCallback(
    async (modelId: string) => {
      setSavingAction(`del-model:${modelId}`)
      try {
        const models = await deleteModel(modelId)
        setConfig((prev) => (prev ? { ...prev, models } : prev))
        syncConfigCache({ models })
      } catch (e) {
        toast.error('删除模型失败', { description: getApiMsg(e, '删除模型失败') })
      } finally {
        setSavingAction(null)
      }
    },
    [syncConfigCache],
  )

  // 保存模型设置（上下文窗口/推理标记/default_params——拉取或自定义添加的
  // 模型初始只有最小字段，上下文与 think 参数都靠这里补全）
  const handleSaveModelSettings = useCallback(
    async (modelId: string, settings: Partial<ModelConfig>) => {
      setSavingAction(`model-settings:${modelId}`)
      try {
        const models = await updateModel(modelId, settings)
        setConfig((prev) => (prev ? { ...prev, models } : prev))
        syncConfigCache({ models })
        toast.success(`已保存 ${modelId} 的模型设置`)
      } catch (e) {
        toast.error('保存模型设置失败', { description: getApiMsg(e, '保存模型设置失败') })
      } finally {
        setSavingAction(null)
      }
    },
    [syncConfigCache],
  )

  // 填写 / 更新提供商 API Key（明文经后端写入 .env，yaml 保持 ${VAR} 占位）
  const handleUpdateApiKey = useCallback(
    async (providerId: string, apiKey: string, provider: ProviderConfig) => {
      if (!apiKey.trim()) return
      setSavingAction(`key:${providerId}`)
      try {
        const firstKey = provider.keys?.[0]
        const entry: Record<string, unknown> = {
          id: firstKey?.id ?? `${providerId}_main`,
          api_key: apiKey.trim(),
        }
        const updated = await updateProviderConfig(providerId, { keys: [entry] })
        setConfig((prev) => (prev ? { ...prev, providers: updated } : prev))
        syncConfigCache({ providers: updated })
        toast.success(`已保存 ${providerId} 的 Key（写入 .env，立即生效）`)
      } catch (e) {
        toast.error('保存密钥失败', { description: getApiMsg(e, '保存密钥失败') })
      } finally {
        setSavingAction(null)
      }
    },
    [syncConfigCache],
  )

  // 保存提供者并发 / RPM（KeyPool 消费；不带 api_key，后端按条目合并保留占位符）
  const handleSaveConcurrency = useCallback(
    async (providerId: string, maxConcurrent: number, rpm: number) => {
      setSavingAction(`concurrency:${providerId}`)
      try {
        const firstKey = config?.providers[providerId]?.keys?.[0]
        const entry: Record<string, unknown> = {
          id: firstKey?.id ?? `${providerId}_main`,
          max_concurrent: maxConcurrent,
          rpm,
        }
        const updated = await updateProviderConfig(providerId, { keys: [entry] })
        setConfig((prev) => (prev ? { ...prev, providers: updated } : prev))
        syncConfigCache({ providers: updated })
        toast.success(`已保存 ${providerId} 的并发设置`)
      } catch (e) {
        toast.error('保存并发设置失败', { description: getApiMsg(e, '保存并发设置失败') })
      } finally {
        setSavingAction(null)
      }
    },
    [config, syncConfigCache],
  )

  const handleAddProvider = useCallback(async () => {
    if (!newProviderId.trim()) return
    setSavingAction('add-provider')
    try {
      const providerConfig: { type: string; api_base?: string; api_key?: string } = {
        type: newProviderType,
      }
      if (newProviderApiBase.trim()) providerConfig.api_base = newProviderApiBase.trim()
      if (newProviderApiKey.trim()) providerConfig.api_key = newProviderApiKey.trim()
      const updated = await addProvider(newProviderId.trim(), providerConfig)
      setConfig((prev) => (prev ? { ...prev, providers: updated } : prev))
      syncConfigCache({ providers: updated })
      setSelectedProvider(newProviderId.trim())
      setNewProviderId('')
      setNewProviderType('openai')
      setNewProviderApiBase('')
      setNewProviderApiKey('')
    } catch (e) {
      toast.error('添加提供商失败', { description: getApiMsg(e, '添加提供商失败') })
    } finally {
      setSavingAction(null)
    }
  }, [newProviderId, newProviderType, newProviderApiBase, newProviderApiKey, syncConfigCache])

  const handleDeleteProvider = useCallback(
    async (providerId: string) => {
      setSavingAction(`del-provider:${providerId}`)
      try {
        const updated = await deleteProvider(providerId)
        setConfig((prev) => (prev ? { ...prev, providers: updated } : prev))
        syncConfigCache({ providers: updated })
      } catch (e) {
        toast.error('删除提供商失败', { description: getApiMsg(e, '删除提供商失败') })
      } finally {
        setSavingAction(null)
      }
    },
    [syncConfigCache],
  )

  // 添加模型（拉取列表勾选 + 手填模型名，同一提交入口）
  // limits：id → litellm 注册表下发的事实上限（context_window / max_output_tokens）；
  // 手填模型名不在拉取列表里 → 无事实，走 DEFAULT_MAX_TOKENS 兜底。
  // 添加参数草稿被触碰过才参与提交（否则远端事实上限不被默认值覆盖）。
  const handleAddModels = useCallback(
    async (providerId: string, modelNames: string[], limits: Map<string, RemoteModel>) => {
      setSavingAction('add-model')
      let lastModels: Record<string, ModelConfig> | null = null
      const failed: string[] = []
      const extra = addParamsTouched ? buildModelFields(addParams) : null
      for (const name of modelNames) {
        const base = buildRemoteModelFields(limits.get(name) ?? {})
        try {
          const { models } = await addModel(name, {
            provider: providerId,
            model_name: name,
            display_name: name,
            ...base,
            ...(extra ?? {}),
            default_params: { ...(base.default_params ?? {}), ...(extra?.default_params ?? {}) },
          })
          lastModels = models
        } catch {
          failed.push(name)
        }
      }
      const merged = lastModels
      if (merged) {
        setConfig((prev) => (prev ? { ...prev, models: merged } : prev))
        syncConfigCache({ models: merged })
      }
      setSavingAction(null)
      if (failed.length === 0) {
        toast.success(`已添加 ${modelNames.length} 个模型到 ${providerId}`)
      } else if (failed.length < modelNames.length) {
        toast.warning(`已添加 ${modelNames.length - failed.length} 个模型`, {
          description: `添加失败（可能已存在）: ${failed.join(', ')}`,
        })
      } else {
        toast.error('添加模型失败', { description: failed.join(', ') })
      }
    },
    [addParams, addParamsTouched, syncConfigCache],
  )

  if (configResource.status === 'loading') {
    return (
      <PageShell
        title="模型设置"
        description="配置大语言模型提供商与模型"
        embedded={embedded}
        backHref="/settings"
        backLabel="设置"
        maxWidth="max-w-3xl"
      >
        <LoadingState text="加载配置..." />
      </PageShell>
    )
  }

  // 失败态（OBS-R258-1 铁律 1/2）：主配置面失败整页 ErrorState + 重试，
  // 不渲染「暂无提供商/暂无模型」类零数据伪装物
  if (configResource.status === 'error') {
    return (
      <PageShell
        title="模型设置"
        description="配置大语言模型提供商与模型"
        embedded={embedded}
        backHref="/settings"
        backLabel="设置"
        maxWidth="max-w-3xl"
      >
        <ErrorState
          variant="center"
          message={configResource.error ?? ''}
          onRetry={configResource.refetch}
        />
      </PageShell>
    )
  }

  const providerNeedsNoKey = effectiveProvider === 'ollama'
  const canPull = Boolean(
    currentProvider && (providerNeedsNoKey || (currentProvider.has_key ?? false)),
  )

  return (
    <PageShell
      title="模型设置"
      description="配置大语言模型提供商与模型"
      embedded={embedded}
      mainLabel="模型设置表单"
    >
      <div className="space-y-6">
        {/* ── 1. 提供商：一个选择控件 ── */}
        <section>
          <h3 className="mb-3 text-sm font-semibold">提供商</h3>
          <div className="space-y-3">
            <FieldRow label="提供商" htmlFor="llm-provider">
              <Select value={effectiveProvider} onValueChange={setSelectedProvider}>
                <SelectTrigger id="llm-provider" aria-label="提供商">
                  <SelectValue placeholder="选择提供商" />
                </SelectTrigger>
                <SelectContent>
                  {providerOptionGroups.map((g) => (
                    <SelectGroup key={g.label}>
                      <SelectLabel>{g.label}</SelectLabel>
                      {g.items.map((item) => (
                        <SelectItem key={item.id} value={item.id}>
                          {item.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  ))}
                  <SelectGroup>
                    <SelectLabel>其他</SelectLabel>
                    <SelectItem value={CUSTOM_PROVIDER}>自定义（新增提供商）</SelectItem>
                  </SelectGroup>
                </SelectContent>
              </Select>
            </FieldRow>

            {isCustomProvider ? (
              // 自定义：URL / API Key 在同一界面下方展开
              <div className="space-y-2">
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
                        {(presets?.common_provider_types ?? []).map((t) => (
                          <SelectItem key={t} value={t}>
                            {t}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                      <SelectGroup>
                        <SelectLabel>litellm 全部（{providerTypes.length}）</SelectLabel>
                        {providerTypes
                          .filter((t) => !(presets?.common_provider_types ?? []).includes(t))
                          .map((t) => (
                            <SelectItem key={t} value={t}>
                              {t}
                            </SelectItem>
                          ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </FieldRow>
                <FieldRow label="Base URL" htmlFor="new-provider-apibase">
                  <Input
                    id="new-provider-apibase"
                    value={newProviderApiBase}
                    onChange={(e) => setNewProviderApiBase(e.target.value)}
                    placeholder="如: https://my.proxy/v1（留空使用该类型默认端点）"
                  />
                </FieldRow>
                <FieldRow label="API Key" htmlFor="new-provider-apikey">
                  <Input
                    id="new-provider-apikey"
                    type="password"
                    autoComplete="new-password"
                    value={newProviderApiKey}
                    onChange={(e) => setNewProviderApiKey(e.target.value)}
                    placeholder="输入 API Key（自动写入 .env）"
                  />
                </FieldRow>
                <div className="pl-0 sm:pl-[136px]">
                  <Button
                    size="sm"
                    onClick={handleAddProvider}
                    disabled={!newProviderId.trim() || savingAction === 'add-provider'}
                  >
                    {savingAction === 'add-provider' ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Plus className="mr-1.5 h-3.5 w-3.5" />
                    )}
                    {savingAction === 'add-provider' ? '添加中...' : '添加提供商'}
                  </Button>
                </div>
                <p className="text-muted-foreground text-xs">
                  类型目录来自 litellm，升级 litellm 后自动出现新提供者。
                </p>
              </div>
            ) : currentProvider ? (
              <ProviderDetailPanel
                key={effectiveProvider}
                providerId={effectiveProvider}
                displayName={currentDisplayName}
                provider={currentProvider}
                keySaving={savingAction === `key:${effectiveProvider}`}
                concurrencySaving={savingAction === `concurrency:${effectiveProvider}`}
                deleting={savingAction === `del-provider:${effectiveProvider}`}
                onUpdateKey={handleUpdateApiKey}
                onDelete={handleDeleteProvider}
                onSaveConcurrency={handleSaveConcurrency}
              />
            ) : (
              <div className="text-muted-foreground py-2 text-sm">
                暂无提供商——在「提供商」里选「自定义」新增一个
              </div>
            )}
          </div>
        </section>

        {/* ── 2. 模型：与提供商同界面完成选择 ── */}
        <section className="border-t pt-4">
          <h3 className="mb-1 text-sm font-semibold">模型</h3>
          <p className="text-muted-foreground mb-3 text-xs leading-relaxed">
            从当前提供商拉取模型列表勾选，或直接手填模型名——两种方式添加的模型都会进入下方
            「已注册模型」。
          </p>
          {currentProvider ? (
            <ModelPickerPanel
              key={effectiveProvider}
              providerId={effectiveProvider}
              providerLabel={currentDisplayName}
              canPull={canPull}
              adding={savingAction === 'add-model'}
              onAdd={handleAddModels}
            />
          ) : (
            <div className="text-muted-foreground py-2 text-sm">
              先添加一个自定义提供商，之后即可从中拉取或手填模型名。
            </div>
          )}

          {/* 添加时可选一并填写参数（不触碰则按模型类型兜底/远端事实写入） */}
          {currentProvider && (
            <details
              className="mt-3"
              onToggle={(e) => {
                if (!(e.target as HTMLDetailsElement).open) return
                if (!addParamsTouched) setAddParamsTouched(true)
              }}
            >
              <summary className="cursor-pointer text-xs font-medium select-none">
                添加时一并填写模型参数（可选）
              </summary>
              <div className="mt-2 rounded-lg border p-3">
                <ModelParamsEditor
                  value={addParams}
                  onChange={(v) => {
                    setAddParams(v)
                    setAddParamsTouched(true)
                  }}
                  levels={strengthLevels}
                />
              </div>
            </details>
          )}
        </section>

        {/* ── 3. 已注册模型：简洁条目逐行 ── */}
        <section className="border-t pt-4">
          <h3 className="mb-1 text-sm font-semibold">已注册模型 ({modelIds.length})</h3>
          <p className="text-muted-foreground mb-3 text-xs">
            每注册一个模型即增加一行；展开「参数」可设置上下文窗口、采样参数、思考模式、
            思考强度映射与多模态能力。
          </p>
          <ModelRegistryList
            models={config?.models ?? {}}
            strengthLevels={strengthLevels}
            isDeleting={(id) => savingAction === `del-model:${id}`}
            isSavingSettings={(id) => savingAction === `model-settings:${id}`}
            onDelete={handleDeleteModel}
            onSaveSettings={handleSaveModelSettings}
          />
        </section>

        {/* ── 4. 默认模型档位：只留选择栏位与当前值 ── */}
        <section className="border-t pt-4">
          <h3 className="mb-1 text-sm font-semibold">默认模型</h3>
          <p className="text-muted-foreground mb-3 text-xs leading-relaxed">
            为大/中/小档位与对话、向量用途指派默认模型（写入 llm.yaml defaults）；
            在此只做指派，不列出模型清单。
          </p>
          <div className="space-y-2">
            <DefaultModelSelect
              id="default-chat"
              label="默认对话模型"
              value={defaultsDraft.chat}
              models={modelIds}
              onChange={(v) => setDefaultsDraft((p) => ({ ...p, chat: v }))}
            />
            {TIER_ROWS.map((tier) => (
              <DefaultModelSelect
                key={tier.key}
                id={`default-tier-${tier.key}`}
                label={tier.label}
                value={defaultsDraft.tiers[tier.key] ?? ''}
                models={modelIds}
                onChange={(v) =>
                  setDefaultsDraft((p) => ({ ...p, tiers: { ...p.tiers, [tier.key]: v } }))
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
              <Button size="sm" onClick={handleSaveDefaults} disabled={savingAction === 'defaults'}>
                {savingAction === 'defaults' ? '保存中...' : '保存默认模型'}
              </Button>
            </div>
          </div>
        </section>
      </div>
    </PageShell>
  )
}

/* ============================================ */
/* 共享子组件                                    */
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
            <SelectItem key={m} value={m}>
              {m}
            </SelectItem>
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
