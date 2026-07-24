import React from "react";

const ClockIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`clock-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <circle fill="none" stroke="#94A3B8" stroke-width="1.8" transform="matrix(1 0 0 1 2.5 2.5)" cx="7.5" cy="7.5" r="7.5"/><path transform="matrix(1 0 0 1 10 5.80002)" d="M0.9 3.7183L0.9 0Q0.9109 -0.3773 0.6364 -0.6364Q0.3773 -0.9109 0 -0.9Q-0.3773 -0.9109 -0.6364 -0.6364Q-0.9109 -0.3773 -0.9 0L-0.9 4.2Q-0.9016 4.4261 -0.7935 4.6247Q-0.6883 4.8248 -0.4992 4.9489L2.5008 6.9489Q2.8087 7.1672 3.1765 7.0825Q3.5486 7.0192 3.7488 6.6992Q3.9672 6.3913 3.8825 6.0235Q3.8192 5.6514 3.4992 5.4511L0.9 3.7183Z" fill-rule="evenodd"/>
    </svg>
  );
};

export default ClockIcon;