/**
 * 切户清账单点（用户隔离执法点②，前端）。
 *
 * 契约：docs/working/用户隔离机制_审计与修复_20261018.md §5——认证态翻转
 * （跨户登录 / 登出 / 认证过期）经 transitionCacheUser 统一 purge 全部持久化
 * 缓存面；同户重入（装机版每次启动自动登录）不 purge，缓存秒开语义保持。
 *
 * 缺陷回归（1018 实证）：admin/maintenance 同机交替登录，旧账户 persist 缓存
 * 先渲染（闪现）再被服务端对账清掉（全空）。存储命名空间（执法点①
 * userScopedStorage）保证 B 读不到 A 的键；本模块保证 A/B 键内的陈旧数据被
 * 及时回收 + 各 store 内存态同步重置（命名空间隔离不了同页内存）。
 *
 * 新增持久化面接入：在本文件注册表登记一行（store 面）或加入 USER_DIM_KEYS /
 * LEGACY_PERSIST_NAMES（键面），禁在业务代码里散写清理逻辑。
 */

import { del as idbDel, createStore } from 'idb-keyval'
import {
  useChatInputStore,
  CHAT_INPUT_ACCOUNT_RESET,
} from '@/stores/chatInputStore'
import {
  useLayoutModeStore,
  LAYOUT_MODE_ACCOUNT_RESET,
} from '@/stores/layoutModeStore'
import {
  useLongTermTaskStore,
  LONG_TERM_TASK_ACCOUNT_RESET,
} from '@/stores/longTermTaskStore'
import {
  usePersonaPossessStore,
  PERSONA_POSSESS_ACCOUNT_RESET,
} from '@/stores/personaPossessStore'
import {
  usePipelineMessageStore,
  PIPELINE_MESSAGE_ACCOUNT_RESET,
} from '@/stores/pipelineMessageStore'
import {
  useWorkspaceStore,
  WORKSPACE_ACCOUNT_RESET,
} from '@/stores/workspaceStore'
import {
  currentCacheUserId,
  setCacheUserId,
  userScopedKey,
} from '@/utils/userScopedStorage'
import { STORAGE_KEYS } from '../../constants/storage'
import { queryClient } from '../query/queryClient'
import { queryPersisterStorage } from '../query/queryPersister'

/** zustand persist 账户数据面注册表：clearStorage 清当前身份存储键，setState
 * 重置内存（partial merge 只落数据域，actions 保留）。主题/插件健康（设备级）
 * 豁免不登记——命名空间已隔离，无泄漏面。 */
const PERSIST_STORE_REGISTRY: Array<{ clearStorage: () => void; reset: () => void }> = [
  {
    clearStorage: () => usePipelineMessageStore.persist.clearStorage(),
    reset: () => usePipelineMessageStore.setState(PIPELINE_MESSAGE_ACCOUNT_RESET),
  },
  {
    clearStorage: () => useChatInputStore.persist.clearStorage(),
    reset: () => useChatInputStore.setState(CHAT_INPUT_ACCOUNT_RESET),
  },
  {
    clearStorage: () => useLayoutModeStore.persist.clearStorage(),
    reset: () => useLayoutModeStore.setState(LAYOUT_MODE_ACCOUNT_RESET),
  },
  {
    clearStorage: () => useLongTermTaskStore.persist.clearStorage(),
    reset: () => useLongTermTaskStore.setState(LONG_TERM_TASK_ACCOUNT_RESET),
  },
  {
    clearStorage: () => usePersonaPossessStore.persist.clearStorage(),
    reset: () => usePersonaPossessStore.setState(PERSONA_POSSESS_ACCOUNT_RESET),
  },
  {
    clearStorage: () => useWorkspaceStore.persist.clearStorage(),
    reset: () => useWorkspaceStore.setState(WORKSPACE_ACCOUNT_RESET),
  },
]

/** 用户维 localStorage 键（uiStorage 账户命名空间消费的键域） */
const USER_DIM_KEYS: string[] = [
  STORAGE_KEYS.LAST_ACTIVE_SESSION,
  STORAGE_KEYS.SESSION_WATCHLIST,
  STORAGE_KEYS.SESSION_CUSTOM_GROUPS,
]

