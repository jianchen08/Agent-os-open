/**
 * 模型参数编辑器（受控组件）：添加模型表单与模型行「参数」面板共用。
 * 草稿类型与序列化见 ./modelParams（draftFromModel / buildModelFields）。
 */
import type { ModalityDraft, ModelParamsDraft, StrengthLevelDraft } from './modelParams'

const STRENGTH_LEVELS = ['high', 'medium', 'low'] as const
const MODALITIES = ['image', 'audio', 'video'] as const

const THINKING_TYPE_OPTIONS: [string, string][] = [
  ['enabled', '开启 (enabled)'],
  ['adaptive', '自适应 (adaptive)'],
  ['disabled', '关闭 (disabled)'],
]
const EFFORT_OPTIONS: [string, string][] = [
  ['low', 'low'],
  ['medium', 'medium'],
  ['high', 'high'],
  ['max', 'max'],
]
const LEVEL_LABELS: Record<'high' | 'medium' | 'low', string> = {
  high: '高',
  medium: '中',
  low: '低',
}

function ParamSelect({
  ariaLabel,
  value,
  emptyLabel,
  options,
  onChange,
}: {
  ariaLabel: string
  value: string
  emptyLabel: string
  options: [string, string][]
  onChange: (v: string) => void
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={ariaLabel}
      className="border-border bg-background h-7 w-28 rounded px-1.5 text-xs"
    >
      <option value="">{emptyLabel}</option>
      {options.map(([v, label]) => (
        <option key={v} value={v}>
          {label}
        </option>
      ))}
    </select>
  )
}

/**
 * 参数编辑面板（受控）。同一页面会同时挂载多个实例（添加表单 + 每个模型行），
 * 交互控件的 aria-label 均带实例内唯一前缀，测试请用 within(scope) 定位。
 */
