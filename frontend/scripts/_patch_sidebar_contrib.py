# -*- coding: utf-8 -*-
from pathlib import Path
import re

p = Path("src/components/layout/Sidebar.tsx")
t = p.read_text(encoding="utf-8")

# ---- imports + remove fixed SIDEBAR_MENU ----
# cut from first import block icons through SIDEBAR_MENU end
m = re.search(r"^import \{\n  Brain,[\s\S]*?const SIDEBAR_MENU:[\s\S]*?\]\n", t, re.M)
if not m:
    # maybe already partially edited
    if "getViewsContainers" in t and "SIDEBAR_MENU" not in t:
        print("already patched imports?")
    else:
        # try broader
        m = re.search(r"^import \{\n[\s\S]*?from '@/assets/icons'\n[\s\S]*?(?=interface SidebarProps)", t, re.M)
        if not m:
            raise SystemExit("cannot find import+menu block")
        start, end = m.span()
else:
    start, end = m.span()

new_head = '''import {
  Bell,
  ChatIcon,
  ChatActiveIcon,
  Loader2,
  Plus,
  Search,
  Settings,
  User,
  X,
} from '@/assets/icons'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SessionEditModal } from '@/components/session/SessionEditModal'
import { SessionList } from '@/components/session/SessionList'
import { SessionSearch } from '@/components/session/SessionSearch'
import { ThemeButton } from '@/components/layout/ThemeButton'
import { ThemePanel } from '@/components/layout/ThemePanel'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { cn } from '@/lib/utils'
import { reportError } from '@/services/errorReporting'
import {
  openWorkspacePanel,
  openWorkspacePanelByPath,
} from '@/services/workspacePanelOpener'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { ContributionEntry } from '@/services/schema/ContributionRegistry'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useAgentStore } from '@/stores/agentStore'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useAuthStore } from '@/stores/authStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import type { Session } from '@/types'

/** fixed sessions + plugin container id */
type SidebarView = 'sessions' | string

'''
t = t[:start] + new_head + t[end:]

# ---- state ----
old_state = "  const [activeView, setActiveView] = useState<SidebarView>('sessions')\n"
# may have comment line before
t = re.sub(
    r"  /\*\*[^\n]*\*/\n  const \[activeView, setActiveView\] = useState<SidebarView>\('sessions'\)\n",
    "  const [activeView, setActiveView] = useState<SidebarView>('sessions')\n",
    t,
    count=1,
)
if "const [showThemePanel" not in t:
    t = t.replace(
        old_state,
        old_state
        + '''  const [showThemePanel, setShowThemePanel] = useState(false)
  const [contribTick, setContribTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => {
      const n = contributionRegistry.getViewsContainers().length
      setContribTick((prev) => (prev === n ? prev : n))
    }, 1500)
    return () => window.clearInterval(id)
  }, [])
  const pluginContainers = useMemo(() => {
    void contribTick
    return contributionRegistry
      .getViewsContainers()
      .slice()
      .sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
  }, [contribTick])
  const user = useAuthStore((s) => s.user)
''',
    )
    print("state ok")

# ---- handler ----
if "handlePluginClick" not in t:
    # remove old handleMenuClick if present
    t = re.sub(
        r"  /\*\* 侧栏菜单点击[\s\S]*?\[isMobile, setSidebarCollapsed\],\n  \)\n",
        "",
        t,
        count=1,
    )
    t = re.sub(
        r"  const handleMenuClick = useCallback\([\s\S]*?\[isMobile, setSidebarCollapsed\],\n  \)\n",
        "",
        t,
        count=1,
    )
    insert_after = "  const handleSessionClickMobile = useCallback(\n"
    # better insert before return (
    marker = "  return (\n"
    idx = t.rfind(marker)
    # find the return of component - first return after export const Sidebar
    idx = t.find("export const Sidebar")
    idx = t.find("\n  return (\n", idx)
    if idx < 0:
        raise SystemExit("return not found")
    handler = '''
  const handlePluginClick = useCallback(
    (entry: ContributionEntry) => {
      setActiveView(entry.id)
      if (entry.path && typeof entry.path === 'string') {
        const opened = openWorkspacePanelByPath(entry.path)
        if (!opened && entry.path.startsWith('/')) {
          navigate(entry.path)
        }
      } else if (entry.widget) {
        openWorkspacePanel({
          id: `ws-plugin-${entry.id}`,
          title: entry.title || entry.id,
          component: String(entry.widget),
          icon: entry.icon,
          moduleId: entry.pluginId ? `__plugin_${entry.pluginId}__` : `__contrib_${entry.id}__`,
        })
      } else {
        openWorkspacePanel({
          id: `ws-plugin-${entry.id}`,
          title: entry.title || entry.id,
          component: entry.id,
          icon: entry.icon,
          moduleId: `__contrib_${entry.id}__`,
        })
      }
      if (isMobile) setSidebarCollapsed(true)
    },
    [isMobile, navigate, setSidebarCollapsed],
  )

  const handleSessionsClick = useCallback(() => {
    setActiveView('sessions')
  }, [])

'''
    t = t[:idx] + handler + t[idx:]
    print("handler ok")