/** agentTabStore 每会话键前缀（与 stores/agentTabStore.ts STORAGE_KEY_PREFIX 同源；
 * 按会话 id 寻址跨户不可达，仅作孤儿垃圾清扫） */
const AGENT_TABS_PREFIX = 'agent-tabs-'

/** IndexedDB 面持久化名（queryPersister 与 indexedDbStorage 共用 app-db/kv 库） */
const IDB_PERSIST_NAMES: string[] = ['pipeline-messages', 'tanstack-query-cache']
/** localStorage 面持久化裸键（各 persist store name 升级前形态） */
const LEGACY_LOCAL_PERSIST_NAMES: string[] = [
  'chat-input-drafts',
  'layout-mode',
  'long-term-task-storage',
  'persona.possessed',
  'agentos-plugin-health',
  'theme-storage',
  'workspace-store',
]

const idbStore = createStore('app-db', 'kv')

/** legacy IndexedDB 裸键回收（fire-and-forget：删除失败留待下次清扫，不阻断；
 * 无 IndexedDB 环境——测试 webview/隐私极端态——无事可清直接跳过） */
function removeIdbLegacy(key: string): void {
  try {
    if (typeof globalThis.indexedDB === 'undefined') return
    void idbDel(key, idbStore).catch(() => {
      /* 删除失败不阻断清账主流程 */
    })
  } catch {
    /* 同上 */
  }
}

function removeLocal(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    /* 清理失败不影响主流程 */
  }
}

/** purge 全部持久化缓存面。必须在身份翻转前调用（旧身份在场时 clearStorage
 * 才命中旧账户的键）。IndexedDB 面为 fire-and-forget：removeItem 内部已取消
 * 挂起的节流写回，重置内存后的新写只会落新身份键。 */
function purgeAll(): void {
  // ① zustand persist 存储 + 内存（内存重置放 flip 后统一做，见 transitionCacheUser）
  for (const entry of PERSIST_STORE_REGISTRY) {
    entry.clearStorage()
  }
  // ② query 持久缓存（IndexedDB scoped 键 + legacy 裸键）+ 内存查询缓存
  queryPersisterStorage.removeItem(userScopedKey('tanstack-query-cache'))
  queryPersisterStorage.removeItem('tanstack-query-cache')
  queryClient.removeQueries()
  // ③ 用户维 localStorage 键（scoped 形态 + legacy 裸键形态）
  for (const key of USER_DIM_KEYS) {
    removeLocal(userScopedKey(key))
    removeLocal(key)
  }
  // ④ legacy 裸键清扫（升级一次性；IndexedDB 面与 localStorage 面分域回收）
  for (const name of IDB_PERSIST_NAMES) {
    removeIdbLegacy(name)
  }
  for (const name of LEGACY_LOCAL_PERSIST_NAMES) {
    removeLocal(name)
  }
  // ⑤ agent-tabs-* 会话页签孤儿
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.startsWith(AGENT_TABS_PREFIX)) doomed.push(k)
    }
    for (const k of doomed) removeLocal(k)
  } catch {
    /* localStorage 不可用时无孤儿可清 */
  }
}

/** 重置注册表 store 的内存数据域（identity flip 后调用，新写落新身份键） */
function resetMemoryFragments(): void {
  for (const entry of PERSIST_STORE_REGISTRY) {
    entry.reset()
  }
}

/**
 * 认证态翻转统一入口（authStore login/register/logout/认证过期四处调用）。
 *
 * - `userId: string`（登录/注册成功）：previous 在场且 ≠ 新身份 → purge（旧身份
 *   在场清旧键）→ flip → 重置内存。previous 为 null（匿名首登）或同户重入 →
 *   不 purge（无前账户可泄 / 缓存秒开保持）。
 * - `userId: null`（登出/认证过期）：purge → 落匿名。
 */
export function transitionCacheUser(userId: string | null): void {
  const previous = currentCacheUserId()
  if (userId === null) {
    purgeAll()
    setCacheUserId(null)
    resetMemoryFragments()
    return
  }
  // 仅真实翻转才 purge+重置内存：同户重入（自动登录/刷新恢复）不动在用水合数据
  const flipped = previous !== null && previous !== userId
  if (flipped) {
    purgeAll()
  }
  setCacheUserId(userId)
  if (flipped) {
    resetMemoryFragments()
  }
}
