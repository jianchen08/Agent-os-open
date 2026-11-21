/**
 * 已注册模型清单（逐行条目）
 *
 * 自 LlmSettingsPage 抽出：页面只负责状态与数据流，条目渲染独立成件。
 * 每个已注册模型一行——显示名 · 提供商/模型名 · 「参数」展开 · 删除，
 * 注册一个模型即多一行。参数编辑走共享 ModelParamsEditor 草稿，
 * 保存经 buildModelFields 序列化为部分字段 PUT /llm/models/{id}。
 */
import { useState } from 'react'
import { ChevronDown } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { buildModelFields, draftFromModel } from './modelParams'
import { ModelParamsEditor } from './ModelParamsEditor'
import type { ModelConfig } from '@/services/api/config'

/**
 * 模型条目：展示 + 删除 + 模型设置编辑。
 *
 * 参数编辑（上下文窗口、输出上限、采样、thinking、思考强度映射、多模态、
 * 自定义参数）走共享 ModelParamsEditor 草稿，保存经 buildModelFields
 * 序列化为部分字段 PUT /llm/models/{id}。
 */
function ModelRow({
  modelId,
  model,
  strengthLevels,
  deleting,
  settingsSaving,
  onDelete,
  onSaveSettings,
}: {
  modelId: string
  model: ModelConfig
  /** 思考强度档位（llm_service 预置声明下发） */
  strengthLevels: readonly string[]
  /** 删除模型进行中 */
  deleting: boolean
  /** 保存模型设置进行中 */
  settingsSaving: boolean
  onDelete: (id: string) => void
  onSaveSettings: (id: string, settings: Partial<ModelConfig>) => Promise<void>
}) {
  const [expanded, setExpanded] = useState(false)
  const [draft, setDraft] = useState(() => draftFromModel(model, strengthLevels))

  const handleSave = () => {
    onSaveSettings(modelId, buildModelFields(draft, model))
  }

  return (
    <div className="bg-card rounded-lg border px-3 py-2">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{model.display_name || modelId}</div>
          <div className="text-muted-foreground text-xs">
            {model.provider} / {model.model_name}
          </div>
        </div>
        <Button variant="outline" size="xs" onClick={() => setExpanded((v) => !v)}>
          <ChevronDown
            className={`mr-1 h-3 w-3 transition-transform ${expanded ? 'rotate-180' : ''}`}
          />
          参数
        </Button>
        <Button
          variant="destructive"
          size="xs"
          onClick={() => onDelete(modelId)}
          disabled={deleting}
        >
          {deleting ? '删除中...' : '删除'}
        </Button>
      </div>
      {expanded && (
        <div className="mt-2 space-y-3 border-t pt-2">
          <ModelParamsEditor value={draft} onChange={setDraft} levels={strengthLevels} />
          <div className="flex items-center gap-2">
            <Button size="xs" onClick={handleSave} disabled={settingsSaving}>
              {settingsSaving ? '保存中...' : '保存设置'}
            </Button>
            <p className="text-muted-foreground text-[10px]">
              「保持原样」不改动对应字段；未自定义的高级参数维持 yaml 原值
            </p>
          </div>
        </div>
      )}
    </div>
  )
}

/** 已注册模型清单：标题 + 计数 + 逐行条目（空态给出下一步动作提示） */
export function ModelRegistryList({
  models,
  strengthLevels,
  isDeleting,
  isSavingSettings,
  onDelete,
  onSaveSettings,
}: {
  models: Record<string, ModelConfig>
  strengthLevels: readonly string[]
  isDeleting: (id: string) => boolean
  isSavingSettings: (id: string) => boolean
  onDelete: (id: string) => void
  onSaveSettings: (id: string, settings: Partial<ModelConfig>) => Promise<void>
}) {
  const modelIds = Object.keys(models)
  return (
    <div className="space-y-2">
      {modelIds.length === 0 ? (
        <div className="text-muted-foreground py-4 text-center text-sm">
          暂无模型——在上方选择提供商后拉取或手动填写模型名添加
        </div>
      ) : (
        modelIds.map((id) => (
          <ModelRow
            key={id}
            modelId={id}
            model={models[id]}
            strengthLevels={strengthLevels}
            deleting={isDeleting(id)}
            settingsSaving={isSavingSettings(id)}
            onDelete={onDelete}
            onSaveSettings={onSaveSettings}
          />
        ))
      )}
    </div>
  )
}
