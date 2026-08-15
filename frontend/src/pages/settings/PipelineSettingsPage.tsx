/**
 * 管道配置设置页面（0.2 多循环体可视化编辑器）
 *
 * - 编辑对象固定为内核实际执行的 config/pipelines/autonomous.yaml
 *   （0.2 多循环体格式；0.1 扁平格式 default/l1-* 等 yaml 文件保留给
 *   Python 侧消费，不再提供 UI 入口）
 * - 可视化视图：PipelineFlowEditor——按循环体 → step → 插件组合编排，
 *   数据为 GET 返回的 raw JSON，path 不可变更新，未知字段保存不丢
 * - 源码视图：ConfigObject 通用表单兜底（配置非 0.2 格式时自动切到本视图）
 * - 插件目录：fetchPipelinePluginCatalog()（role/enabled/config_files）；
 *   目录获取失败不阻塞编辑，引用降级为「未命中」chip
 * - 读写：GET/PUT /api/v1/config/pipelines/autonomous（P7 端点原子写，
 *   无 If-Match；PUT 后引擎需重启生效——启动期加载，无热重载）
 *
 * @param embedded 嵌入设置主页右侧面板时为 true（去掉独立全屏头）
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Code, Eye, Loader2 } from '@/assets/icons'
import { ConfigObject } from '@/components/config/PluginConfigEditor'
import { PipelineFlowEditor } from '@/components/pipeline/PipelineFlowEditor'
import { PageShell } from '@/components/shared/PageShell'
import { Button } from '@/components/ui/button'
import { toast } from '@/components/ui/sonner'
import {
  getPipelineConfig,
  savePipelineConfig,
} from '@/services/api/pipelineConfig'
import { fetchPipelinePluginCatalog } from '@/services/api/pipelines'
import {
  deleteAtPath,
  insertAtPath,
  isPipelineV2Data,
  moveArrayItem,
  setAtPath,
} from '@/services/pipeline/model'
import type { PipelinePluginCatalogEntry } from '@/services/api/pipelines'
import type { Path, PipelineEditorOps } from '@/services/pipeline/model'

/** 内核实际执行的唯一管道（pipeline_loader 启动期硬编码加载） */
const PIPELINE_NAME = 'autonomous'

/** 保存状态 */
type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/** 视图模式：可视化 / 源码 JSON */
type ViewMode = 'visual' | 'raw'

/**
 * 管道配置设置页组件
 *
 * @param embedded 嵌入设置主页右侧面板时为 true（去掉独立全屏头）
 */