# ---- collapsed rail: only sessions + plugin containers ----
# replace sidebar-rail block body map SIDEBAR_MENU
t = re.sub(
    r"\{SIDEBAR_MENU\.map\(\(item\) => \{[\s\S]*?\)\}\)\n",
    "",
    t,
)

# force rewrite expanded nav and collapsed rail more surgically by marker regions
# Expanded nav section
nav_start = t.find('data-testid="sidebar-nav"')
if nav_start > 0:
    # find starting <div of sidebar-nav
    div_start = t.rfind("<div", 0, nav_start)
    # find next border divider after nav (session section header)
    # replace from div_start to '会话{' section start
    sess_mark = t.find("会话{filteredSessions.length", div_start)
    if sess_mark < 0:
        sess_mark = t.find("会话", div_start)
    # find the wrapping div before session header
    # easier: from div_start to the line with 会话(
    line = t.find("\n", sess_mark)
    # go back to start of that block's parent "text-muted-foreground flex"
    block = t.rfind('<div className="text-muted-foreground flex', div_start, sess_mark + 1)
    if block < 0:
        block = t.find('<div className="text-muted-foreground', div_start)
    end_nav = block if block > 0 else div_start + 1

    new_nav = r'''
            <div className="flex min-h-0 flex-1 flex-col" data-testid="sidebar-main">
            <div className="flex flex-col gap-0.5 px-2 pt-3 pb-2" data-testid="sidebar-nav">
              <button
                type="button"
                onClick={handleOpenNewSessionModal}
                className="hover:bg-white/5 text-foreground mb-1 flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] font-medium transition-colors"
                data-testid="new-session-button"
              >
                <span
                  className="flex h-6 w-6 items-center justify-center rounded-md"
                  style={{ background: 'var(--ds-bg-elevated, #111C38)' }}
                >
                  <Plus className="h-3.5 w-3.5 text-[var(--ds-accent-primary,#22D3EE)]" />
                </span>
                新建会话
              </button>

              {/* fixed: sessions */}
              <button
                type="button"
                onClick={handleSessionsClick}
                className={cn(
                  'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors',
                  activeView === 'sessions'
                    ? 'text-foreground'
                    : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
                )}
                style={
                  activeView === 'sessions'
                    ? {
                        background: 'var(--ds-bg-elevated, #111C38)',
                        boxShadow:
                          'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.35))',
                      }
                    : undefined
                }
                data-testid="sidebar-menu-sessions"
              >
                {activeView === 'sessions' ? (
                  <ChatActiveIcon className="h-4 w-4 shrink-0" />
                ) : (
                  <ChatIcon className="h-4 w-4 shrink-0 opacity-90" />
                )}
                <span className="min-w-0 flex-1 truncate">会话</span>
              </button>

              {/* plugin contributed viewsContainers (vscode-like) */}
              {pluginContainers.map((entry) => {
                const selected = activeView === entry.id
                return (
                  <button
                    key={entry.id}
                    type="button"
                    onClick={() => handlePluginClick(entry)}
                    className={cn(
                      'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors',
                      selected
                        ? 'text-foreground'
                        : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
                    )}
                    style={
                      selected
                        ? {
                            background: 'var(--ds-bg-elevated, #111C38)',
                            boxShadow:
                              'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.35))',
                          }
                        : undefined
                    }
                    data-testid={`sidebar-menu-plugin-${entry.id}`}
                    title={entry.title || entry.id}
                  >
                    <span className="flex h-4 w-4 shrink-0 items-center justify-center text-[13px] opacity-90">
                      {entry.icon || '◆'}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{entry.title || entry.id}</span>
                  </button>
                )
              })}
            </div>

            <div className="border-border mx-2 mb-1 border-t" />

'''
    t = t[:div_start] + new_nav + t[end_nav:]
    print("expanded nav replaced")

