import React from "react";

/**
 * 还原最大化 — 两个错位的方框(前小后大)
 */
const RestoreWindowIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`restore-window-icon ${className}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      {/* 后方框(大,靠右上) */}
      <path d="M7.5 4.5 H15.5 a1 1 0 0 1 1 1 V13.5" />
      <path d="M15.5 13.5 H7.5 a1 1 0 0 1 -1 -1 V5.5" />
      {/* 前方框(小,靠左下) */}
      <path d="M3.5 8.5 H11.5 a1 1 0 0 1 1 1 V16.5" />
      <path d="M3.5 16.5 a1 1 0 0 1 1 -1 H11.5" />
      <path d="M3.5 16.5 V8.5 a1 1 0 0 1 1 -1" />
    </svg>
  );
};

export default RestoreWindowIcon;
