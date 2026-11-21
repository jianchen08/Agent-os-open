/**
 * 产物面板（任务现场「产物区」）
 *
 * **一个条目**（用户裁定 2026-11-20：「产物文件和变更整合成一个条目，产物文件就是
 * 文件变更的集合并且这些文件要都能打开」）：本节只有「产物文件」一个区块，内部按
 * 两层呈现同一件事——
 *
 * 1. **变更文件行**（主列表）：agent 写过/删过的文件，一行一文件。首列是**文件名**
 *    （basename，取不到才回落完整路径），第二列是所在目录，状态徽标标新增/修改/删除；
 *    每行右侧固定「打开」按钮，路由到该类型的查看器（`getGlobalOpenFileCallback`）。
 * 2. **其它文件（按目录，可折叠）**：工作区里没被工具写过、但确实存在的文件
 *    （如 bash 命令/下载产出的文件）。默认收起，展开后才拉取工作区树——
 *    它是同一区块的补充视角，不另立第二块。
 *
 * 另保留「产物画面」（截图缩略图 + 灯箱）：截图是图像产物，无法归入文件变更行。
 *
 * **「未命名」缺陷根因**：上一版把 `file_tree` widget 直接嵌进来却没传
 * `nodeTitleField`，widget 默认取 `title` 字段，而 workspace 懒加载树的节点字段是
 * `name`（`plugins/shared/workspace/workspace_service.py:_list_layer`）→ 全部落
 * 「未命名」。此版显式传 `nodeTitleField="name"` 并补 `onFileClick` 打开入口。
 *
 * 复用（不自研）：`DetailBlock` 的 diff 视图、`file_tree` widget、文件打开回调、
 * `formatFileSize`、`SceneThumbnail`。
 */

