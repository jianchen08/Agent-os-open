/**
 * 管道 step 节点卡片（0.2 可视化编辑器的核心单元）。
 *
 * 一个 step = 一个配置单元：头部（id + 徽标 + 排序/删除）+ 插件组合区
 * （steps 引用 chips，增删/排序/添加，目录解析失败与动态模板降级展示）
 * + 折叠详情（context 键值 / step 级 loop_config / routes 路由分支）。
 *
 * 编辑基于 raw data 的 path 不可变更新（ops），本组件不持有配置状态。
 */

import { useMemo, useState } from 'react'
import {
  ChevronDown,
  ChevronUp,
  Plus,
  RotateCcw,
  Settings,
  Trash2,
  Zap,
} from '@/assets/icons'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { resolveRef, normalizeStepRef } from '@/services/pipeline/model'
import { openWorkspacePanel } from '@/services/workspacePanelOpener'
import { KeyValueEditor } from './KeyValueEditor'
import { PluginPickerDialog } from './PluginPickerDialog'
import { PipeHooksDisplay } from './PipeHooksDisplay'
import { RouteRulesEditor } from './RouteRulesEditor'
import type { PipelinePluginCatalogEntry } from '@/services/api/pipelines'
import type { Path, PipelineEditorOps, PipelineStepV2 } from '@/services/pipeline/model'

/** role 徽标颜色（input/core/output/未知） */
const ROLE_BADGE: Record<string, { label: string; className: string }> = {
  input: {
    label: 'input',
    className: 'text-[var(--ds-accent-primary,#22D3EE)] border-[rgba(34,211,238,0.35)]',
  },
  core: {
    label: 'core',
    className: 'text-[var(--ds-accent-ai,#A78BFA)] border-[rgba(167,139,250,0.35)]',
  },
  output: {
    label: 'output',
    className: 'text-status-success border-[rgba(74,222,128,0.35)]',
  },
}

/** 去掉 pipeline_ 前缀的短名（完整 id 放 title） */
function shortName(id: string): string {
  return id.startsWith('pipeline_') ? id.slice('pipeline_'.length) : id
}

