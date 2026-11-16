/**
 * LLM 设置页提供商卡片（从 LlmSettingsPage 抽出的展示子件）
 *
 * 抽出动机：页面主体逼近巨型文件冻结线（>1000 行门禁），本件为纯展示子件
 * （props 进/回调出，无页面闭包依赖），搬移零行为变化——页面测试
 * （LlmSettingsPage.gaps/pending）经页面路径全量回归。
 */
import { useState } from 'react'
import { ChevronDown, Loader2, RefreshCw, Trash2 } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { ProviderConfig } from '@/services/api/config'

/** 提供商卡片：填 Key / 更新 Key / 拉取模型 / 并发设置 / 删除（pending 态由页面
 *  级 savingAction 派生传入，await 窗口按钮 disabled+spinner 防连点） */
export function ProviderCard({
  providerId,
  displayName,
  provider,
  keySaving,
  concurrencySaving,
  deleting,
  onUpdateKey,
  onDelete,
  onFetchModels,
  onSaveConcurrency,
}: {
  providerId: string
  displayName?: string
  provider: ProviderConfig
  /** 保存密钥进行中 */
  keySaving: boolean
  /** 保存并发设置进行中 */
  concurrencySaving: boolean
  /** 删除提供商进行中 */
  deleting: boolean
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
          <span className="truncate text-sm font-semibold">{displayName ?? providerId}</span>
          {displayName && (
            <span className="text-muted-foreground truncate font-mono text-xs">{providerId}</span>
          )}
          {provider.type && (
            <span className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 font-mono text-[10px]">
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
          <Button
            variant="destructive"
            size="xs"
            onClick={() => onDelete(providerId)}
            disabled={deleting}
          >
            {deleting ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : (
              <Trash2 className="mr-1 h-3 w-3" />
            )}
            {deleting ? '删除中...' : '删除'}
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
            autoComplete="new-password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="输入新的 API Key"
            className="h-7 w-56 text-xs"
          />
          <Button
            size="xs"
            disabled={!apiKey.trim() || keySaving}
            onClick={() => {
              onUpdateKey(providerId, apiKey, provider)
              setApiKey('')
            }}
          >
            {keySaving ? (
              <>
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                保存中...
              </>
            ) : (
              '更新 Key'
            )}
          </Button>
        </div>
      ) : (
        <div className="space-y-1.5">
          {provider.env_var && (
            <p className="text-muted-foreground text-xs">
              在 .env 设置{' '}
              <code className="bg-muted rounded px-1 font-mono">{provider.env_var}</code>
              ，或直接填入：
            </p>
          )}
          <div className="flex items-center gap-2">
            <Input
              type="password"
              autoComplete="new-password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="填入 API Key（自动写入 .env，立即生效）"
              className="h-7 flex-1 text-xs"
              aria-label={`填写 ${providerId} 的 API Key`}
            />
            <Button
              size="xs"
              disabled={!apiKey.trim() || keySaving}
              onClick={() => {
                onUpdateKey(providerId, apiKey, provider)
                setApiKey('')
              }}
            >
              {keySaving ? (
                <>
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                  保存中...
                </>
              ) : (
                '保存'
              )}
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
          <ChevronDown
            className={`mr-1 h-3 w-3 transition-transform ${showAdvanced ? 'rotate-180' : ''}`}
          />
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
            <Button
              size="xs"
              onClick={() => onSaveConcurrency(providerId, maxConcurrent, rpm)}
              disabled={concurrencySaving}
            >
              {concurrencySaving ? '保存中...' : '保存并发设置'}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
