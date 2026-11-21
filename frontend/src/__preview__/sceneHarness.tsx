/**
 * 组件取证夹具（dev-only，仅本轮真界面取证；不进生产路径）
 *
 * 背景：本容器没有可执行的 Linux 内核二进制（`kernel/target/release` 只有
 * `agentos-kernel.exe`），dev 前端的会话数据取不到。这里用**真实组件 + 真实纯函数
 * + fixture 数据**在真实 Chromium 渲染：
 *   A. 实例看板 + 实例运行窗口（点击路由）
 *   B. 产物区（合并后的单条目 / 文件名 / 打开入口）
 *   C. 消息流工具卡片（打开路由）
 *   D. 最新 AI 气泡状态行（执行中 + 现场入口 + 预览）
 *
 * 数据侧全部走生产真值：`extractSceneActivities`（tool_call part → 活动）、
 * `buildSceneInstances`、`buildFileChanges` 都是生产纯函数，未在夹具里重写。
 * 「打开文件」的全局回调换成可视化记录器（真实环境是 openFile → 查看器页签；
 * 取流需内核，属环境缺失非代码缺失）。
 */

import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import ActivityCard from '@/components/chat/ActivityCard'
import { TaskSceneStatusRow } from '@/components/chat/TaskSceneStatusRow'
import { extractSceneActivities } from '@/components/workspace/taskScene/sceneActivities'
import { isScreenshotActivity } from '@/components/workspace/taskScene/sceneImageSource'
import { SceneInstanceBoard } from '@/components/workspace/taskScene/SceneInstanceBoard'
import { SceneInstanceDetail } from '@/components/workspace/taskScene/SceneInstanceDetail'
import { buildSceneInstances } from '@/components/workspace/taskScene/sceneInstances'
import { TaskSceneArtifacts } from '@/components/workspace/taskScene/TaskSceneArtifacts'
import { describeOpenTarget } from '@/services/fileOpener'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { registerGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import type { SceneInstance } from '@/components/workspace/taskScene/sceneInstances'
import type { ActivityData } from '@/types/activity'
import type { Message } from '@/types/models'

const TASK_ID = 'bca9ad25d2e4'

function message(id: string, parts: Array<Record<string, unknown>>): Message {
  return {
    id,
    role: 'assistant',
    content: '',
    timestamp: new Date(Date.now() - 3000).toISOString(),
    parts: parts.map((p) => ({ type: 'tool_call', ...p })),
  } as unknown as Message
}

/** 现场 fixture：运行中的终端 + 运行中的浏览器 + 三类产物文件（md / py / png） */
const MESSAGES: Message[] = [
  message('m1', [
    {
      callId: 'call-bash-1',
      name: 'bash',
      args: { command: 'npm run dev -- --port 5173' },
      state: 'streaming',
      result: { output: 'VITE v5.4.0  ready in 412 ms', exit_code: 0 },
      resultData: { output: 'VITE v5.4.0  ready in 412 ms', exit_code: 0 },
      durationMs: 2400,
    },
    {
      callId: 'call-browser-1',
      name: 'browser_navigate',
      args: { url: 'http://localhost:5173/' },
      state: 'streaming',
      result: {},
      resultData: {},
      durationMs: 1200,
    },
  ]),
  message('m2', [
    {
      callId: 'call-md',
      name: 'file_write',
      args: { path: 'docs/方案说明.md' },
      state: 'done',
      result: { file: 'docs/方案说明.md', added: 4, removed: 0, size: 64 },
      resultData: {
        file: 'docs/方案说明.md',
        added: 4,
        removed: 0,
        size: 64,
        old_content: '',
        new_content: '# 方案\n\n- 产物区合并\n- 未命名修复\n',
      },
      durationMs: 800,
    },
    {
      callId: 'call-py',
      name: 'file_write',
      args: { path: 'src/auth/login.py' },
      state: 'done',
      result: { file: 'src/auth/login.py', added: 2, removed: 1, size: 132 },
      resultData: {
        file: 'src/auth/login.py',
        added: 2,
        removed: 1,
        size: 132,
        old_content: 'def login():\n    pass\n',
        new_content: 'def login(user, pwd):\n    return user == "admin"\n',
      },
      durationMs: 700,
    },
    {
      callId: 'call-png',
      name: 'file_write',
      args: { path: 'assets/screenshot.png' },
      state: 'done',
      result: { file: 'assets/screenshot.png', size: 20480 },
      resultData: { file: 'assets/screenshot.png', size: 20480 },
      durationMs: 600,
    },
  ]),
]

const ACTIVITIES = extractSceneActivities(MESSAGES)

// 状态行预览条目读 pipelineMessageStore（生产同源）：模块作用域注入，避免被
// StrictMode 双调用 / persist rehydrate 覆盖
usePipelineMessageStore.setState({
  messagesByPipeline: { [TASK_ID]: MESSAGES },
  pipelines: { [TASK_ID]: { status: 'running' } },
} as never)
;(window as unknown as { __store?: unknown }).__store = usePipelineMessageStore
const INSTANCES = buildSceneInstances(ACTIVITIES, {
  pipelineRunning: true,
  isScreenshot: isScreenshotActivity,
})

/* ── 打开记录器 ── */
const openLog: string[] = []
function logHtml(): string {
  const rows =
    openLog.length === 0
      ? '<span style="color:#8a90a0">（尚未点击）</span>'
      : openLog.map((l) => `<div><code>${l}</code></div>`).join('')
  return (
    '<div style="font:12px/1.7 system-ui;padding:8px 12px;border-top:1px solid #d7dbe5;background:#fff">' +
    '<b>统一打开入口调用记录</b>（describeOpenTarget 判定结果）' +
    rows +
    '</div>'
  )
}
function push(line: string): void {
  openLog.push(line)
  const box = document.getElementById('open-log')
  if (box) box.innerHTML = logHtml()
}
registerGlobalOpenFileCallback((filePath: string, cid?: string) => {
  const t = describeOpenTarget(filePath)
  push(`${filePath} → ${t.viewerLabel} [${t.viewerId} / widget=${t.widget} / source=${t.source}]${cid ? ` · cid=${cid}` : ''}`)
})

/** A+B：实例看板（可点）+ 运行窗口 + 产物区 */
function SceneSection(): React.ReactElement {
  const [openKey, setOpenKey] = useState<string | null>(null)
  const detail: SceneInstance | undefined = openKey
    ? INSTANCES.find((i) => i.key === openKey)
    : undefined
  return (
    <div style={{ background: '#fff', border: '1px solid #d7dbe5', borderRadius: 8 }}>
      <div style={{ padding: '6px 12px', font: '11px system-ui', color: '#666' }}>
        任务现场（真实组件：SceneInstanceBoard / SceneInstanceDetail / TaskSceneArtifacts）
      </div>
      <SceneInstanceBoard
        instances={INSTANCES}
        pipelineStatus="running"
        hasPluginPage={() => true}
        onOpenInstance={(inst) => setOpenKey((prev) => (prev === inst.key ? null : inst.key))}
      />
      {detail && (
        <SceneInstanceDetail instance={detail} onClose={() => setOpenKey(null)} />
      )}
      <TaskSceneArtifacts taskId={TASK_ID} activities={ACTIVITIES} />
    </div>
  )
}

/** C：消息流工具卡片 */
function StreamCard(): React.ReactElement {
  const filePath = 'assets/screenshot.png'
  const t = describeOpenTarget(filePath)
  const activity: ActivityData = {
    id: 'card-image-read',
    type: 'tool_call',
    status: 'completed',
    title: '读取 screenshot.png',
    toolName: 'file_read',
    subtitle: filePath,
    filePath,
    durationMs: 320,
    onOpenFile: (p: string) => {
      const target = describeOpenTarget(p)
      push(`${p} → ${target.viewerLabel} [${target.viewerId} / widget=${target.widget} / source=${target.source}]`)
    },
    details: [{ id: 'd1', label: '文件', content: '', contentType: 'file', path: filePath }],
  }
  return (
    <div style={{ padding: 12 }} data-testid="harness-card">
      <div style={{ font: '12px system-ui', color: '#666', marginBottom: 6 }}>
        消息流工具卡片（file_read 图片）· 打开目标：{t.viewerLabel}
      </div>
      <ActivityCard activity={activity} />
    </div>
  )
}

function Harness(): React.ReactElement {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh', background: 'var(--background)' }}>
      <div style={{ padding: '8px 12px', font: '13px/1.4 system-ui', fontWeight: 600, background: '#fff', borderBottom: '1px solid #d7dbe5' }}>
        AgentOS 组件取证夹具（真实组件 + 生产纯函数 + fixture 数据，dev-only）
      </div>
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start', padding: 12 }}>
        <div style={{ flex: '1 1 0', minWidth: 0 }}>
          <SceneSection />
        </div>
        <div style={{ flex: '0 0 460px' }}>
          <div style={{ background: '#fff', border: '1px solid #d7dbe5', borderRadius: 8 }}>
            <StreamCard />
            <div style={{ padding: 12 }} data-testid="harness-status">
              <div style={{ font: '12px system-ui', color: '#666', marginBottom: 4 }}>
                最新 AI 气泡下方状态区（TaskSceneStatusRow）
              </div>
              <TaskSceneStatusRow
                taskId={TASK_ID}
                streaming
                streamAnchorMs={Date.now() - 7300}
                pendingInteraction={false}
              />
            </div>
          </div>
        </div>
      </div>
      <div id="open-log" dangerouslySetInnerHTML={{ __html: logHtml() }} />
    </div>
  )
}

const root = document.getElementById('root')
if (!root) throw new Error('root missing')
createRoot(root).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
)
