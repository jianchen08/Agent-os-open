/**
 * 管道配置设置页面
 *
 * 对齐 0.1 前端 PipelineSettingsPage（CategoryConfigPage tabs 模式）：
 * - 管道列表：default / l1-main / l2-evaluator / l2-subtask
 * - 读取：GET /api/v1/config/pipelines/{name}（内核 P7 端点，config_service denylist 含
 *   pipelines，不走 generic config）
 * - 编辑：复用 PluginConfigEditor 的 ConfigObject 通用表单渲染器（schema 驱动，自动按
 *   字段类型渲染 Input/checkbox/JSON textarea/嵌套对象）
 * - 保存：PUT /api/v1/config/pipelines/{name}（body { data }）
 *
 * 注意：内核 P7 管道端点（routes.rs::put_pipeline_config_handler）为原子写，无 If-Match
 * 乐观锁（插件配置端点才有），故前端不维护 etag。
 *
 * @param embedded 嵌入设置主页右侧面板时为 true（去掉独立全屏头）
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { ConfigObject } from '@/components/config/PluginConfigEditor'
import { PageShell } from '@/components/shared/PageShell'
import { Button } from '@/components/ui/button'
import { toast } from '@/components/ui/sonner'
import {
  getPipelineConfig,
  savePipelineConfig,
} from '@/services/api/pipelineConfig'

/** 管道标签页配置 */
interface PipelineTab {
  /** 管道名（对应 config/pipelines/{name}.yaml） */
  name: string
  /** 标签页标题 */
  title: string
}

/** 可配置管道列表（对齐 0.1 前端参考：default + L1/L2 管道） */
const PIPELINE_TABS: PipelineTab[] = [
  { name: 'default', title: '默认' },
  { name: 'l1-main', title: 'L1 主 Agent' },
  { name: 'l2-evaluator', title: 'L2 评估' },
  { name: 'l2-subtask', title: 'L2 子任务' },
]

/** 保存状态 */
type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/**
 * 管道配置设置页组件
 *
 * @param embedded 嵌入设置主页右侧面板时为 true（去掉独立全屏头）
 */
export function PipelineSettingsPage({ embedded = false }: { embedded?: boolean }) {
  const [activeTab, setActiveTab] = useState(0)
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')

  const currentTab = PIPELINE_TABS[activeTab]
  const pipelineName = currentTab?.name ?? ''

  // 加载当前标签页的管道配置
  useEffect(() => {
    if (!pipelineName) return
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)
    setConfig(null)
    setSaveState('idle')

    getPipelineConfig(pipelineName)
      .then((result) => {
        if (cancelled) return
        setConfig(result.data ?? {})
      })
      .catch((error: unknown) => {
        if (cancelled) return
        const msg = error instanceof Error ? error.message : '无法加载配置'
        setConfig({})
        setLoadError('无法加载配置')
        toast.error('配置加载失败', { description: msg })
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [pipelineName])

  const handleChange = useCallback((path: string[], value: unknown) => {
    setConfig((prev) => {
      if (!prev) return prev
      const root = structuredClone(prev)
      let target: Record<string, unknown> = root
      for (const seg of path.slice(0, -1)) {
        target = target[seg] as Record<string, unknown>
      }
      target[path[path.length - 1]] = value
      return root
    })
  }, [])

  const handleDelete = useCallback((path: string[]) => {
    setConfig((prev) => {
      if (!prev || path.length === 0) return prev
      const root = structuredClone(prev)
      let target: Record<string, unknown> = root
      for (const seg of path.slice(0, -1)) {
        target = target[seg] as Record<string, unknown>
      }
      delete target[path[path.length - 1]]
      return root
    })
  }, [])

  const handleAddField = useCallback((parentPath: string[], key: string, value: unknown) => {
    setConfig((prev) => {
      if (!prev || !key) return prev
      const root = structuredClone(prev)
      let target: Record<string, unknown> = root
      for (const seg of parentPath) {
        target = target[seg] as Record<string, unknown>
      }
      target[key] = value
      return root
    })
  }, [])

  const handleSave = useCallback(async () => {
    if (!config || !pipelineName) return
    setSaveState('saving')
    try {
      await savePipelineConfig(pipelineName, config)
      setSaveState('saved')
      toast.success('管道配置已保存')
      setTimeout(() => setSaveState('idle'), 2000)
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : '保存配置时发生错误'
      setSaveState('error')
      toast.error('配置保存失败', { description: msg })
    }
  }, [config, pipelineName])

  const showTabs = PIPELINE_TABS.length > 1

  const body = (
    <>
      {showTabs && (
        <nav className="mb-4 flex shrink-0 gap-1 border-b" role="tablist">
          {PIPELINE_TABS.map((tab, i) => (
            <button
              key={tab.name}
              type="button"
              role="tab"
              aria-selected={i === activeTab}
              onClick={() => setActiveTab(i)}
              className={`relative px-3 py-2 text-sm font-medium transition-colors ${
                i === activeTab
                  ? 'text-foreground'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {tab.title}
              {i === activeTab && (
                <span
                  className="bg-primary absolute bottom-0 left-0 right-0 h-0.5 rounded-t"
                />
              )}
            </button>
          ))}
        </nav>
      )}

      {isLoading && (
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          加载配置...
        </div>
      )}

      {!isLoading && loadError && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          {loadError}
        </div>
      )}

      {!isLoading && config && (
        <>
          <div role="form" aria-label="管道配置表单">
            <ConfigObject
              obj={config}
              parentPath={[]}
              onChange={handleChange}
              onDelete={handleDelete}
              onAddField={handleAddField}
            />
          </div>

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
            {saveState === 'saved' && (
              <span className="text-xs text-status-success" role="status">
                已保存
              </span>
            )}
            {saveState === 'error' && (
              <span className="text-xs text-status-error" role="alert">
                保存失败
              </span>
            )}
          </div>
        </>
      )}
    </>
  )

  if (embedded) {
    return (
      <PageShell
        title="管道配置"
        description="管道插件链与 Agent 管道配置（config/pipelines/*.yaml）"
        embedded
      >
        {body}
      </PageShell>
    )
  }

  return (
    <PageShell
      title="管道配置"
      description="管道插件链与 Agent 管道配置（config/pipelines/*.yaml）"
      backHref="/settings"
      backLabel="返回设置"
      maxWidth="max-w-3xl"
    >
      {body}
    </PageShell>
  )
}
