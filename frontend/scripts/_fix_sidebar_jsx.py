# -*- coding: utf-8 -*-
from pathlib import Path

p = Path("src/components/layout/Sidebar.tsx")
t = p.read_text(encoding="utf-8")

marker = "                  itemHeight={SIDEBAR_STYLES.itemHeight}\n                />\n              )}\n            </div>\n"
idx = t.find(marker)
if idx < 0:
    raise SystemExit("list end marker missing")
idx = idx + len(marker)

modal = t.find("<SessionEditModal")
if modal < 0:
    raise SystemExit("modal missing")

insert = """
            {/* bottom fixed: user / theme / notifications */}
            <div
              className="border-border mt-auto flex items-center gap-1 border-t px-2 py-2"
              data-testid="sidebar-footer"
            >
              <div className="text-muted-foreground flex min-w-0 flex-1 items-center gap-2 px-1">
                <User className="h-4 w-4 shrink-0" />
                <span className="truncate text-[12px]">
                  {(user as { username?: string; email?: string } | null)?.username ||
                    (user as { username?: string; email?: string } | null)?.email ||
                    '未登录'}
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
          </>
        )}
      </aside>

      """

t = t[:idx] + insert + t[modal:]
# dedupe multiple footers if any previous leftover
# keep first sidebar-footer only is hard; verify structure by counting tags roughly

# remove orphan broken lines if duplicate footers
first = t.find('data-testid="sidebar-footer"')
second = t.find('data-testid="sidebar-footer"', first + 1)
if second > 0:
    # remove second footer block roughly
    print("multiple footers, cleaning second")
    # find start of second footer div
    s = t.rfind("<div", 0, second)
    e = t.find("</div>\n            </div>", second)
    if e > 0:
        e = e + len("</div>\n            </div>")
        t = t[:s] + t[e:]

p.write_text(t, encoding="utf-8")
print("fixed", p)
