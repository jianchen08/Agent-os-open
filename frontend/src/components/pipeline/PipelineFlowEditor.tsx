/**
 * 0.2 多循环体管道可视化编辑器（顶层）。
 *
 * 纵向编排 loop_bodies（体间箭头标注顺序推进语义，末尾标注 run 结束），
 * 每体一张 LoopBodyCard。step id / 体 id 全量收集供路由目标下拉。
 * 不持有状态：所有编辑经 ops 落到 raw data，由页面统一保存。
 */

import { useMemo } from 'react'
import { ChevronDown } from '@/assets/icons'
import { Input } from '@/components/ui/input'
import { collectBodyIds, collectStepIds, getLoopBodies } from '@/services/pipeline/model'
import { LoopBodyCard } from './LoopBodyCard'
import type { PipelinePluginCatalogEntry } from '@/services/api/pipelines'
import type { Path, PipelineEditorOps } from '@/services/pipeline/model'

/**
 * 管道流程编辑器。
 *
 * @param data GET /api/v1/config/pipelines/autonomous 的 data（raw，唯一真相）
 * @param ops 编辑器操作集
 * @param catalog 插件目录（不可用时传 []，引用降级为 unknown 展示）
 */
export function PipelineFlowEditor({
  data,
  ops,
  catalog,
}: {
  data: Record<string, unknown>
  ops: PipelineEditorOps
  catalog: PipelinePluginCatalogEntry[]
}) {
  const bodies = useMemo(() => getLoopBodies(data), [data])
  const bodyIds = useMemo(() => collectBodyIds(data), [data])
  const stepIds = useMemo(() => collectStepIds(data), [data])
  const stepIdSet = useMemo(() => new Set(stepIds), [stepIds])
  const name = typeof data.name === 'string' ? data.name : ''

  return (
    <div data-testid="pipeline-flow-editor" aria-label="多循环体管道可视化编辑器">
      {/* 管道名（autonomous.yaml 顶层 name） */}
      <div className="mb-4 flex items-center gap-2">
        <span className="text-muted-foreground shrink-0 text-xs">管道名</span>
        <Input
          value={name}
          onChange={(e) => ops.set(['name'], e.target.value)}
          className="h-7 w-48 font-mono text-xs"
          aria-label="管道名"
          spellCheck={false}
        />
        <span className="text-muted-foreground text-[10px]">
          {bodies.length} 个循环体 · config/pipelines/autonomous.yaml
        </span>
      </div>

      <div className="space-y-0">
        {bodies.map((body, i) => {
          const bodyPath: Path = ['loop_bodies', i]
          return (
            <div key={`${body?.id ?? i}-${i}`}>
              <LoopBodyCard
                body={body}
                bodyPath={bodyPath}
                bodyIndex={i}
                ops={ops}
                catalog={catalog}
                knownStepIds={stepIds}
                knownPhaseIds={bodyIds}
                knownStepIdSet={stepIdSet}
              />
              {/* 体间转移语义标注：默认顺序推进 / 末尾 run 结束 */}
              <div className="text-muted-foreground flex flex-col items-center gap-0.5 py-1.5">
                <ChevronDown className="h-4 w-4" />
                <span className="text-[10px]">
                  {i < bodies.length - 1 ? '顺序推进（exit_routes 可覆盖）' : 'run 结束'}
                </span>
              </div>
            </div>
          )
        })}
        {bodies.length === 0 && (
          <p className="text-muted-foreground border-border rounded-xl border border-dashed py-10 text-center text-sm">
            无循环体（loop_bodies 为空）
          </p>
        )}
      </div>
    </div>
  )
}