import { useMemo, useState } from 'react'
import { ChevronDown, ChevronRight, ExternalLink, FolderOpen } from '@/assets/icons'
import { DetailBlock } from '@/components/chat/ActivityBlockViews'
import { FileTreeWidget } from '@/components/schema/widgets/FileTreeWidget'
import { cn } from '@/lib/utils'
import { describeOpenTarget } from '@/services/fileOpener'
import { formatFileSize } from '@/utils/format'
import { getGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import { SceneBadge } from './SceneBadge'
import {
  LARGE_FILE_BYTES,
  SCENE_CHANGE_LABELS,
  buildFileChanges,
  canInlinePreview,
  computeFileNetStat,
  summarizeChanges,
} from './sceneDiff'
import { baseNameOf, dirNameOf } from './sceneFilePaths'
import { isScreenshotActivity } from './sceneImageSource'
import { channelLabelOfTool } from './sceneInstances'
import { SceneThumbnail } from './SceneThumbnail'
import type { SceneActivity } from './sceneActivities'
import type { SceneFileChange } from './sceneDiff'
import type { FC } from 'react'

export interface TaskSceneArtifactsProps {
  /** 任务 ID（被写文件的容器坐标 + file_tree 数据源坐标） */
  taskId: string
  /** 现场活动（diff 聚合与「谁产出」的数据面） */
  activities: readonly SceneActivity[]
  /** 打开文件回调缺省走全局注册表；测试可注入 */
  onOpenFile?: (path: string) => void
}

/** 产物区截图直接铺开的张数上限（其余计数指向活动历史，不静默丢） */
const SHOT_LIMIT = 8

/** 产物面板（一个条目的产物文件集合 + 截图产物） */
export const TaskSceneArtifacts: FC<TaskSceneArtifactsProps> = ({
  taskId,
  activities,
  onOpenFile,
}) => {
  const changes = useMemo(() => buildFileChanges(activities), [activities])
  const summary = useMemo(() => summarizeChanges(changes), [changes])
  const screenshots = useMemo(
    () => activities.filter((activity) => isScreenshotActivity(activity)),
    [activities],
  )
  const [expandedPath, setExpandedPath] = useState<string | null>(null)
  /** 其它文件树：默认收起（主列表已是产物文件集合，树是补充视角不抢版面） */
  const [showTree, setShowTree] = useState(false)

  const openFile = (path: string): void => {
    if (onOpenFile) {
      onOpenFile(path)
      return
    }
    getGlobalOpenFileCallback()(path, taskId)
  }

  return (
    <div className="flex min-h-0 flex-col" data-testid="task-scene-artifacts">
      {/* ── 产物文件（唯一条目）：汇总条 + 变更文件行 + 其它文件树 ── */}
      <div className="border-border flex flex-shrink-0 flex-col border-b" data-testid="task-scene-files">
        <div
          className="flex flex-shrink-0 flex-wrap items-center gap-2 px-3 py-2"
          data-testid="task-scene-changes-summary"
        >
          <span className="text-foreground text-xs font-medium">产物文件</span>
          <span className="text-status-success text-[11px]" data-testid="task-scene-added">
            新增 {summary.added}
          </span>
          <span className="text-status-info text-[11px]" data-testid="task-scene-modified">
            修改 {summary.modified}
          </span>
          <span className="text-status-error text-[11px]" data-testid="task-scene-deleted">
            删除 {summary.deleted}
          </span>
          <span className="text-muted-foreground/60 text-[11px]" data-testid="task-scene-file-count">
            {changes.length} 个文件
          </span>
          <button
            type="button"
            onClick={() => setShowTree((prev) => !prev)}
            aria-expanded={showTree}
            className="text-muted-foreground hover:text-foreground ml-auto flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] transition-colors"
            title="按目录查看工作区里的其它文件（含未经工具写入的文件）"
            data-testid="task-scene-toggle-tree"
          >
            <FolderOpen className="h-icon-xs w-icon-xs" />
            其它文件（按目录）
            {showTree ? (
              <ChevronDown className="h-icon-xs w-icon-xs" />
            ) : (
              <ChevronRight className="h-icon-xs w-icon-xs" />
            )}
          </button>
        </div>

        {/* 变更文件行（L0 文件行 → 展开 L1/L2）。
            按内容自然高度：不 flex-1 撑满剩余高度——内容少时会被撑出几百 px
            空白并把后续区块推到屏底；内容多时随页签整体滚动。 */}
        <div className="overflow-auto" data-testid="task-scene-changes">
          {changes.length === 0 ? (
            <div className="text-muted-foreground p-3 text-center text-[11px]">
              这个任务还没有写入或删除任何文件
            </div>
          ) : (
            changes.map((change) => (
              <ChangeRow
                key={change.path}
                change={change}
                expanded={expandedPath === change.path}
                onToggle={() =>
                  setExpandedPath((prev) => (prev === change.path ? null : change.path))
                }
                onOpenFile={openFile}
              />
            ))
          )}
        </div>

        {/* 其它文件（按目录）：展开才挂载，避免默认视图为一个可能很长的树付版面与请求 */}
        {showTree && (
          <div
            className="border-border/60 max-h-[40vh] min-h-0 overflow-auto border-t px-2 pb-1"
            data-testid="task-scene-file-tree"
          >
            <FileTreeWidget
              dataSource={`workspace://${taskId}`}
              showStatus={false}
              compactStates
              nodeTitleField="name"
              nodeChildrenField="children"
              onFileClick={(filePath: string) => openFile(filePath)}
            />
          </div>
        )}
      </div>

      {/* 截图产物：实例跑出来的现场画面（缩略图 + 灯箱，复用 SceneThumbnail） */}
      {screenshots.length > 0 && (
        <div
          className="border-border flex flex-shrink-0 flex-col border-t"
          data-testid="task-scene-shots"
        >
          <div className="text-muted-foreground flex flex-shrink-0 items-center gap-2 px-3 py-1 text-[11px]">
            产物画面
            <span className="text-muted-foreground/60">{screenshots.length} 张</span>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 px-3 pb-2">
            {screenshots.slice(-SHOT_LIMIT).map((shot) => (
              <span
                key={shot.callId}
                className="flex w-20 flex-col items-center gap-0.5"
                title={`${shot.summary}（${shot.toolName}）`}
                data-testid="scene-shot-item"
              >
                <SceneThumbnail activity={shot} containerTaskId={shot.containerTaskId ?? taskId} />
                {/* 文案完整显示：容器给足宽度 + 允许折行，不再单行截断成「记...」 */}
                <span className="text-muted-foreground/70 w-full break-words text-center text-[9px] leading-tight">
                  {shot.summary}
                </span>
              </span>
            ))}
            {screenshots.length > SHOT_LIMIT && (
              <span className="text-muted-foreground/60 text-[10px]">
                另有 {screenshots.length - SHOT_LIMIT} 张（见活动历史）
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/** 单文件变更行（L0 → L1 展开 / L2 历史轮次） */
const ChangeRow: FC<{
  change: SceneFileChange
  expanded: boolean
  onToggle: () => void
  onOpenFile: (path: string) => void
}> = ({ change, expanded, onToggle, onOpenFile }) => {
  const inline = canInlinePreview(change.path)
  const large = change.size !== undefined && change.size > LARGE_FILE_BYTES
  const netStat = expanded ? computeFileNetStat(change) : undefined
  const fileName = baseNameOf(change.path)
  const dirName = dirNameOf(change.path)
  /** 打开后会进哪个查看器（提示与测试断言同源，不在 UI 里另判扩展名） */
  const target = describeOpenTarget(change.path)
  const openable = change.status !== 'deleted'

  return (
    <div className="border-border/60 border-b last:border-b-0" data-testid="scene-change-row">
      <div className="hover:bg-[var(--hover-overlay)] flex items-center gap-1.5 px-2 py-1">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          className="text-muted-foreground flex-shrink-0"
          title={expanded ? '收起' : '展开看这个文件改了什么'}
          data-testid="scene-change-toggle"
        >
          {expanded ? (
            <ChevronDown className="h-icon-sm w-icon-sm" />
          ) : (
            <ChevronRight className="h-icon-sm w-icon-sm" />
          )}
        </button>
        <span
          className={cn(
            'flex-shrink-0 rounded px-1 text-[10px] leading-4',
            change.status === 'added'
              ? 'text-status-success bg-status-success/10'
              : change.status === 'deleted'
                ? 'text-status-error bg-status-error/10'
                : 'text-status-info bg-status-info/10',
          )}
          data-testid="scene-change-status"
        >
          {SCENE_CHANGE_LABELS[change.status]}
        </span>
        {/* 文件名（主位）：真实 basename，不落「未命名」 */}
        <span
          className="text-foreground min-w-0 max-w-[45%] truncate font-mono text-[11px]"
          title={change.path}
          data-testid="scene-change-name"
        >
          {fileName}
        </span>
        {dirName && (
          <span
            className="text-muted-foreground/70 min-w-0 flex-1 truncate font-mono text-[10px]"
            title={change.path}
            data-testid="scene-change-dir"
          >
            {dirName}
          </span>
        )}

        {/* L0 增删行（末轮上报，非累计——不跑 LCS） */}
        {(change.added !== undefined || change.removed !== undefined) &&
          change.status !== 'deleted' && (
            <span className="flex-shrink-0 font-mono text-[10px]">
              <span className="text-status-success">+{change.added ?? 0}</span>{' '}
              <span className="text-status-error">-{change.removed ?? 0}</span>
            </span>
          )}
        {change.rounds.length > 1 && (
          <span className="text-muted-foreground/70 flex-shrink-0 text-[10px]">
            {change.rounds.length} 轮
          </span>
        )}
        {change.size !== undefined && (
          <span className="text-muted-foreground/70 flex-shrink-0 text-[10px]">
            {formatFileSize(change.size)}
          </span>
        )}

        {/* 悬浮标记：不能内嵌的内容必须有可见提示（点开能看） */}
        {!inline && change.status !== 'deleted' && (
          <SceneBadge
            label={large ? '大文件' : '可查看'}
            tone={large ? 'warning' : 'info'}
            title={
              large
                ? `超过 ${formatFileSize(LARGE_FILE_BYTES)}，点击在页签内按需加载`
                : '该类型不能内嵌预览，点击在工作区打开'
            }
          />
        )}

        {openable && (
          <button
            type="button"
            onClick={() => onOpenFile(change.path)}
            className="text-muted-foreground/60 hover:text-primary flex-shrink-0 rounded p-0.5 transition-colors hover:bg-[var(--hover-overlay)]"
            title={`用${target.viewerLabel}打开: ${change.path}`}
            aria-label={`打开文件 ${change.path}`}
            data-open-viewer={target.viewerId}
            data-testid="scene-change-open"
          >
            <ExternalLink className="h-icon-sm w-icon-sm" />
          </button>
        )}
      </div>

      {expanded && (
        <div className="space-y-1.5 px-2 pb-2" data-testid="scene-change-detail">
          <div className="text-muted-foreground/70 flex flex-wrap items-center gap-2 text-[10px]">
            <span>产出工具 {change.producedBy}</span>
            <span data-testid="scene-change-channel">
              来自 {channelLabelOfTool(change.producedBy)}
            </span>
            {netStat && (
              <span>
                首末净差 <span className="text-status-success">+{netStat.added}</span>{' '}
                <span className="text-status-error">-{netStat.removed}</span>
              </span>
            )}
            {change.firstSeenAt && <span>首次 {change.firstSeenAt}</span>}
            <span data-testid="scene-change-viewer">{target.viewerLabel}</span>
          </div>

          {change.status === 'deleted' ? (
            // 删除只有路径：数据面无 old_content（G6），如实标注不造假 diff
            <div className="text-muted-foreground bg-muted/30 rounded px-2 py-1 text-[11px]">
              已删除（只有路径，没有删除前内容——工具结果不含文件正文，无法展示删除 diff）
            </div>
          ) : change.firstOldContent !== null && change.lastNewContent !== null ? (
            <DetailBlock
              block={{
                id: 'scene-change-diff',
                label: '差异对比',
                content: '',
                contentType: 'diff',
                diffOld: change.firstOldContent,
                diffNew: change.lastNewContent,
                collapsible: false,
              }}
            />
          ) : (
            <div className="text-muted-foreground bg-muted/30 rounded px-2 py-1 text-[11px]">
              工具结果未带改前/改后全文，无法展示 diff（可按下方轮次查看每次写入参数）
            </div>
          )}

          {/* L2：历史轮次（中间轮次折叠在此，逐轮动作与增删） */}
          {change.rounds.length > 0 && (
            <div className="space-y-0.5" data-testid="scene-change-rounds">
              <div className="text-muted-foreground text-[10px]">
                写入轮次（{change.rounds.length}）
              </div>
              {change.rounds.map((round, index) => (
                <div
                  key={`${round.callId}-${index}`}
                  className="text-muted-foreground/80 flex items-center gap-2 truncate text-[10px]"
                >
                  <span className="flex-shrink-0">#{index + 1}</span>
                  <span className="min-w-0 flex-1 truncate" title={round.summary}>
                    {round.summary}
                  </span>
                  {round.added !== undefined || round.removed !== undefined ? (
                    <span className="flex-shrink-0 font-mono">
                      <span className="text-status-success">+{round.added ?? 0}</span>{' '}
                      <span className="text-status-error">-{round.removed ?? 0}</span>
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
