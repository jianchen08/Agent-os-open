/**
 * 提供商详情面板（选中某个内置提供商后在其下方展示）
 *
 * 内置提供商的 Base URL / 类型来自配置，选中即用——URL 只读展示，无需填写；
 * API Key 满足「已配置」时不再要求填写（仅在未配置时提供填写入口，已配置时
 * 折叠为「更换 Key」入口，保留原能力）。并发/RPM 与删除提供商能力一并保留。
 */
import { useEffect, useState } from 'react'
import { ChevronDown, Loader2, Trash2 } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { ProviderConfig } from '@/services/api/config'

/** 提供商详情：Key 状态/填写、并发设置、删除（pending 态由页面级 savingAction 派生） */
export function ProviderDetailPanel({
  providerId,
  displayName,
  provider,
  keySaving,
  concurrencySaving,
  deleting,
  onUpdateKey,
  onDelete,
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
  onSaveConcurrency: (id: string, maxConcurrent: number, rpm: number) => void
}) {
  const [apiKey, setApiKey] = useState('')
  const [showKeyInput, setShowKeyInput] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [maxConcurrent, setMaxConcurrent] = useState(provider.keys?.[0]?.max_concurrent ?? 6)
  const [rpm, setRpm] = useState(provider.keys?.[0]?.rpm ?? 10)

  // has_key 由后端按 ${VAR} 能否解析出真实 key 判定；后端未提供该字段时退回非空判断
  const hasKey = provider.has_key ?? Boolean(provider.keys?.[0]?.api_key)
  const firstKey = provider.keys?.[0]
  const maskedKey = firstKey?.api_key ?? '未设置'
  // 本地部署提供商（ollama）无需 Key，不展示 Key 相关控件
  const needsNoKey = providerId === 'ollama'
  // 已配置时默认不展开输入框（选中内置提供商无需再填 Key）；未配置时直接给输入入口
  const keyInputVisible = !needsNoKey && (!hasKey || showKeyInput)

  // 保存落定：清空输入并收合输入区（await 窗口内保持展开，pending 态与结果可见）
  const [savedWatch, setSavedWatch] = useState(false)
  useEffect(() => {
    if (keySaving) {
      setSavedWatch(true)
      return
    }
    if (savedWatch) {
      setSavedWatch(false)
      setApiKey('')
      setShowKeyInput(false)
    }
  }, [keySaving, savedWatch])

  return (
    <div className="bg-card space-y-2 rounded-lg border px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="truncate text-sm font-semibold">{displayName ?? providerId}</span>
        {displayName && (
          <span className="text-muted-foreground truncate font-mono text-xs">{providerId}</span>
        )}
        {provider.type && (
          <span className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 font-mono text-[10px]">
            {provider.type}
          </span>
        )}
        <span
          className={`rounded px-2 py-0.5 text-xs ${hasKey || needsNoKey ? 'bg-status-success/10 text-status-success' : 'bg-status-warning/10 text-status-warning'}`}
        >
          {needsNoKey ? '无需 Key' : hasKey ? '已配置' : '未配置'}
        </span>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {hasKey && !needsNoKey && !showKeyInput && (
            <Button size="xs" variant="ghost" onClick={() => setShowKeyInput(true)}>
              更换 Key
            </Button>
          )}
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

      {/* Base URL 只读展示：内置提供商的端点随配置自带，不需要填写 */}
      {provider.api_base && (
        <div className="text-muted-foreground text-xs">Base URL: {provider.api_base}</div>
      )}

      {keyInputVisible && (
        <div className="space-y-1.5">
          {hasKey ? (
            <p className="text-muted-foreground font-mono text-xs">当前 Key: {maskedKey}</p>
          ) : (
            <p className="text-muted-foreground text-xs">
              {provider.env_var ? (
                <>
                  该提供商尚未配置 Key——可在 .env 设置{' '}
                  <code className="bg-muted rounded px-1 font-mono">{provider.env_var}</code>
                  ，或在此填入：
                </>
              ) : (
                '该提供商尚未配置 Key，在此填入后即可使用：'
              )}
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
              onClick={() => onUpdateKey(providerId, apiKey, provider)}
            >
              {keySaving ? (
                <>
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                  保存中...
                </>
              ) : (
                '保存 Key'
              )}
            </Button>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
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