/** 单条 steps 引用的 chip（含上移/下移/移除；插件类附配置深链） */
function RefChip({
  itemRef,
  when,
  gated,
  kind,
  entry,
  onRemove,
  onMoveUp,
  onMoveDown,
  onOpenConfig,
}: {
  itemRef: string
  /** G9 项级 when 门条件（对象条目专属） */
  when?: string
  /** 原始条目是否为对象形态（when 门） */
  gated?: boolean
  kind: 'plugin' | 'step' | 'template' | 'unknown'
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 目录条目仅取显示字段
  entry?: any
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
  onOpenConfig?: () => void
}) {
  const base =
    'group inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs'
  const gateBadge = gated ? (
    <span
      className="text-muted-foreground shrink-0 text-[10px] font-mono"
      title={`项级 when 门：${when ?? '（空）'}`}
    >
      ?when
    </span>
  ) : null

  if (kind === 'template') {
    return (
      <span
        className={`${base} border-dashed border-border text-muted-foreground`}
        title={`动态插件引用：运行期渲染（${itemRef}）`}
      >
        <Zap className="h-3 w-3 shrink-0" />
        <span className="truncate font-mono">{itemRef}</span>
        {gateBadge}
        <ChipActions onRemove={onRemove} onMoveUp={onMoveUp} onMoveDown={onMoveDown} />
      </span>
    )
  }

  if (kind === 'step') {
    return (
      <span
        className={`${base} border-border text-foreground`}
        title={`组合节点引用：递归执行 step "${itemRef}"`}
      >
        <span className="text-muted-foreground shrink-0 text-[10px]">step</span>
        <span className="truncate font-mono">{itemRef}</span>
        {gateBadge}
        <ChipActions onRemove={onRemove} onMoveUp={onMoveUp} onMoveDown={onMoveDown} />
      </span>
    )
  }

  if (kind === 'unknown') {
    return (
      <span
        className={`${base} border-[rgba(251,191,36,0.4)] text-status-warning`}
        title={`未在插件目录与 step 清单中命中：可能是公共 step 库 id（config/steps/*）或未注册插件（${itemRef}）`}
      >
        <span className="shrink-0 text-[10px]">?</span>
        <span className="truncate font-mono">{itemRef}</span>
        {gateBadge}
        <ChipActions onRemove={onRemove} onMoveUp={onMoveUp} onMoveDown={onMoveDown} />
      </span>
    )
  }

  const role = ROLE_BADGE[entry?.role ?? ''] ?? {
    label: entry?.role ?? '插件',
    className: 'text-muted-foreground border-border',
  }
  // 启用状态三态（FE7）：true=已启用 / false=已禁用 / null=状态侧缺失（未知）
  const enabledState: 'enabled' | 'disabled' | 'unknown' =
    entry?.enabled === false ? 'disabled' : entry?.enabled == null ? 'unknown' : 'enabled'
  const enabledTitle: Record<typeof enabledState, string> = {
    enabled: itemRef,
    disabled: `${itemRef}（插件当前已禁用，需在插件管理中启用后参与执行）`,
    unknown: `${itemRef}（启用状态未知：状态接口未返回该插件）`,
  }
  return (
    <span
      className={`${base} ${role.className} bg-[var(--hover-overlay)]`}
      title={enabledTitle[enabledState]}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          enabledState === 'disabled'
            ? 'bg-[var(--status-pending)]'
            : enabledState === 'unknown'
              ? 'bg-muted-foreground/50'
              : 'bg-status-success'
        }`}
        aria-label={enabledState === 'disabled' ? '已禁用' : enabledState === 'unknown' ? '状态未知' : '已启用'}
      />
      <span className="text-foreground truncate font-medium">{shortName(itemRef)}</span>
      <span className={`shrink-0 text-[10px] ${role.className}`}>{role.label}</span>
      {gateBadge}
      {onOpenConfig && (
        <button
          type="button"
          onClick={onOpenConfig}
          className="text-muted-foreground hover:text-foreground rounded p-0.5 opacity-60 group-hover:opacity-100"
          aria-label={`配置 ${itemRef}`}
          title="打开插件配置"
        >
          <Settings className="h-3 w-3" />
        </button>
      )}
      <ChipActions onRemove={onRemove} onMoveUp={onMoveUp} onMoveDown={onMoveDown} />
    </span>
  )
}

/** chip 尾部的排序/移除按钮组 */
function ChipActions({
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
}) {
  return (
    <span className="flex shrink-0 items-center">
      <button
        type="button"
        onClick={onMoveUp}
        className="text-muted-foreground hover:text-foreground rounded p-0.5 opacity-60 group-hover:opacity-100"
        aria-label="上移"
        title="上移"
      >
        <ChevronUp className="h-3 w-3" />
      </button>
      <button
        type="button"
        onClick={onMoveDown}
        className="text-muted-foreground hover:text-foreground rounded p-0.5 opacity-60 group-hover:opacity-100"
        aria-label="下移"
        title="下移"
      >
        <ChevronDown className="h-3 w-3" />
      </button>
      <button
        type="button"
        onClick={onRemove}
        className="text-muted-foreground hover:text-status-error rounded p-0.5 opacity-60 group-hover:opacity-100"
        aria-label="移除"
        title="从组合移除"
      >
        <Trash2 className="h-3 w-3" />
      </button>
    </span>
  )
}

/**
 * step 节点卡片。
 *
 * @param step raw data 中该 step 的类型视图
 * @param stepPath 该 step 对象在 raw data 中的路径
 * @param stepIndex 在父 steps 数组中的下标（排序按钮禁用边界）
 * @param totalSteps 父 steps 数组长度
 * @param ops 编辑器操作集
 * @param catalog 插件目录（引用解析 + 添加弹窗）
 * @param knownStepIds 管道内全部 step id（引用分类 + 路由目标）
 * @param knownPhaseIds 全部循环体 id（路由 phase 目标）
 */
export function StepNode({
  step,
  stepPath,
  stepIndex,
  totalSteps,
  ops,
  catalog,
  knownStepIds,
  knownPhaseIds,
}: {
  step: PipelineStepV2
  stepPath: Path
  stepIndex: number
  totalSteps: number
  ops: PipelineEditorOps
  catalog: PipelinePluginCatalogEntry[]
  knownStepIds: string[]
  knownPhaseIds: string[]
}) {
  const [detailOpen, setDetailOpen] = useState(true)
  const [pickerOpen, setPickerOpen] = useState(false)

  const knownStepIdSet = useMemo(() => new Set(knownStepIds), [knownStepIds])
  const stepsPath: Path = [...stepPath, 'steps']
  const refs = Array.isArray(step?.steps) ? step.steps : []

  // 插件配置深链：设置中枢工作区页签（无独立路由页）。
  // 每个插件配置文件一个稳定 tab id，重复点击激活已有页签（initialActive 只在首开生效）
  const openPluginConfig = (pluginId: string, fileId: string) => {
    openWorkspacePanel({
      id: `ws-plugin-config-${pluginId}-${fileId}`,
      title: `配置 ${fileId}`,
      component: 'settings_hub',
      icon: 'settings',
      moduleId: '__panel_settings__',
      props: { initialActive: `plugin:${pluginId}:${fileId}` },
    })
  }

  return (
    <div
      className="border-border bg-[var(--ds-bg-panel,#0A1226)] rounded-xl border p-3"
      data-testid={`step-node-${step?.id ?? stepIndex}`}
    >
      {/* 头部：id + 徽标 + 排序/删除 */}
      <div className="mb-2.5 flex flex-wrap items-center gap-2">
        <Input
          value={typeof step?.id === 'string' ? step.id : ''}
          onChange={(e) => ops.set([...stepPath, 'id'], e.target.value)}
          className="h-7 w-44 border-transparent bg-transparent px-1 font-mono text-sm font-semibold focus-visible:border"
          aria-label="step id"
          spellCheck={false}
        />
        <Badge variant="secondary" className="text-[10px]">
          {refs.length} 个引用
        </Badge>
        {step?.loop_config?.enabled && (
          <Badge variant="info" className="gap-1 text-[10px]">
            <RotateCcw className="h-3 w-3" />
            step 循环
            {typeof step.loop_config.max_iterations === 'number' && (
              <span className="font-mono">
                {step.loop_config.max_iterations === -1
                  ? ' ∞'
                  : ` ×${step.loop_config.max_iterations}`}
              </span>
            )}
          </Badge>
        )}
        <div className="ml-auto flex items-center">
          <button
            type="button"
            onClick={() => setDetailOpen((v) => !v)}
            className="text-muted-foreground hover:text-foreground rounded px-1.5 py-0.5 text-xs"
            aria-expanded={detailOpen}
          >
            {detailOpen ? '收起' : '展开'}
          </button>
          <button
            type="button"
            onClick={() => ops.move(stepPath.slice(0, -1), stepIndex, -1)}
            disabled={stepIndex === 0}
            className="text-muted-foreground hover:text-foreground rounded p-0.5 disabled:opacity-30"
            aria-label={`step ${step?.id} 上移`}
            title="上移"
          >
            <ChevronUp className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => ops.move(stepPath.slice(0, -1), stepIndex, 1)}
            disabled={stepIndex === totalSteps - 1}
            className="text-muted-foreground hover:text-foreground rounded p-0.5 disabled:opacity-30"
            aria-label={`step ${step?.id} 下移`}
            title="下移"
          >
            <ChevronDown className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => ops.remove(stepPath)}
            className="text-muted-foreground hover:text-status-error rounded p-0.5"
            aria-label={`删除 step ${step?.id}`}
            title="删除 step"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* 插件组合区 */}
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="插件组合">
        {refs.map((rawEntry, i) => {
          // 条目两态归一化：字符串直引 / G9 项级 when 门对象 {name, when}；
          // 畸形条目（无 name 等）降级展示，不崩溃
          const normalized = normalizeStepRef(rawEntry)
          if (normalized === undefined) {
            return (
              <span
                key={`malformed-${i}`}
                className="group inline-flex max-w-full items-center gap-1.5 rounded-full border border-[rgba(251,191,36,0.4)] px-2 py-0.5 text-xs text-status-warning"
                title={`无法识别的 steps 条目（缺 name 字段）：${JSON.stringify(rawEntry)}`}
              >
                <span className="shrink-0 text-[10px]">!</span>
                <span className="truncate font-mono">条目 #{i + 1}</span>
                <ChipActions
                  onRemove={() => ops.remove([...stepsPath, i])}
                  onMoveUp={() => ops.move(stepsPath, i, -1)}
                  onMoveDown={() => ops.move(stepsPath, i, 1)}
                />
              </span>
            )
          }
          const resolution = resolveRef(normalized.name, catalog, knownStepIdSet)
          const entry = resolution.catalogEntry as PipelinePluginCatalogEntry | undefined
          return (
            <RefChip
              key={`${normalized.name}-${i}`}
              itemRef={normalized.name}
              when={normalized.when}
              gated={normalized.gated}
              kind={resolution.kind}
              entry={entry}
              onRemove={() => ops.remove([...stepsPath, i])}
              onMoveUp={() => ops.move(stepsPath, i, -1)}
              onMoveDown={() => ops.move(stepsPath, i, 1)}
              onOpenConfig={
                entry && entry.configFiles.length > 0
                  ? () =>
                      openPluginConfig(entry.id, entry.configFiles[0]?.id ?? 'default')
                  : undefined
              }
            />
          )
        })}
        <button
          type="button"
          onClick={() => setPickerOpen(true)}
          className="text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)] inline-flex items-center gap-1 rounded-full border border-dashed px-2.5 py-1 text-xs"
          aria-label={`向 step ${step?.id} 添加插件`}
        >
          <Plus className="h-3.5 w-3.5" />
          添加插件
        </button>
      </div>

      {/* 折叠详情：context / step 循环 / routes */}
      {detailOpen && (
        <div className="border-border mt-3 space-y-3 border-t pt-3">
          <section>
            <h4 className="text-foreground mb-1.5 text-xs font-medium">
              context（merge 进 state）
            </h4>
            <KeyValueEditor
              value={step?.context}
              onChange={(next) => {
                if (next === undefined) {
                  ops.remove([...stepPath, 'context'])
                } else {
                  ops.set([...stepPath, 'context'], next)
                }
              }}
              emptyHint="无 context 字段"
            />
          </section>

          <section>
            <h4 className="text-foreground mb-1.5 text-xs font-medium">
              loop_config（step 自带循环）
            </h4>
            <div className="text-muted-foreground flex flex-wrap items-center gap-3 text-xs">
              <label className="flex cursor-pointer items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={step?.loop_config?.enabled === true}
                  onChange={(e) =>
                    ops.set([...stepPath, 'loop_config', 'enabled'], e.target.checked)
                  }
                  className="h-3.5 w-3.5 accent-[var(--btn-primary-bg)]"
                />
                启用循环
              </label>
              <label className="flex items-center gap-1.5">
                最大迭代
                <Input
                  type="number"
                  value={
                    typeof step?.loop_config?.max_iterations === 'number'
                      ? step.loop_config.max_iterations
                      : ''
                  }
                  onChange={(e) => {
                    const parsed = Number(e.target.value)
                    ops.set(
                      [...stepPath, 'loop_config', 'max_iterations'],
                      e.target.value === '' || Number.isNaN(parsed)
                        ? e.target.value
                        : parsed,
                    )
                  }}
                  className="h-7 w-24 font-mono text-xs"
                  aria-label="step 最大迭代次数"
                />
              </label>
              <span className="text-[10px]">-1 = 无限</span>
            </div>
          </section>

          <section>
            <h4 className="text-foreground mb-1.5 text-xs font-medium">
              hooks（step 级钩子，只读）
            </h4>
            <PipeHooksDisplay hooks={step?.hooks} scopeHint={`step:${step?.id ?? ''}`} />
          </section>

          <RouteRulesEditor
            rules={step?.routes}
            arrayPath={[...stepPath, 'routes']}
            ops={ops}
            knownStepIds={knownStepIds}
            knownPhaseIds={knownPhaseIds}
            label="routes（step 级路由分支）"
          />
        </div>
      )}

      <PluginPickerDialog
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        catalog={catalog}
        excludeIds={refs
          .map((entry) => normalizeStepRef(entry)?.name)
          .filter((name): name is string => name !== undefined)}
        onPick={(ref) => ops.insert(stepsPath, refs.length, ref)}
      />
    </div>
  )
}
