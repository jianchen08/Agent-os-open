/** API 配置页面 管理外部 API 密钥、端点、超时等配置 */

import { Loader2 } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  getAPIConfig,
  saveAPIConfig,
  type APIConfig,
  type EndpointConfig,
  type RateLimitConfig,
} from '@/services/api/config'

/** 保存状态 */
type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/** API 配置页面组件 */
export function ApiSettingsPage() {
  const [config, setConfig] = useState<APIConfig | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle')

  // 加载配置
  useEffect(() => {
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)
    getAPIConfig()
      .then((data) => {
        if (!cancelled) setConfig(data)
      })
      .catch(() => {
        if (!cancelled) {
          setLoadError('无法连接服务器加载配置，请稍后重试')
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 更新端点字段
  const updateEndpoint = useCallback(
    <K extends keyof EndpointConfig>(key: K, value: EndpointConfig[K]) => {
      setConfig((prev) => (prev ? { ...prev, endpoint: { ...prev.endpoint, [key]: value } } : prev))
    },
    [],
  )

  // 更新限流字段
  const updateRateLimit = useCallback(
    <K extends keyof RateLimitConfig>(key: K, value: RateLimitConfig[K]) => {
      setConfig((prev) =>
        prev ? { ...prev, rate_limit: { ...prev.rate_limit, [key]: value } } : prev,
      )
    },
    [],
  )

  // 更新 CORS
  const updateCorsOrigins = useCallback((value: string) => {
    const origins = value
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
    setConfig((prev) => (prev ? { ...prev, cors_origins: origins } : prev))
  }, [])

  // 保存配置
  const handleSave = useCallback(async () => {
    if (!config) return
    setSaveState('saving')
    try {
      const saved = await saveAPIConfig(config)
      setConfig(saved)
      setSaveState('saved')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch {
      setSaveState('error')
    }
  }, [config])

  // 连接测试
  const handleTestConnection = useCallback(async () => {
    setTestStatus('testing')
    try {
      const res = await fetch(`${config?.endpoint.base_url}/health`, {
        method: 'GET',
        signal: AbortSignal.timeout(5000),
      })
      setTestStatus(res.ok ? 'ok' : 'fail')
    } catch {
      setTestStatus('fail')
    }
    setTimeout(() => setTestStatus('idle'), 3000)
  }, [config?.endpoint.base_url])

  if (isLoading) {
    return (
      <SettingsPageShell title="API 配置" description="管理外部 API 密钥和端点">
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <div className="border-primary mr-2 h-5 w-5 animate-spin rounded-full border-2 border-t-transparent" />
          加载配置...
        </div>
      </SettingsPageShell>
    )
  }

  if (!config) {
    return (
      <SettingsPageShell title="API 配置" description="管理外部 API 密钥和端点">
        <div className="text-muted-foreground py-20 text-center text-sm">
          {loadError || '无法加载配置'}
        </div>
      </SettingsPageShell>
    )
  }

  return (
    <SettingsPageShell title="API 配置" description="管理外部 API 密钥和端点">
      {loadError && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          {loadError}
        </div>
      )}

      {/* 端点配置 */}
      <Section title="端点配置">
        <FieldRow label="Base URL" htmlFor="api-base-url">
          <Input
            id="api-base-url"
            value={config.endpoint.base_url}
            onChange={(e) => updateEndpoint('base_url', e.target.value)}
            placeholder="https://api.example.com"
          />
        </FieldRow>
        <FieldRow label="API Version" htmlFor="api-version">
          <Input
            id="api-version"
            value={config.endpoint.version}
            onChange={(e) => updateEndpoint('version', e.target.value)}
            placeholder="v1"
          />
        </FieldRow>
        <FieldRow label="超时时间 (秒)" htmlFor="api-timeout">
          <Input
            id="api-timeout"
            type="number"
            min={1}
            max={300}
            value={config.endpoint.timeout}
            onChange={(e) => updateEndpoint('timeout', Number(e.target.value))}
          />
        </FieldRow>
        <div className="mt-2 flex items-center gap-2">
          <Button size="sm" onClick={handleTestConnection} disabled={testStatus === 'testing'}>
            {testStatus === 'testing' ? '测试中...' : '测试连接'}
          </Button>
          {testStatus === 'ok' && <span className="text-xs text-status-success">连接成功</span>}
          {testStatus === 'fail' && <span className="text-xs text-status-error">连接失败</span>}
        </div>
      </Section>

      {/* API Key 管理 */}
      <Section title="API Key 管理">
        <p className="text-muted-foreground mb-3 text-xs">API 密钥由后端安全存储，此处仅显示状态</p>
        <div className="space-y-2">
          {['primary', 'fallback'].map((keyType) => (
            <div
              key={keyType}
              className="bg-card flex items-center gap-3 rounded-lg border px-3 py-2"
            >
              <span className="min-w-[80px] text-sm font-medium">
                {keyType === 'primary' ? '主密钥' : '备用密钥'}
              </span>
              <span className="text-muted-foreground flex-1 font-mono text-xs">
                ••••••••••••••••
              </span>
              <span className="rounded bg-status-success/10 px-2 py-0.5 text-xs text-status-success">
                已配置
              </span>
            </div>
          ))}
        </div>
      </Section>

      {/* 限流配置 */}
      <Section title="限流配置">
        <FieldRow label="全局限流" htmlFor="rl-global">
          <Input
            id="rl-global"
            value={config.rate_limit.global_limit}
            onChange={(e) => updateRateLimit('global_limit', e.target.value)}
            placeholder="100/minute"
          />
        </FieldRow>
        <FieldRow label="认证限流" htmlFor="rl-auth">
          <Input
            id="rl-auth"
            value={config.rate_limit.auth}
            onChange={(e) => updateRateLimit('auth', e.target.value)}
            placeholder="5/minute"
          />
        </FieldRow>
        <FieldRow label="任务限流" htmlFor="rl-tasks">
          <Input
            id="rl-tasks"
            value={config.rate_limit.tasks}
            onChange={(e) => updateRateLimit('tasks', e.target.value)}
            placeholder="20/minute"
          />
        </FieldRow>
        <FieldRow label="WebSocket 限流" htmlFor="rl-ws">
          <Input
            id="rl-ws"
            value={config.rate_limit.websocket}
            onChange={(e) => updateRateLimit('websocket', e.target.value)}
            placeholder="50/minute"
          />
        </FieldRow>
      </Section>

      {/* CORS 配置 */}
      <Section title="CORS 配置">
        <FieldRow label="允许的源 (逗号分隔)" htmlFor="cors-origins">
          <Input
            id="cors-origins"
            value={config.cors_origins.join(', ')}
            onChange={(e) => updateCorsOrigins(e.target.value)}
            placeholder="http://localhost:3000, https://example.com"
          />
        </FieldRow>
      </Section>

      {/* 保存按钮 */}
      <div className="mt-6 flex items-center gap-3 border-t pt-4">
        <Button onClick={handleSave} disabled={saveState === 'saving'}>
          {saveState === 'saving' ? (
            <>
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              保存中...
            </>
          ) : (
            '保存配置'
          )}
        </Button>
        {saveState === 'saved' && <span className="text-xs text-status-success" role="status">已保存</span>}
        {saveState === 'error' && <span className="text-xs text-status-error" role="alert">保存失败</span>}
      </div>
    </SettingsPageShell>
  )
}

/* 共享子组件 */

/** 页面外壳 */
function SettingsPageShell({
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
      <main className="max-w-3xl flex-1 overflow-y-auto p-3 sm:p-6" role="form" aria-label="API配置表单">{children}</main>
    </div>
  )
}

/** 配置段 */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-6">
      <h2 className="text-foreground mb-3 text-sm font-semibold">{title}</h2>
      <div className="space-y-3">{children}</div>
    </section>
  )
}

/** 表单行 */
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
