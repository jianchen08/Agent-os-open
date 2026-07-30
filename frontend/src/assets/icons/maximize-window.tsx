import React from "react";

/**
 * 最大化(进入) — 方框 + 右下角向外的双箭头
 */
const MaximizeWindowIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`maximize-window-icon ${className}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      {/* 主方框 */}
      <rect x="3.5" y="3.5" width="9" height="9" rx="1" />
      {/* 右下角向外双箭头 */}
      <path d="M11 16.5 H16.5 V11" />
      <path d="M16.5 16.5 L11.5 11.5" />
    </svg>
  );
};

export default MaximizeWindowIcon;
