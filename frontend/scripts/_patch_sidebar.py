# -*- coding: utf-8 -*-
from pathlib import Path

path = Path("src/components/layout/Sidebar.tsx")
text = path.read_text(encoding="utf-8")
start = text.find("        {/* ---- 折叠状态")
end = text.find("            {/* 搜索框区域", start)
assert start > 0 and end > start, (start, end)

replacement = r'''        {/* ---- 折叠状态：图标菜单栏（48px，VS Code 感，非双侧栏） ---- */}
        {sidebarCollapsed && !isMobile ? (
          <div className="flex h-full flex-col items-center py-3" data-testid="sidebar-rail">
            {SIDEBAR_MENU.map((item) => {
              const Active = item.ActiveIcon || item.Icon
              const Icon = activeView === item.id ? Active : item.Icon
              const selected = activeView === item.id
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => handleMenuClick(item.id)}
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
                  title={item.label}
                  aria-label={item.label}
                  data-testid={`sidebar-rail-${item.id}`}
                >
                  <Icon className="h-5 w-5" />
                </button>
              )
            })}
            <div className="mt-auto flex flex-col items-center gap-2 pb-1">
              <button
                type="button"
                onClick={handleOpenNewSessionModal}
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 flex h-9 w-9 items-center justify-center rounded-[10px]"
                title="新建会话"
                aria-label="新建会话"
              >
                <Plus className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => openWorkspacePanelByPath('/monitoring')}
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 flex h-9 w-9 items-center justify-center rounded-[10px]"
                title="监控"
                aria-label="监控"
              >
                <LayoutGrid className="h-4 w-4" />
              </button>
            </div>
          </div>
        ) : (
          /* ---- 展开状态：菜单条 + 会话列表 ---- */
          <>
            <div
              className={cn(
                'border-border flex items-center justify-between',
                SIDEBAR_STYLES.headerHeight,
                SIDEBAR_STYLES.paddingX,
              )}
              data-testid="sidebar-header"
            >
              <h2 className="text-foreground text-[15px] font-semibold leading-none">
                {SIDEBAR_MENU.find((m) => m.id === activeView)?.label || '会话'}
              </h2>
              <div className="flex items-center gap-1">
                {isMobile && (
                  <Button
                    size={SIDEBAR_STYLES.buttonSize}
                    variant="ghost"
                    onClick={handleCloseSidebar}
                    aria-label="关闭侧边栏"
                    title="关闭侧边栏"
                    data-testid="close-sidebar-button"
                    className="h-7 w-7 p-0"
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                )}
                <Button
                  size={SIDEBAR_STYLES.buttonSize}
                  variant="default"
                  onClick={handleOpenNewSessionModal}
                  aria-label="新建会话"
                  title="新建会话"
                  data-testid="new-session-button"
                  className="h-7 w-7 p-0"
                >
                  <Plus className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>

            <div
              className="border-border flex items-center gap-1 overflow-x-auto border-b px-2 py-1.5"
              data-testid="sidebar-menu"
            >
              {SIDEBAR_MENU.map((item) => {
                const Active = item.ActiveIcon || item.Icon
                const Icon = activeView === item.id ? Active : item.Icon
                const selected = activeView === item.id
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => handleMenuClick(item.id)}
                    className={cn(
                      'flex h-8 w-8 shrink-0 items-center justify-center rounded-md transition-colors',
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
                    title={item.label}
                    aria-label={item.label}
                    data-testid={`sidebar-menu-${item.id}`}
                  >
                    <Icon className="h-4 w-4" />
                  </button>
                )
              })}
            </div>

'''

text2 = text[:start] + replacement + text[end:]
# drop unused imports if leftover: ChevronLeft/Right/toggleSidebar still may be unused
for old, new in [
    ("ChevronLeft, ChevronRight, Loader2, MessageSquare, Plus, Search, X",
     "Loader2, MessageSquare, Plus, Search, X"),
]:
    if old in text2:
        # already changed imports earlier; ignore
        pass

# Remove unused toggleSidebar binding noise is ok if lint unused; neutralize by prefix usage in comment no.
# If ChevronLeft/Right still imported in old form they may not be in current imports.
path.write_text(text2, encoding="utf-8")
print("OK replaced", end - start, "chars")
print("ChevronLeft", text2.count("ChevronLeft"), "toggleSidebar", text2.count("toggleSidebar"))
