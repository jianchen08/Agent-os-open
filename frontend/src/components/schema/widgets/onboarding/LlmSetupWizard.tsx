/**
 * llm_setup 配置向导 — 「开始使用 · 配置模型与 API」内嵌向导。
 *
 * 写入与设置页走同一服务层（services/api/config.ts：updateProviderConfig /
 * addModel / saveDefaults，Key 语义 = 后端写 .env + yaml 保持 ${VAR} 引用）——
 * 本组件只是同一配置 API 的线性入口，不是第二套表单
 * （docs/decisions/2026-09-24-onboarding-plugin.md 决策 4）。
 * 范围：预置提供商 + Key + 默认模型；自定义提供商/高级参数一律走「高级设置」。
 */

import { useEffect, useState } from 'react'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { toast } from '@/components/ui/sonner'
import {
  addModel,
  getLLMConfig,
  getLLMPresets,
  getRemoteModels,
  saveDefaults,
  updateProviderConfig,
} from '@/services/api/config'
import type { LLMPresets, LLMConfigResponse, RemoteModel } from '@/services/api/config'
import type { ReactNode } from 'react'

const WIZARD_STEPS = ['选择提供商', '填写 API Key', '选择默认模型'] as const

/** Key 是否已配置：非空且非 llm.yaml 的 ${VAR} 占位（与设置页展示口径一致） */
function hasKey(config: LLMConfigResponse, providerId: string): boolean {
  return !!config.providers[providerId]?.keys?.some(
    (k) => typeof k.api_key === 'string' && k.api_key.length > 0 && !k.api_key.startsWith('${'),
  )
}

export function LlmSetupWizard({
  onConfigured,
  onOpenAdvanced,
}: {
  onConfigured: () => void
  onOpenAdvanced: () => void
}): ReactNode {
  const [config, setConfig] = useState<LLMConfigResponse | null>(null)
  const [presets, setPresets] = useState<LLMPresets | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [step, setStep] = useState(0)
  const [providerId, setProviderId] = useState<string | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [models, setModels] = useState<RemoteModel[] | null>(null)
  const [selectedModel, setSelectedModel] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([getLLMConfig(), getLLMPresets()])
      .then(([cfg, ps]) => {
        if (cancelled) return
        setConfig(cfg)
        setPresets(ps)
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError((e as Error).message || '配置读取失败')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (loadError) return <ErrorState message={loadError} />
  if (!config || !presets) return <LoadingState />

  // 预置组 ∩ 已声明 provider（工厂 llm.yaml 已预声明全部预置厂商；
  // 未声明者属自定义场景，走「高级设置」）
  const presetIds = [
    ...new Set(presets.provider_groups.flatMap((g) => g.providers.map(([id]) => id))),
  ].filter((id) => id in config.providers)

  if (presetIds.length === 0) {
    return (
      <div className="text-muted-foreground flex flex-col items-start gap-3 py-2 text-sm">
        <span>未发现预置提供商声明，请从设置页手动配置。</span>
        <Button variant="outline" size="sm" onClick={onOpenAdvanced}>
          打开高级设置
        </Button>
      </div>
    )
  }

  const saveKeyAndFetchModels = async (): Promise<void> => {
    if (!providerId || !apiKey.trim()) return
    setBusy(true)
    try {
      const firstKey = config.providers[providerId]?.keys?.[0]
      const providers = await updateProviderConfig(providerId, {
        keys: [{ id: firstKey?.id ?? `${providerId}_main`, api_key: apiKey.trim() }],
      })
      setConfig((prev) => (prev ? { ...prev, providers } : prev))
      const { models: remote } = await getRemoteModels(providerId)
      setModels(remote)
      setStep(2)
    } catch (e) {
      toast.error('保存密钥或拉取模型失败', { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  const applyDefaultModel = async (): Promise<void> => {
    if (!providerId || !selectedModel) return
    setBusy(true)
    try {
      if (!(selectedModel in config.models)) {
        await addModel(selectedModel, {
          provider: providerId,
          model_name: selectedModel,
          display_name: selectedModel,
        })
      }
      await saveDefaults({ chat: selectedModel })
      setStep(3)
      onConfigured()
    } catch (e) {
      toast.error('设置默认模型失败', { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="border-border bg-background/50 rounded-lg border p-4" data-testid="llm-setup-wizard">
      <div className="text-muted-foreground mb-3 flex items-center gap-2 text-xs">
        {WIZARD_STEPS.map((label, i) => (
          <span
            key={label}
            className={i === step ? 'text-foreground font-medium' : undefined}
          >
            {i + 1}. {label}
            {i < WIZARD_STEPS.length - 1 && <span className="mx-1.5">→</span>}
          </span>
        ))}
      </div>

      {step === 0 && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {presetIds.map((id) => (
            <button
              key={id}
              type="button"
              onClick={() => {
                setProviderId(id)
                setStep(1)
              }}
              className="border-border hover:border-primary bg-card flex flex-col items-start gap-0.5 rounded-lg border p-2.5 text-left"
            >
              <span className="text-foreground text-sm font-medium">
                {presets.provider_groups.flatMap((g) => g.providers).find(([pid]) => pid === id)?.[1] ?? id}
              </span>
              <span className="text-muted-foreground text-xs">
                {hasKey(config, id) ? '已配置 Key' : '未配置'}
              </span>
            </button>
          ))}
        </div>
      )}

      {step === 1 && providerId && (
        <div className="flex flex-col gap-2">
          <div className="text-foreground text-sm">
            填写 {providerId} 的 API Key（保存后写入本地 .env，不会出现在配置文件明文里）
          </div>
          <div className="flex gap-2">
            <Input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              data-testid="wizard-api-key"
            />
            <Button size="sm" disabled={busy || !apiKey.trim()} onClick={() => void saveKeyAndFetchModels()}>
              保存并获取模型
            </Button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="flex flex-col gap-2">
          {models && models.length > 0 ? (
            <div className="max-h-40 overflow-y-auto rounded-md border" data-testid="wizard-models">
              {models.map((m) => (
                <label key={m.id} className="hover:bg-accent flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm">
                  <input
                    type="radio"
                    name="wizard-model"
                    checked={selectedModel === m.id}
                    onChange={() => setSelectedModel(m.id)}
                  />
                  <span className="text-foreground">{m.id}</span>
                </label>
              ))}
            </div>
          ) : (
            <div className="text-muted-foreground text-sm">该提供商未返回模型列表，可跳过本向导稍后在设置页手动添加。</div>
          )}
          <Button size="sm" disabled={busy || !selectedModel} onClick={() => void applyDefaultModel()}>
            设为默认模型
          </Button>
        </div>
      )}

      {step === 3 && (
        <div className="text-foreground flex flex-col items-start gap-1 text-sm" data-testid="wizard-done">
          <span>✅ 配置完成：{providerId} / {selectedModel} 已设为默认模型。</span>
          <span className="text-muted-foreground">如需调整或换用其他模型，前往高级设置。</span>
        </div>
      )}

      <div className="text-muted-foreground mt-3 flex items-center justify-between text-xs">
        <button type="button" className="underline-offset-2 hover:underline" onClick={onOpenAdvanced}>
          高级设置（自定义提供商 / 高级参数）
        </button>
        <span>自定义 Key 仅存本地，不会上传</span>
      </div>
    </div>
  )
}
