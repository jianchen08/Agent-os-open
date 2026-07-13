/**
 * 海洋微风主题
 *
 * 清新自然的蓝绿色调，带来海洋般的宁静感受
 * 适合日间使用，带来轻松愉悦的视觉体验
 */

import type { ThemeConfig } from '@/types/theme'

export const oceanBreezeTheme: ThemeConfig = {
  id: 'ocean-breeze',
  name: '海洋微风',
  description: '清新自然的蓝绿色调，带来海洋般的宁静感受',
  category: 'light',

  colors: {
    // 海洋微风灵魂：Radix teal 青绿 + sky 天蓝，清新治愈，层次从浅到深
    primary: '#0d9b8a',
    secondary: '#00749e',
    accent: '#0eb39e',

    background: {
      main: 'linear-gradient(135deg, #f3fbf9 0%, #e1f6fd 50%, #e0f8f3 100%)',
      card: 'rgba(243, 251, 249, 0.85)',
      sidebar: 'rgba(241, 250, 253, 0.95)',
      input: '#e1f6fd',
      elevated: 'rgba(255, 255, 255, 0.9)',
    },

    text: {
      primary: '#0d3d38',
      secondary: '#0d3d38',
      muted: '#3d7585',
      disabled: '#6a8a85',
    },

    border: {
      default: 'rgba(13, 155, 154, 0.18)',
      hover: 'rgba(13, 155, 154, 0.3)',
      active: 'rgba(13, 155, 154, 0.45)',
    },

    status: {
      success: '#0d3d38',
      warning: '#8a5408',
      error: '#c0392b',
      info: '#00749e',
      running: '#0eb39e',
      pending: '#3d7585',
    },

    bubble: {
      user_bg: 'linear-gradient(135deg, #045468 0%, #023b37 50%, #084843 100%)',
      user_text: '#ffffff',
      user_radius: '1.25rem 1.25rem 1.25rem 0.25rem',
      user_shadow: '0 4px 14px rgba(13, 155, 154, 0.25)',
      ai_bg: '#e1f6fd',
      ai_text: '#0d3d38',
      ai_radius: '1.125rem 1.125rem 1.125rem 0.25rem',
      ai_shadow: '0 2px 8px rgba(0, 116, 158, 0.08)',
      ai_border: '1px solid rgba(13, 155, 154, 0.15)',
    },
  },

  components: {
    borderRadius: {
      none: '0',
      sm: '0.375rem',
      md: '0.625rem',
      lg: '0.875rem',
      xl: '1.125rem',
      full: '9999px',
      defaultRadius: 'xl',
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
      defaultShadow: 'light',
    },

    glow: {
      running: '0 0 15px rgba(34, 211, 238, 0.3)',
      waiting: '0 0 15px rgba(8, 145, 178, 0.3)',
      success: '0 0 15px rgba(5, 150, 105, 0.3)',
      error: '0 0 15px rgba(220, 38, 38, 0.3)',
      defaultGlowIntensity: 30,
    },

    button: {
      style: 'pill',
      shadow: false,
      borderWidth: '1px',
      hoverEffect: 'lift',
      texture: 'gradient',
      textureOpacity: 0.15,
      variants: {
        primary: {
          bg: 'linear-gradient(135deg, #e6fffa 0%, #ccfbf1 100%)',
          text: '#0f766e',
          border: '#5eead4',
          hoverBg: 'linear-gradient(135deg, #ccfbf1 0%, #99f6e4 100%)',
        },
        secondary: {
          bg: 'linear-gradient(135deg, #e6fffa 0%, #ccfbf1 100%)',
          text: '#0f766e',
          border: '#a7f3d0',
          hoverBg: 'linear-gradient(135deg, #ccfbf1 0%, #99f6e4 100%)',
        },
        ghost: {
          bg: 'transparent',
          text: '#0f766e',
          border: 'transparent',
          hoverBg: 'rgba(8, 145, 178, 0.1)',
        },
        destructive: {
          bg: '#fee2e2',
          text: '#991b1b',
          border: '#fca5a5',
          hoverBg: '#fecaca',
        },
      },
    },

    input: {
      style: 'outlined',
      focusBorder: '#22d3ee',
      focusGlow: '0 0 0 3px rgba(34, 211, 238, 0.2)',
    },

    card: {
      style: 'glass',
      blur: '12px',
      border: '1px solid rgba(8, 145, 178, 0.15)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: '#e6fffa',
          text: '#0f766e',
          border: '#5eead4',
        },
        secondary: {
          bg: '#e6fffa',
          text: '#0f766e',
          border: '#a7f3d0',
        },
        success: {
          bg: '#d1fae5',
          text: '#065f46',
          border: '#a7f3d0',
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
          bg: '#cffafe',
          text: '#0e7490',
          border: '#a5f3fc',
        },
      },
    },

    dialog: {
      borderRadius: '1.125rem',
      overlayBg: '#0891b2',
      overlayOpacity: 0.3,
      shadow: '0 25px 50px -12px rgba(8, 145, 178, 0.2)',
      border: '1px solid rgba(8, 145, 178, 0.2)',
    },

    tabs: {
      borderRadius: '0.875rem',
      listBg: 'rgba(8, 145, 178, 0.1)',
      activeBg: '#ffffff',
      activeText: '#0f766e',
      inactiveText: '#0d9488',
    },

    toast: {
      borderRadius: '0.875rem',
      shadow: '0 10px 15px -3px rgba(8, 145, 178, 0.15)',
      variants: {
        default: {
          bg: '#ffffff',
          text: '#0f766e',
          border: 'rgba(8, 145, 178, 0.2)',
        },
        success: {
          bg: '#d1fae5',
          text: '#065f46',
          border: '#a7f3d0',
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
          bg: '#cffafe',
          text: '#0e7490',
          border: '#a5f3fc',
        },
      },
    },

    progress: {
      borderRadius: '9999px',
      trackBg: 'rgba(8, 145, 178, 0.15)',
      variants: {
        default: 'linear-gradient(90deg, #0891b2 0%, #22d3ee 100%)',
        success: '#059669',
        warning: '#d97706',
        error: '#dc2626',
      },
    },

    dropdownMenu: {
      borderRadius: '0.875rem',
      shadow: '0 10px 15px -3px rgba(8, 145, 178, 0.15)',
      border: '1px solid rgba(8, 145, 178, 0.2)',
      itemHoverBg: 'rgba(8, 145, 178, 0.1)',
      itemHoverText: '#0f766e',
    },
  },

  effects: {
    glassmorphism: true,
    animations: true,
    transitionDuration: 300,
    transitionEasing: 'cubic-bezier(0.34, 1.56, 0.64, 1)',
  },

  backgrounds: {
    main: {
      type: 'gradient',
      value: 'linear-gradient(135deg, #f0fdff 0%, #e6fffa 30%, #ccfbf1 70%, #a7f3d0 100%)',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(240, 253, 255, 0.5)',
      overlayOpacity: 0.5,
    },
    texture: {
      type: 'lines',
      color: 'rgba(34, 211, 238, 0.08)',
      size: '60px',
      opacity: 0.4,
    },
    sidebar: {
      type: 'solid',
      value: 'rgba(230, 255, 250, 0.95)',
      texture: { type: 'none' },
    },
    chat: {
      type: 'gradient',
      value: 'linear-gradient(180deg, #f0fdff 0%, #e6fffa 100%)',
    },
  },
}