# append bottom fixed bar before closing expanded fragment / before modal
# Find session list end then add footer before outer fragment close of expanded branch
# Insert bottom bar after session list container ends, before Theme is none.
if 'data-testid="sidebar-footer"' not in t:
    # after session list loading/empty/list block, before SessionEditModal usage in expanded branch
    # Look for final SessionList / empty and the fragment close of expanded
    m = re.search(
        r"(</div>\n\s*\)\}\n\s*</div>\n)(\s*<SessionEditModal)",
        t,
    )
    # more robust: before the single SessionEditModal that serves both states
    idx = t.find("<SessionEditModal")
    if idx > 0:
        footer = r'''
            {/* bottom fixed: user / theme / notifications */}
            <div
              className="border-border mt-auto flex items-center gap-1 border-t px-2 py-2"
              data-testid="sidebar-footer"
            >
              <div className="text-muted-foreground flex min-w-0 flex-1 items-center gap-2 px-1">
                <User className="h-4 w-4 shrink-0" />
                <span className="truncate text-[12px]">
                  {user?.username || user?.email || '未登录'}
                </span>
              </div>
              <div className="relative">
                <ThemeButton onClick={() => setShowThemePanel(true)} />
                <ThemePanel isOpen={showThemePanel} onClose={() => setShowThemePanel(false)} />
              </div>
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 flex h-8 w-8 items-center justify-center rounded-md"
                title="通知"
                aria-label="通知"
              >
                <Bell className="h-4 w-4" />
              </button>
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 flex h-8 w-8 items-center justify-center rounded-md"
                title="设置"
                aria-label="设置"
                onClick={() => openWorkspacePanelByPath('/settings')}
              >
                <Settings className="h-4 w-4" />
              </button>
            </div>
            </div>
'''
        # find a safe anchor: end of session list container just before modal in component
        # Use: after `</div>\n          </>\n        )}` of expanded branch - insert footer before `</>`
        close_frag = t.find("\n          </>\n        ) : (", 0)
        # expanded branch closes with `</>` before collapsed? Structure collapsed ? rail : expanded
        # find last `</>` of expanded before SessionEditModal
        # Search for `session list` section end
        anchor = t.find('data-testid="sidebar-search-section"')
        if anchor > 0:
            # after entire session list section ends - look for `min-h-0 flex-1 overflow`
            list_div = t.find('min-h-0 flex-1 overflow-x-hidden', anchor)
            # find matching close of that div roughly: next SessionEditModal is outside
            # Insert footer right before `          </>` that closes expanded fragment, near SessionEditModal
            pass
        # simpler: insert before SessionEditModal and ensure we close the main flex wrapper we opened
        t = t[:idx] + footer + "\n      " + t[idx:]
        print("footer inserted")

# collapsed rail rewrite
rail_start = t.find('data-testid="sidebar-rail"')
if rail_start > 0:
    div_start = t.rfind("<div", 0, rail_start)
    # end at next `) : (` for expanded
    end = t.find(") : (", rail_start)
    # include up to `</div>\n        `
    end = t.rfind("</div>", rail_start, end) + len("</div>")
    new_rail = r'''
          <div className="flex h-full flex-col items-center py-3" data-testid="sidebar-rail">
            <button
              type="button"
              onClick={handleSessionsClick}
              className={cn(
                'mb-2 flex h-10 w-10 items-center justify-center rounded-[10px] transition-colors',
                activeView === 'sessions'
                  ? 'text-[var(--ds-accent-primary,#22D3EE)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
              )}
              style={
                activeView === 'sessions'
                  ? {
                      background: 'var(--ds-bg-elevated, #111C38)',
                      boxShadow:
                        'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.45))',
                    }
                  : undefined
              }
              title="会话"
              data-testid="sidebar-rail-sessions"
            >
              {activeView === 'sessions' ? (
                <ChatActiveIcon className="h-5 w-5" />
              ) : (
                <ChatIcon className="h-5 w-5" />
              )}
            </button>
            {pluginContainers.map((entry) => {
              const selected = activeView === entry.id
              return (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => handlePluginClick(entry)}
                  className={cn(
                    'mb-2 flex h-10 w-10 items-center justify-center rounded-[10px] transition-colors',
                    selected
                      ? 'text-[var(--ds-accent-primary,#22D3EE)]'
                      : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
                  )}
                  style={
                    selected
                      ? {
                          background: 'var(--ds-bg-elevated, #111C38)',
                          boxShadow:
                            'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.45))',
                        }
                      : undefined
                  }
                  title={entry.title || entry.id}
                  data-testid={`sidebar-rail-plugin-${entry.id}`}
                >
                  <span className="text-[14px]">{entry.icon || '◆'}</span>
                </button>
              )
            })}
            <div className="mt-auto flex flex-col items-center gap-2 pb-1">
              <button
                type="button"
                onClick={handleOpenNewSessionModal}
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 flex h-9 w-9 items-center justify-center rounded-[10px]"
                title="新建会话"
              >
                <Plus className="h-4 w-4" />
              </button>
              <ThemeButton onClick={() => setShowThemePanel(true)} />
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 flex h-9 w-9 items-center justify-center rounded-[10px]"
                title="通知"
              >
                <Bell className="h-4 w-4" />
              </button>
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 flex h-9 w-9 items-center justify-center rounded-[10px]"
                title="设置"
                onClick={() => openWorkspacePanelByPath('/settings')}
              >
                <Settings className="h-4 w-4" />
              </button>
            </div>
          </div>
'''
    t = t[:div_start] + new_rail + t[end:]
    print("rail replaced")

# remove leftover SIDEBAR_MENU / handleMenuClick / TOP_NAV_PANELS / LayoutGrid
t = t.replace("TOP_NAV_PANELS", "/*TOP_NAV_PANELS*/")
if "SIDEBAR_MENU" in t:
    print("WARN SIDEBAR_MENU still present")

p.write_text(t, encoding="utf-8")
print("done", p)