export function ModelParamsEditor({
  value,
  onChange,
}: {
  value: ModelParamsDraft
  onChange: (next: ModelParamsDraft) => void
}) {
  const set = (patch: Partial<ModelParamsDraft>) => onChange({ ...value, ...patch })
  const setLevel = (level: 'high' | 'medium' | 'low', patch: Partial<StrengthLevelDraft>) =>
    set({ strength: { ...value.strength, [level]: { ...value.strength[level], ...patch } } })
  const setModality = (m: 'image' | 'audio' | 'video', patch: Partial<ModalityDraft>) =>
    set({ multimodal: { ...value.multimodal, [m]: { ...value.multimodal[m], ...patch } } })

  const addCustomParam = () => {
    const k = value.customKey.trim()
    if (!k) return
    // 同名覆盖：允许修改已加入的自定义参数
    set({
      customParams: [...value.customParams.filter((p) => p.key !== k), { key: k, value: value.customValue.trim() }],
      customKey: '',
      customValue: '',
    })
  }

  const modalityLabels = { image: '图片', audio: '音频', video: '视频' } as const

  return (
    <div className="space-y-3">
      {/* 基础参数 */}
      <div>
        <h4 className="text-muted-foreground mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
          基础
        </h4>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-muted-foreground flex flex-col gap-1 text-xs">
            上下文窗口 (tokens)
            <input
              type="number"
              min={0}
              value={value.contextWindow}
              onChange={(e) => set({ contextWindow: e.target.value })}
              placeholder="未设置"
              aria-label="上下文窗口 (tokens)"
              className="border-border bg-background h-7 w-32 rounded px-1.5 text-xs"
            />
          </label>
          <label className="text-muted-foreground flex flex-col gap-1 text-xs">
            最大输出 (max_tokens)
            <input
              type="number"
              min={1}
              value={value.maxTokens}
              onChange={(e) => set({ maxTokens: Number(e.target.value) })}
              aria-label="最大输出 (max_tokens)"
              className="border-border bg-background h-7 w-28 rounded px-1.5 text-xs"
            />
          </label>
          <label className="text-muted-foreground flex flex-col gap-1 text-xs">
            Temperature
            <input
              type="number"
              min={0}
              max={2}
              step={0.1}
              value={value.temperature}
              onChange={(e) => set({ temperature: Number(e.target.value) })}
              aria-label="Temperature"
              className="border-border bg-background h-7 w-24 rounded px-1.5 text-xs"
            />
          </label>
          <label className="text-muted-foreground flex flex-col gap-1 text-xs">
            Top P
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={value.topP}
              onChange={(e) => set({ topP: Number(e.target.value) })}
              aria-label="Top P"
              className="border-border bg-background h-7 w-24 rounded px-1.5 text-xs"
            />
          </label>
        </div>
      </div>

      {/* 推理参数（think 类模型） */}
      <div>
        <h4 className="text-muted-foreground mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
          推理 (thinking)
        </h4>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-muted-foreground flex items-center gap-1.5 pb-1.5 text-xs">
            <input
              type="checkbox"
              checked={value.reasoningModel}
              onChange={(e) => set({ reasoningModel: e.target.checked })}
              className="border-border h-3.5 w-3.5"
            />
            推理模型
          </label>
          <label className="text-muted-foreground flex flex-col gap-1 text-xs">
            思考模式
            <ParamSelect
              ariaLabel="思考模式"
              value={value.thinkingType}
              emptyLabel="保持原样"
              options={THINKING_TYPE_OPTIONS}
              onChange={(v) => set({ thinkingType: v })}
            />
          </label>
          <label className="text-muted-foreground flex flex-col gap-1 text-xs">
            推理力度
            <ParamSelect
              ariaLabel="推理力度"
              value={value.effort}
              emptyLabel="保持原样"
              options={EFFORT_OPTIONS}
              onChange={(v) => set({ effort: v })}
            />
          </label>
        </div>
      </div>

      {/* 思考强度映射：聊天页档位 → 该模型参数（留空回退内置默认表） */}
      <div>
        <h4 className="text-muted-foreground mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
          思考强度映射
        </h4>
        <p className="text-muted-foreground mb-1.5 text-[10px] leading-relaxed">
          聊天页思考强度 高/中/低 → 本模型参数；留空=该档位用内置默认（推理力度 low/medium/high）。
          DeepSeek 类模型填推理力度，GLM/MiniMax 类填思考模式。
        </p>
        <div className="space-y-1.5">
          {STRENGTH_LEVELS.map((level) => (
            <div key={level} className="flex flex-wrap items-center gap-2">
              <span className="text-muted-foreground w-6 text-xs">{LEVEL_LABELS[level]}</span>
              <ParamSelect
                ariaLabel={`思考模式（${LEVEL_LABELS[level]}）`}
                value={value.strength[level].thinkingType}
                emptyLabel="内置默认"
                options={THINKING_TYPE_OPTIONS}
                onChange={(v) => setLevel(level, { thinkingType: v })}
              />
              <ParamSelect
                ariaLabel={`推理力度（${LEVEL_LABELS[level]}）`}
                value={value.strength[level].effort}
                emptyLabel="内置默认"
                options={EFFORT_OPTIONS}
                onChange={(v) => setLevel(level, { effort: v })}
              />
            </div>
          ))}
        </div>
      </div>

      {/* 多模态：勾选后聊天输入框才显示对应上传入口 */}
      <div>
        <h4 className="text-muted-foreground mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
          多模态
        </h4>
        <div className="space-y-1.5">
          {MODALITIES.map((m) => {
            const d = value.multimodal[m]
            return (
              <div key={m} className="flex flex-wrap items-center gap-2">
                <label className="text-muted-foreground flex w-14 items-center gap-1.5 text-xs">
                  <input
                    type="checkbox"
                    checked={d.enabled}
                    onChange={(e) => setModality(m, { enabled: e.target.checked })}
                    className="border-border h-3.5 w-3.5"
                  />
                  {modalityLabels[m]}
                </label>
                {d.enabled && (
                  <>
                    <input
                      value={d.types}
                      onChange={(e) => setModality(m, { types: e.target.value })}
                      aria-label={`${modalityLabels[m]}类型`}
                      placeholder="逗号分隔 MIME"
                      className="border-border bg-background h-7 flex-1 min-w-48 rounded px-1.5 font-mono text-xs"
                    />
                    <label className="text-muted-foreground flex items-center gap-1 text-xs">
                      上限(MB)
                      <input
                        type="number"
                        min={1}
                        value={d.maxSizeMb}
                        onChange={(e) => setModality(m, { maxSizeMb: e.target.value })}
                        aria-label={`${modalityLabels[m]}上限(MB)`}
                        className="border-border bg-background h-7 w-20 rounded px-1.5 text-xs"
                      />
                    </label>
                  </>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* 自定义参数：合并进 default_params，随请求发送 */}
      <div>
        <h4 className="text-muted-foreground mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
          自定义参数
        </h4>
        <div className="flex items-center gap-2">
          <input
            value={value.customKey}
            onChange={(e) => set({ customKey: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                addCustomParam()
              }
            }}
            placeholder="参数名，如 extra_body"
            aria-label="自定义参数名"
            className="border-border bg-background h-7 w-40 rounded px-1.5 text-xs"
          />
          <span className="text-muted-foreground text-xs">=</span>
          <input
            value={value.customValue}
            onChange={(e) => set({ customValue: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                addCustomParam()
              }
            }}
            placeholder="值（数字/true/false 自动识别类型）"
            aria-label="自定义参数值"
            className="border-border bg-background h-7 flex-1 rounded px-1.5 text-xs"
          />
          <button
            type="button"
            onClick={addCustomParam}
            disabled={!value.customKey.trim()}
            className="border-border bg-background hover:bg-muted h-7 rounded border px-2 text-xs disabled:opacity-50"
          >
            加入
          </button>
        </div>
        {value.customParams.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {value.customParams.map((p) => (
              <span
                key={p.key}
                className="bg-muted flex items-center gap-1 rounded px-2 py-0.5 font-mono text-xs"
              >
                {p.key}={p.value || "''"}
                <button
                  type="button"
                  onClick={() => set({ customParams: value.customParams.filter((x) => x.key !== p.key) })}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={`移除自定义参数 ${p.key}`}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        <p className="text-muted-foreground mt-1 text-[10px]">
          保存时合并进 default_params 随请求发送；同名覆盖既有字段
        </p>
      </div>
    </div>
  )
}
