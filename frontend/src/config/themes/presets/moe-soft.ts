/**
 * 奶油甜心主题
 *
 * 奶油软萌系(cream / milk-coffee / dusty-rose):
 * - 奶白底 + 玫瑰粉主色,低饱和但保对比:软萌不等于看不清
 * - 适中圆角 + 柔光弥散阴影,像牛奶咖啡与棉花糖般安静温柔
 * - 圆体字体(本地 Quicksand + 系统幼圆)+ 细实线 + 极淡波点 + Q 弹回弹动效
 */

import type { ThemeConfig } from '@/types/theme'

export const moeSoftTheme: ThemeConfig = {
  id: 'moe-soft',
  name: '奶油甜心',
  description: '奶油软萌系:奶白底+玫瑰粉主色,柔光圆角,甜而不腻、清晰可读',
  category: 'light',

  colors: {
    // 主色加深一档:玫瑰粉 / 软紫 / 奶油杏
    primary: '#d9738f',
    secondary: '#b8a1cf',
    accent: '#f5d6b8',

    background: {
      // 奶白 → 淡杏的极浅奶油渐变
      main: 'linear-gradient(160deg, #fff9f5 0%, #fdf3ec 60%, #f9efe8 100%)',
      card: 'rgba(255, 255, 255, 0.88)',
      sidebar: '#f7ede6',
      input: '#f7ede6',
      elevated: 'rgba(255, 255, 255, 0.95)',
    },

    text: {
      // 奶咖棕系文字,整体加深一档:温柔但要读得清
      primary: '#5d4a41',
      secondary: '#877068',
      muted: '#ab968e',
      disabled: '#d6ccc5',
    },

    border: {
      default: 'rgba(217, 115, 143, 0.32)',
      hover: 'rgba(217, 115, 143, 0.5)',
      active: '#d9738f',
    },

    status: {
      // 降饱和但拉开明度与色相:状态一眼可分
      success: '#7fb07f',
      warning: '#e0a45f',
      error: '#dd5468',
      info: '#8aa5d1',
      running: '#d9738f',
      pending: '#ab968e',
    },

    bubble: {
      user_bg: 'linear-gradient(150deg, #f3c4d3 0%, #e9a9bf 100%)',
      user_text: '#7e4557',
      user_radius: '1.25rem 1.25rem 1.25rem 0.375rem',
      user_shadow: '0 6px 18px rgba(220, 150, 170, 0.25)',
      user_border: '1px solid rgba(217, 115, 143, 0.45)',
      ai_bg: '#ffffff',
      ai_text: '#5d4a41',
      ai_radius: '1.25rem 1.25rem 0.375rem 1.25rem',
      ai_shadow: '0 4px 14px rgba(220, 150, 170, 0.15)',
      ai_border: '1px solid rgba(217, 115, 143, 0.28)',
    },
  },

  components: {
    // 全站边框线型:细实线(2px 圆点线在大面积上会显脏)
    borderStyle: 'solid',

    borderRadius: {
      none: '0',
      sm: '0.5rem',
      md: '0.75rem',
      lg: '1rem',
      xl: '1.5rem',
      full: '9999px',
      defaultRadius: 'lg',
    },

    fonts: {
      // 本地 Quicksand(拉丁)+ 系统圆体中文(幼圆);离线可用
      ui: "'Quicksand', 'YouYuan', '幼圆', 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif",
      code: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },

    fontSize: {
      xs: '14px',
      sm: '15px',
      md: '16px',
      lg: '18px',
      xl: '20px',
      defaultFontSize: 'md',
    },

    shadows: {
      // 大模糊 + 低透明度 = 蓬松棉花糖弥散阴影(奶油棕调)
      none: { sm: 'none', md: 'none', lg: 'none' },
      light: {
        sm: '0 2px 8px rgba(214, 170, 160, 0.14)',
        md: '0 4px 14px rgba(214, 170, 160, 0.16)',
        lg: '0 8px 22px rgba(214, 170, 160, 0.18)',
      },
      normal: {
        sm: '0 3px 12px rgba(214, 170, 160, 0.2)',
        md: '0 6px 20px rgba(214, 170, 160, 0.22)',
        lg: '0 10px 28px rgba(214, 170, 160, 0.24)',
      },
      strong: {
        sm: '0 6px 18px rgba(195, 174, 214, 0.24)',
        md: '0 12px 32px rgba(195, 174, 214, 0.26)',
        lg: '0 20px 48px rgba(214, 170, 160, 0.3)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 14px rgba(217, 115, 143, 0.3)',
      waiting: '0 0 14px rgba(184, 161, 207, 0.28)',
      success: '0 0 14px rgba(127, 176, 127, 0.28)',
      error: '0 0 14px rgba(221, 84, 104, 0.3)',
      defaultGlowIntensity: 20,
    },

    button: {
      style: 'pill',
      shadow: true,
      borderWidth: '1px',
      hoverEffect: 'lift',
      texture: 'gradient',
      textureOpacity: 0.15,
      variants: {
        primary: {
          bg: 'linear-gradient(140deg, #e590ac 0%, #d9738f 100%)',
          text: '#ffffff',
          border: 'rgba(176, 74, 102, 0.45)',
          hoverBg: 'linear-gradient(140deg, #eda2b9 0%, #e0829b 100%)',
        },
        secondary: {
          bg: 'linear-gradient(140deg, #bda8d6 0%, #a68ec6 100%)',
          text: '#ffffff',
          border: 'rgba(124, 98, 163, 0.4)',
          hoverBg: 'linear-gradient(140deg, #c9b6de 0%, #b39bd0 100%)',
        },
        ghost: {
          bg: 'transparent',
          text: '#bf4a68',
          border: 'transparent',
          hoverBg: 'rgba(217, 115, 143, 0.1)',
        },
        destructive: {
          bg: '#fbe0e4',
          text: '#b03a4c',
          border: 'rgba(221, 84, 104, 0.4)',
          hoverBg: '#f7cfd6',
        },
      },
    },

    input: {
      style: 'filled',
      focusBorder: '#d9738f',
      focusGlow: '0 0 0 4px rgba(217, 115, 143, 0.16)',
    },

    card: {
      style: 'glass',
      blur: '16px',
      border: '1px solid rgba(217, 115, 143, 0.3)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: { bg: '#f7dce5', text: '#b04a66', border: 'rgba(217, 115, 143, 0.4)' },
        secondary: { bg: '#ece5f6', text: '#7560a0', border: 'rgba(166, 142, 198, 0.45)' },
        success: { bg: '#e2efe2', text: '#4e7d52', border: 'rgba(127, 176, 127, 0.45)' },
        warning: { bg: '#f8ecd9', text: '#99682a', border: 'rgba(224, 164, 95, 0.45)' },
        error: { bg: '#fadfe2', text: '#b03a4c', border: 'rgba(221, 84, 104, 0.4)' },
        info: { bg: '#e3eaf6', text: '#5a76a6', border: 'rgba(138, 165, 209, 0.45)' },
      },
    },

    dialog: {
      borderRadius: '1.25rem',
      overlayBg: '#c9a49b',
      overlayOpacity: 0.3,
      shadow: '0 24px 48px rgba(214, 170, 160, 0.3)',
      border: '1px solid rgba(217, 115, 143, 0.35)',
    },

    tabs: {
      borderRadius: '0.875rem',
      listBg: 'rgba(217, 115, 143, 0.1)',
      activeBg: '#ffffff',
      activeText: '#bf4a68',
      inactiveText: '#ab968e',
    },

    toast: {
      borderRadius: '1rem',
      shadow: '0 12px 28px rgba(214, 170, 160, 0.26)',
      variants: {
        default: { bg: '#ffffff', text: '#5d4a41', border: '1px solid rgba(217, 115, 143, 0.3)' },
        success: { bg: '#e2efe2', text: '#4e7d52', border: '1px solid rgba(127, 176, 127, 0.4)' },
        error: { bg: '#fadfe2', text: '#b03a4c', border: '1px solid rgba(221, 84, 104, 0.35)' },
        warning: { bg: '#f8ecd9', text: '#99682a', border: '1px solid rgba(224, 164, 95, 0.4)' },
        info: { bg: '#e3eaf6', text: '#5a76a6', border: '1px solid rgba(138, 165, 209, 0.4)' },
      },
    },

    progress: {
      borderRadius: '9999px',
      trackBg: 'rgba(217, 115, 143, 0.14)',
      variants: {
        default: 'linear-gradient(90deg, #e590ac 0%, #d9738f 100%)',
        success: '#7fb07f',
        warning: '#e0a45f',
        error: '#dd5468',
      },
    },

    dropdownMenu: {
      borderRadius: '1rem',
      shadow: '0 12px 28px rgba(214, 170, 160, 0.26)',
      border: '1px solid rgba(217, 115, 143, 0.3)',
      itemHoverBg: 'rgba(217, 115, 143, 0.12)',
      itemHoverText: '#b03a4c',
    },
  },

  effects: {
    glassmorphism: true,
    animations: true,
    transitionDuration: 350,
    transitionEasing: 'cubic-bezier(0.34, 1.56, 0.64, 1)',
  },

  backgrounds: {
    main: {
      type: 'gradient',
      value: 'linear-gradient(160deg, #fff9f5 0%, #fdf3ec 60%, #f9efe8 100%)',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(255, 249, 245, 0.5)',
      overlayOpacity: 0.5,
    },
    texture: {
      // 极淡波点 = 奶油表面的小气泡
      type: 'dots',
      color: 'rgba(217, 115, 143, 0.08)',
      size: '26px',
      opacity: 0.5,
    },
    sidebar: {
      type: 'solid',
      value: '#f7ede6',
      texture: { type: 'dots', color: 'rgba(217, 115, 143, 0.07)', size: '26px', opacity: 0.45 },
    },
    chat: {
      type: 'gradient',
      value: 'linear-gradient(180deg, #fff9f5 0%, #fdf3ec 100%)',
    },
  },
}
