import React from "react";

const UnplugIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 24 24"
      className={`unplug-icon ${className}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="m19 5 3-3" />
      <path d="m2 22 3-3" />
      <path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z" />
      <path d="M7.5 13.5 10 11" />
      <path d="M10.5 5.5 13 3" />
      <path d="m18 3 3 3" />
    </svg>
  );
};

export default UnplugIcon;
