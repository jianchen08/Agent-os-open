/**
 * 现代浅色主题
 *
 * 简洁清新的浅色主题，适合日间办公使用
 * 提供明亮舒适的视觉体验，适合长时间工作
 */

import type { ThemeConfig } from '@/types/theme'

export const modernLightTheme: ThemeConfig = {
  id: 'modern-light',
  name: '现代浅色',
  description: '简洁清新的浅色主题，适合日间办公使用',
  category: 'light',

  colors: {
    primary: '#2563eb',
    secondary: '#4f46e5',
    accent: '#3b82f6',

    background: {
      main: 'linear-gradient(135deg, #ffffff 0%, #f8fafc 100%)',
      card: 'rgba(255, 255, 255, 0.8)',
      sidebar: 'rgba(248, 250, 252, 0.95)',
      input: '#f1f5f9',
      elevated: 'rgba(255, 255, 255, 0.9)',
    },

    text: {
      primary: '#0f172a',
      secondary: '#64748b',
      muted: '#94a3b8',
      disabled: '#cbd5e1',
    },

    border: {
      default: 'rgba(0, 0, 0, 0.08)',
      hover: 'rgba(0, 0, 0, 0.12)',
      active: 'rgba(37, 99, 235, 0.3)',
    },

    status: {
      success: '#10b981',
      warning: '#f59e0b',
      error: '#ef4444',
      info: '#3b82f6',
      running: '#06b6d4',
      pending: '#64748b',
    },

    bubble: {
      user_bg: 'linear-gradient(135deg, #2563eb 0%, #3b82f6 100%)',
      user_text: '#ffffff',
      user_radius: '1.25rem 1.25rem 1.25rem 0.25rem',
      user_shadow: '0 4px 14px rgba(37, 99, 235, 0.2)',
      ai_bg: '#e2e8f0',
      ai_text: '#0f172a',
      ai_radius: '1rem 1rem 1rem 0.25rem',
      ai_shadow: '0 2px 8px rgba(0, 0, 0, 0.06)',
      ai_border: '1px solid rgba(0, 0, 0, 0.05)',
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
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.07)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.07)',
      },
      normal: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.1)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.12)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.12)',
      },
      strong: {
        sm: '0 1px 3px 0 rgb(0 0 0 / 0.15)',
        md: '0 4px 8px -1px rgb(0 0 0 / 0.18)',
        lg: '0 10px 20px -3px rgb(0 0 0 / 0.18)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 15px rgba(6, 182, 212, 0.25)',
      waiting: '0 0 15px rgba(245, 158, 11, 0.25)',
      success: '0 0 15px rgba(16, 185, 129, 0.25)',
      error: '0 0 15px rgba(239, 68, 68, 0.25)',
      defaultGlowIntensity: 25,
    },

    button: {
      style: 'rounded',
      shadow: false,
      borderWidth: '1px',
      hoverEffect: 'lift',
      texture: 'gradient',
      textureOpacity: 0.1,
      variants: {
        primary: {
          bg: 'linear-gradient(135deg, #2563eb 0%, #3b82f6 100%)',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: 'linear-gradient(135deg, #1d4ed8 0%, #2563eb 100%)',
        },
        secondary: {
          bg: 'linear-gradient(135deg, #f1f5f9 0%, #e2e8f0 100%)',
          text: '#0f172a',
          border: '#e2e8f0',
          hoverBg: 'linear-gradient(135deg, #e2e8f0 0%, #cbd5e1 100%)',
        },
        ghost: {
          bg: 'transparent',
          text: '#64748b',
          border: 'transparent',
          hoverBg: 'rgba(0, 0, 0, 0.05)',
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
      style: 'outlined',
      focusBorder: '#2563eb',
      focusGlow: '0 0 0 3px rgba(37, 99, 235, 0.15)',
    },

    card: {
      style: 'glass',
      blur: '12px',
      border: '1px solid rgba(0, 0, 0, 0.06)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: 'linear-gradient(135deg, #2563eb 0%, #3b82f6 100%)',
          text: '#ffffff',
          border: 'transparent',
        },
        secondary: {
          bg: '#f1f5f9',
          text: '#0f172a',
          border: 'transparent',
        },
        success: {
          bg: '#dcfce7',
          text: '#166534',
          border: '#bbf7d0',
        },
        warning: {
          bg: '#fef3c7',
          text: '#92400e',
          border: '#fde68a',
        },
        error: {
          bg: '#fee2e2',
          text: '#991b1b',
          border: '#fecaca',
        },
        info: {
          bg: '#dbeafe',
          text: '#1e40af',
          border: '#bfdbfe',
        },
      },
    },

    dialog: {
      borderRadius: '1rem',
      overlayBg: '#000000',
      overlayOpacity: 0.4,
      shadow: '0 25px 50px -12px rgba(0, 0, 0, 0.15)',
      border: '1px solid rgba(0, 0, 0, 0.08)',
    },

    tabs: {
      borderRadius: '0.5rem',
      listBg: '#f1f5f9',
      activeBg: '#ffffff',
      activeText: '#0f172a',
      inactiveText: '#64748b',
    },

    toast: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
      variants: {
        default: {
          bg: '#ffffff',
          text: '#0f172a',
          border: 'rgba(0, 0, 0, 0.08)',
        },
        success: {
          bg: '#dcfce7',
          text: '#166534',
          border: '#bbf7d0',
        },
        error: {
          bg: '#fee2e2',
          text: '#991b1b',
          border: '#fecaca',
        },
        warning: {
          bg: '#fef3c7',
          text: '#92400e',
          border: '#fde68a',
        },
        info: {
          bg: '#dbeafe',
          text: '#1e40af',
          border: '#bfdbfe',
        },
      },
    },

    progress: {
      borderRadius: '9999px',
      trackBg: 'rgba(37, 99, 235, 0.15)',
      variants: {
        default: 'linear-gradient(90deg, #2563eb 0%, #3b82f6 100%)',
        success: '#10b981',
        warning: '#f59e0b',
        error: '#ef4444',
      },
    },

    dropdownMenu: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
      border: '1px solid rgba(0, 0, 0, 0.08)',
      itemHoverBg: '#f1f5f9',
      itemHoverText: '#0f172a',
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
      value: 'linear-gradient(135deg, #ffffff 0%, #f8fafc 50%, #f1f5f9 100%)',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(255, 255, 255, 0.5)',
      overlayOpacity: 0.5,
    },
    texture: {
      type: 'dots',
      color: 'rgba(59, 130, 246, 0.03)',
      size: '32px',
      opacity: 0.5,
    },
    sidebar: {
      type: 'solid',
      value: '#f8fafc',
      texture: { type: 'none' },
    },
    chat: {
      type: 'gradient',
      value: 'linear-gradient(180deg, #ffffff 0%, #f8fafc 100%)',
    },
  },
}
