import React from "react";

const LayoutIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`layout-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path transform="matrix(1 0 0 1 2.5 2.5)" d="M1.6667 -0.75L13.3333 -0.75Q14.3343 -0.75 15.0422 -0.0422Q15.75 0.6657 15.75 1.6667L15.75 13.3333Q15.75 14.3343 15.0422 15.0422Q14.3343 15.75 13.3333 15.75L1.6667 15.75Q0.6657 15.75 -0.0422 15.0422Q-0.75 14.3343 -0.75 13.3333L-0.75 1.6667Q-0.75 -0.75 1.6667 -0.75ZM1.6667 0.75Q0.75 0.75 0.75 1.6667L0.75 13.3333Q0.75 14.25 1.6667 14.25L13.3333 14.25Q14.25 14.25 14.25 13.3333L14.25 1.6667Q14.25 0.75 13.3333 0.75L1.6667 0.75Z"/><path transform="matrix(1 0 0 1 2.5 7.5)" d="M15 -0.75L0 -0.75L0 0.75L4.25 0.75L4.25 10L5.75 10L5.75 0.75L15 0.75L15 -0.75Z" fillRule="evenodd"/>
    </svg>
  );
};

export default LayoutIcon;
