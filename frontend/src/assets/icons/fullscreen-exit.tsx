import React from "react";

/**
 * 退出全屏 — 四个角向内收缩的直角箭头
 */
const FullscreenExitIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`fullscreen-exit-icon ${className}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M7.5 3.5 V7.5 H3.5" />
      <path d="M12.5 3.5 V7.5 H16.5" />
      <path d="M12.5 16.5 V12.5 H16.5" />
      <path d="M7.5 16.5 V12.5 H3.5" />
    </svg>
  );
};

export default FullscreenExitIcon;
