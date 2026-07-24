import React from "react";

const PersonIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`person-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path transform="matrix(1 0 0 1 4.16667 12.5)" d="M10.9167 3.3333L10.9167 5Q10.9076 5.3144 11.1363 5.5303Q11.3523 5.759 11.6667 5.75Q11.9811 5.759 12.197 5.5303Q12.4257 5.3144 12.4167 5L12.4167 3.3333Q12.4167 1.642 11.2207 0.446Q10.0247 -0.75 8.3333 -0.75L3.3333 -0.75Q1.642 -0.75 0.446 0.446Q-0.75 1.642 -0.75 3.3333L-0.75 5Q-0.759 5.3144 -0.5303 5.5303Q-0.3144 5.759 0 5.75Q0.3144 5.759 0.5303 5.5303Q0.759 5.3144 0.75 5L0.75 3.3333Q0.75 2.2633 1.5067 1.5067Q2.2633 0.75 3.3333 0.75L8.3333 0.75Q9.4034 0.75 10.16 1.5067Q10.9167 2.2633 10.9167 3.3333Z" fill-rule="evenodd"/><circle fill="none" stroke="#94A3B8" stroke-width="1.5" transform="matrix(1 0 0 1 6.66667 2.5)" cx="3.3333" cy="3.3333" r="3.3333"/>
    </svg>
  );
};

export default PersonIcon;