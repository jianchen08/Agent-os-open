/**
 * 授权区管理 widget（ADR 2026-09-24-read-deny-write-zones：设置页管理节）
 *
 * 名单 = 用户空间真值 project_whitelist.yaml（entries=写区，read_deny=读排除），
 * 经 security_check 插件 http_endpoints 读写（/ext/pipeline_security_check/zones*）。
 * 消费方：security_check 位置闸（管道层执法）与 fs_tools 工具层兜底校验。
 *
 * 声明式挂载：security_check plugin.json contributes.pages（space=settings）
 * → 设置中枢「插件页面」组 → 本组件（WidgetRegistry 按 widget id 分发）。
 */

import { useCallback, useEffect, useState } from 'react'
import { Loader2, Plus, X } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { toast } from '@/components/ui/sonner'
import apiClient from '@/services/api/client'

const ZONES_ENDPOINT = '/ext/pipeline_security_check/zones'

interface ZonePolicyData {
  entries: string[]
  read_deny: string[]
}

/** 单节名单：条目列表（带移除）+ 追加输入行 */
function ZoneSection({
  title,
  description,
  section,
  items,
  onSectionChange,
}: {
  title: string
  description: string
  section: 'entries' | 'read_deny'
  items: string[]
  onSectionChange: (section: 'entries' | 'read_deny', merged: string[]) => void
}) {
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const mutate = useCallback(
    async (action: 'add' | 'remove', path: string) => {
      if (!path.trim()) return
      setBusy(true)
      try {
        const resp = await apiClient.post<{ entries?: string[]; error?: string }>(
          `${ZONES_ENDPOINT}/${action}`,
          { section, path },
        )
        if (resp.data?.error) throw new Error(resp.data.error)
        onSectionChange(section, Array.isArray(resp.data?.entries) ? resp.data.entries : [])
        setDraft('')
      } catch (e) {
        toast.error(`授权名单${action === 'add' ? '追加' : '移除'}失败: ${e instanceof Error ? e.message : e}`)
      } finally {
        setBusy(false)
      }
    },
    [section, onSectionChange],
  )

  return (
    <section className="rounded-md border p-3" style={{ borderColor: 'var(--ds-border-subtle)' }}>
      <div className="text-foreground text-[13px] font-semibold">{title}</div>
      <div className="text-muted-foreground mt-0.5 text-[11px]">{description}</div>
      <ul className="mt-2 flex flex-col gap-1">
        {items.length === 0 && (
          <li className="text-muted-foreground text-[11px]">（空）</li>
        )}
        {items.map((item) => (
          <li key={item} className="flex items-center gap-2">
            <span className="truncate font-mono text-[11px]" title={item}>{item}</span>
            <button
              type="button"
              aria-label={`移除 ${item}`}
              disabled={busy}
              onClick={() => void mutate('remove', item)}
              className="text-muted-foreground hover:text-foreground ml-auto shrink-0 disabled:opacity-50"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </li>
        ))}
      </ul>
      <div className="mt-2 flex items-center gap-2">
        <Input
          value={draft}
          placeholder="目录绝对路径，如 D:\myproject"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void mutate('add', draft)
          }}
          className="h-7 text-[12px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={busy || !draft.trim()}
          onClick={() => void mutate('add', draft)}
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
          追加
        </Button>
      </div>
    </section>
  )
}

/** 授权区管理页（设置中枢「插件页面」组声明页） */
export function ZonePolicySettingsWidget() {
  const [data, setData] = useState<ZonePolicyData>({ entries: [], read_deny: [] })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await apiClient.get<ZonePolicyData>(ZONES_ENDPOINT)
      setData({
        entries: Array.isArray(resp.data?.entries) ? resp.data.entries : [],
        read_deny: Array.isArray(resp.data?.read_deny) ? resp.data.read_deny : [],
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  if (loading) {
    return (
      <div className="text-muted-foreground flex items-center gap-2 p-4 text-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> 加载授权名单…
      </div>
    )
  }
  if (error) {
    return (
      <div className="p-4">
        <div className="text-destructive text-sm">授权名单加载失败: {error}</div>
        <Button size="sm" variant="outline" className="mt-2" onClick={() => void reload()}>
          重试
        </Button>
      </div>
    )
  }

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 p-4">
      <div>
        <h2 className="text-foreground text-base font-semibold">授权区</h2>
        <p className="text-muted-foreground mt-1 text-[12px]">
          位置闸的执法依据：写区（entries）内的写入按会话权限档执行，区外写入会弹授权卡；
          读排除（read_deny）内的路径一律拒绝读取。名单为用户配置（agent 只读），
          真值落用户空间，应用升级不丢失。
        </p>
      </div>
      <ZoneSection
        title="写区（entries）"
        description="前缀授权：条目及其任意层级子目录内可写。登记项目根、授权卡「永久写入」都落在这份名单。"
        section="entries"
        items={data.entries}
        onSectionChange={(section, merged) =>
          setData((prev) => ({ ...prev, [section]: merged }))
        }
      />
      <ZoneSection
        title="读排除（read_deny）"
        description="前缀排除：命中条目的路径拒绝读取（凭据黑名单与系统目录之外的用户自留口子）。"
        section="read_deny"
        items={data.read_deny}
        onSectionChange={(section, merged) =>
          setData((prev) => ({ ...prev, [section]: merged }))
        }
      />
    </div>
  )
}