export function PipelineSettingsPage({ embedded = false }: { embedded?: boolean }) {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [catalog, setCatalog] = useState<PipelinePluginCatalogEntry[]>([])
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [viewMode, setViewMode] = useState<ViewMode>('visual')

  // 加载配置 + 插件目录（目录失败不阻塞配置编辑）
  useEffect(() => {
    let cancelled = false
    setIsLoading(true)
    setLoadError(null)
    setSaveState('idle')

    getPipelineConfig(PIPELINE_NAME)
      .then((result) => {
        if (cancelled) return
        const data = result.data ?? {}
        setConfig(data)
        // 非 0.2 格式（无 loop_bodies）时可视化无从渲染，自动落源码视图
        if (!isPipelineV2Data(data)) setViewMode('raw')
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

    fetchPipelinePluginCatalog()
      .then((entries) => {
        if (cancelled) return
        setCatalog(entries)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setCatalogError(error instanceof Error ? error.message : '插件目录获取失败')
      })

    return () => {
      cancelled = true
    }
  }, [])

  // 可视化编辑 ops：raw data 的 path 不可变更新（未知字段不丢）
  const ops: PipelineEditorOps = useMemo(
    () => ({
      set: (path: Path, value: unknown) =>
        setConfig((prev) => (prev === null ? prev : setAtPath(prev, path, value))),
      remove: (path: Path) =>
        setConfig((prev) => (prev === null ? prev : deleteAtPath(prev, path))),
      insert: (arrayPath: Path, index: number, value: unknown) =>
        setConfig((prev) => (prev === null ? prev : insertAtPath(prev, arrayPath, index, value))),
      move: (arrayPath: Path, index: number, delta: -1 | 1) =>
        setConfig((prev) => (prev === null ? prev : moveArrayItem(prev, arrayPath, index, delta))),
    }),
    [],
  )

  // ── 源码视图（ConfigObject 通用表单）的 path 更新回调 ──
  const handleChange = useCallback((path: string[], value: unknown) => {
    setConfig((prev) => (prev === null ? prev : setAtPath(prev, path, value)))
  }, [])

  const handleDelete = useCallback((path: string[]) => {
    setConfig((prev) => (prev === null ? prev : deleteAtPath(prev, path)))
  }, [])

  const handleAddField = useCallback((parentPath: string[], key: string, value: unknown) => {
    if (!key) return
    setConfig((prev) => (prev === null ? prev : setAtPath(prev, [...parentPath, key], value)))
  }, [])

  const handleSave = useCallback(async () => {
    if (!config) return
    setSaveState('saving')
    try {
      await savePipelineConfig(PIPELINE_NAME, config)
      setSaveState('saved')
      toast.success('管道配置已保存', {
        description: '已写入 config/pipelines/autonomous.yaml，重启内核后生效（启动期加载，无热重载）',
      })
      setTimeout(() => setSaveState('idle'), 2000)
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : '保存配置时发生错误'
      setSaveState('error')
      toast.error('配置保存失败', { description: msg })
    }
  }, [config])

  const visualAvailable = isPipelineV2Data(config)

  const viewToggle = (
    <div
      className="border-border inline-flex overflow-hidden rounded-lg border text-xs"
      role="tablist"
      aria-label="视图模式"
    >
      <button
        type="button"
        role="tab"
        aria-selected={viewMode === 'visual'}
        onClick={() => visualAvailable && setViewMode('visual')}
        className={`flex items-center gap-1 px-2.5 py-1.5 ${
          viewMode === 'visual'
            ? 'bg-[var(--hover-overlay)] text-foreground'
            : 'text-muted-foreground hover:text-foreground'
        } ${!visualAvailable ? 'cursor-not-allowed opacity-40' : ''}`}
        title={visualAvailable ? '按循环体 / step / 插件组合可视化' : '配置非 0.2 格式，无 loop_bodies'}
      >
        <Eye className="h-3.5 w-3.5" />
        可视化
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={viewMode === 'raw'}
        onClick={() => setViewMode('raw')}
        className={`flex items-center gap-1 border-l border-border px-2.5 py-1.5 ${
          viewMode === 'raw'
            ? 'bg-[var(--hover-overlay)] text-foreground'
            : 'text-muted-foreground hover:text-foreground'
        }`}
      >
        <Code className="h-3.5 w-3.5" />
        源码
      </button>
    </div>
  )

  const body = (
    <>
      {/* 工具行：视图切换 + 保存 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        {viewToggle}
        <div className="ml-auto flex items-center gap-3">
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
          <Button onClick={handleSave} disabled={saveState === 'saving'} size="sm">
            {saveState === 'saving' ? (
              <>
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                保存中...
              </>
            ) : (
              '保存配置'
            )}
          </Button>
        </div>
      </div>

      {!visualAvailable && !isLoading && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          配置不含 loop_bodies（非 0.2 多循环体格式），已切到源码视图。
        </div>
      )}
      {catalogError && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          插件目录获取失败（{catalogError}）——插件引用将以「未命中」样式展示，仍可手动编辑与保存。
        </div>
      )}

      {isLoading && (
        <div className="text-muted-foreground flex items-center justify-center py-20 text-sm">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          加载配置...
        </div>
      )}

      {loadError && !isLoading && (
        <div className="mb-4 rounded-lg bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
          {loadError}
        </div>
      )}

      {!isLoading && config && viewMode === 'visual' && visualAvailable && (
        <PipelineFlowEditor data={config} ops={ops} catalog={catalog} />
      )}

      {!isLoading && config && viewMode === 'raw' && (
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
        </>
      )}

      <div className="text-muted-foreground mt-4 border-t pt-3 text-[10px]">
        保存写入 config/pipelines/{PIPELINE_NAME}.yaml（原子写，重启内核后生效）；
        写回后 YAML 键序按字母重排，不影响引擎解析。
      </div>
    </>
  )

  if (embedded) {
    return (
      <PageShell
        title="管道配置"
        description="多循环体管道编排：循环体 → step → 插件组合（config/pipelines/autonomous.yaml）"
        embedded
      >
        {body}
      </PageShell>
    )
  }

  return (
    <PageShell
      title="管道配置"
      description="多循环体管道编排：循环体 → step → 插件组合（config/pipelines/autonomous.yaml）"
      backHref="/settings"
      backLabel="返回设置"
    >
      {body}
    </PageShell>
  )
}
