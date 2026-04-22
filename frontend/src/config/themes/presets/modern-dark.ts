/**
 * 现代深色主题
 *
 * 优雅护眼的深色主题，适合夜间工作和长时间编程
 * 采用现代化设计语言，提供舒适的视觉体验
 */

import type { ThemeConfig } from '@/types/theme'

export const modernDarkTheme: ThemeConfig = {
  id: 'modern-dark',
  name: '现代深色',
  description: '优雅护眼的深色主题，适合夜间工作和长时间编程',
  category: 'dark',

  colors: {
    primary: '#3b82f6',
    secondary: '#6366f1',
    accent: '#00d4ff',

    background: {
      main: 'radial-gradient(ellipse at top, #1e293b 0%, #0f172a 50%, #020617 100%)',
      card: 'rgba(30, 41, 59, 0.8)',
      sidebar: 'rgba(15, 23, 42, 0.95)',
      input: 'rgba(51, 65, 85, 0.8)',
      elevated: 'rgba(30, 41, 59, 0.9)',
    },

    text: {
      primary: '#f8fafc',      // 保持高对比度
      secondary: '#cbd5e1',    // 从 #94a3b8 提亮，对比度 7.2:1
      muted: '#94a3b8',        // 从 #64748b 提亮，对比度 5.4:1
      disabled: '#64748b',     // 从 #475569 提亮，对比度 3.2:1
    },

    border: {
      default: 'rgba(255, 255, 255, 0.12)',  // 从 0.08 增加
      hover: 'rgba(255, 255, 255, 0.20)',    // 从 0.15 增加
      active: 'rgba(59, 130, 246, 0.5)',     // 从 0.4 增加
    },

    status: {
      success: '#10b981',
      warning: '#f59e0b',
      error: '#ef4444',
      info: '#3b82f6',
      running: '#00d4ff',
      pending: '#94a3b8',
    },

    bubble: {
      user_bg: 'linear-gradient(135deg, #3b82f6 0%, #00d4ff 100%)',
      user_text: '#ffffff',
      user_radius: '1.25rem 1.25rem 1.25rem 0.25rem',
      user_shadow: '0 4px 16px rgba(0, 212, 255, 0.3), 0 0 20px rgba(59, 130, 246, 0.15)',
      ai_bg: 'rgba(30, 41, 59, 0.8)',
      ai_text: '#f1f5f9',
      ai_radius: '1rem 1rem 1rem 0.25rem',
      ai_shadow: '0 2px 8px rgba(0, 0, 0, 0.3)',
      ai_border: '1px solid rgba(59, 130, 246, 0.15)',
    },
  },

  components: {
    borderRadius: {
      none: '0',
      sm: '0.375rem',
      md: '0.5rem',
      lg: '0.75rem',
      xl: '1rem',
      full: '9999px',
      defaultRadius: 'lg',
    },

    fonts: {
      ui: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      code: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },

    fontSize: {
      xs: '14px',
      sm: '15px',
      md: '16px',
      lg: '17px',
      xl: '18px',
      defaultFontSize: 'md',
    },

    shadows: {
      none: { sm: 'none', md: 'none', lg: 'none' },
      light: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.2)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.25)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.25)',
      },
      normal: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.3)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.4)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.4)',
      },
      strong: {
        sm: '0 1px 3px 0 rgb(0 0 0 / 0.4)',
        md: '0 4px 8px -1px rgb(0 0 0 / 0.5)',
        lg: '0 10px 20px -3px rgb(0 0 0 / 0.5)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 20px rgba(0, 212, 255, 0.4)',
      waiting: '0 0 20px rgba(245, 158, 11, 0.4)',
      success: '0 0 20px rgba(16, 185, 129, 0.4)',
      error: '0 0 20px rgba(239, 68, 68, 0.4)',
      defaultGlowIntensity: 40,
    },

    button: {
      style: 'rounded',
      shadow: true,
      borderWidth: '1px',
      hoverEffect: 'glow',
      texture: 'glass',
      textureOpacity: 0.2,
      variants: {
        primary: {
          bg: 'linear-gradient(135deg, #3b82f6 0%, #00d4ff 100%)',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: 'linear-gradient(135deg, #2563eb 0%, #0891b2 100%)',
        },
        secondary: {
          bg: 'rgba(59, 130, 246, 0.1)',
          text: '#f8fafc',
          border: 'rgba(59, 130, 246, 0.3)',
          hoverBg: 'rgba(59, 130, 246, 0.2)',
        },
        ghost: {
          bg: 'transparent',
          text: '#94a3b8',
          border: 'transparent',
          hoverBg: 'rgba(255, 255, 255, 0.05)',
        },
        destructive: {
          bg: '#ef4444',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: '#dc2626',
        },
      },
    },

    input: {
      style: 'filled',
      focusBorder: '#3b82f6',
      focusGlow: '0 0 0 3px rgba(59, 130, 246, 0.2)',
    },

    card: {
      style: 'glass',
      blur: '12px',
      border: '1px solid rgba(255, 255, 255, 0.08)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: 'linear-gradient(135deg, #3b82f6 0%, #00d4ff 100%)',
          text: '#ffffff',
          border: 'transparent',
        },
        secondary: {
          bg: 'rgba(59, 130, 246, 0.2)',
          text: '#e0f2fe',
          border: 'rgba(59, 130, 246, 0.4)',
        },
        success: {
          bg: 'rgba(16, 185, 129, 0.25)',
          text: '#6ee7b7',
          border: 'rgba(16, 185, 129, 0.5)',
        },
        warning: {
          bg: 'rgba(245, 158, 11, 0.25)',
          text: '#fcd34d',
          border: 'rgba(245, 158, 11, 0.5)',
        },
        error: {
          bg: 'rgba(239, 68, 68, 0.25)',
          text: '#fca5a5',
          border: 'rgba(239, 68, 68, 0.5)',
        },
        info: {
          bg: 'rgba(0, 212, 255, 0.25)',
          text: '#67e8f9',
          border: 'rgba(0, 212, 255, 0.5)',
        },
      },
    },

    dialog: {
      borderRadius: '1rem',
      overlayBg: '#000000',
      overlayOpacity: 0.75,
      shadow: '0 25px 50px -12px rgba(0, 0, 0, 0.5)',
      border: '1px solid rgba(59, 130, 246, 0.2)',
    },

    tabs: {
      borderRadius: '0.5rem',
      listBg: 'rgba(59, 130, 246, 0.1)',
      activeBg: 'rgba(59, 130, 246, 0.2)',
      activeText: '#67e8f9',
      inactiveText: '#94a3b8',
    },

    toast: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.4)',
      variants: {
        default: {
          bg: 'rgba(30, 41, 59, 0.95)',
          text: '#f8fafc',
          border: 'rgba(59, 130, 246, 0.2)',
        },
        success: {
          bg: 'rgba(16, 185, 129, 0.2)',
          text: '#6ee7b7',
          border: 'rgba(16, 185, 129, 0.5)',
        },
        error: {
          bg: 'rgba(239, 68, 68, 0.2)',
          text: '#fca5a5',
          border: 'rgba(239, 68, 68, 0.5)',
        },
        warning: {
          bg: 'rgba(245, 158, 11, 0.2)',
          text: '#fcd34d',
          border: 'rgba(245, 158, 11, 0.5)',
        },
        info: {
          bg: 'rgba(0, 212, 255, 0.2)',
          text: '#67e8f9',
          border: 'rgba(0, 212, 255, 0.5)',
        },
      },
    },

    progress: {
      borderRadius: '9999px',
      trackBg: 'rgba(59, 130, 246, 0.2)',
      variants: {
        default: 'linear-gradient(90deg, #3b82f6 0%, #00d4ff 100%)',
        success: '#10b981',
        warning: '#f59e0b',
        error: '#ef4444',
      },
    },

    dropdownMenu: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.4)',
      border: '1px solid rgba(59, 130, 246, 0.2)',
      itemHoverBg: 'rgba(59, 130, 246, 0.15)',
      itemHoverText: '#67e8f9',
    },
  },

  effects: {
    glassmorphism: true,
    animations: true,
    transitionDuration: 300,
    transitionEasing: 'cubic-bezier(0.25, 0.46, 0.45, 0.94)',
  },

  backgrounds: {
    main: {
      type: 'gradient',
      value: 'radial-gradient(ellipse at top, #1e293b 0%, #0f172a 50%, #020617 100%)',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(15, 23, 42, 0.85)',
      overlayOpacity: 0.85,
    },
    texture: {
      type: 'grid',
      color: 'rgba(0, 212, 255, 0.05)',
      size: '40px',
      opacity: 0.3,
    },
    sidebar: {
      type: 'solid',
      value: '#1e293b',
      texture: { type: 'none' },
    },
    chat: {
      type: 'gradient',
      value: 'linear-gradient(180deg, #0f172a 0%, #1e293b 100%)',
    },
  },
}
