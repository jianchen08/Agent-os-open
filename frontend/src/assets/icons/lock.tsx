import React from "react";

const LockIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`lock-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <rect fill="none" stroke="#94A3B8" strokeWidth="1.8" transform="matrix(1 0 0 1 4 9)" width="12" height="8" rx="2" ry="2"/><path transform="matrix(1 0 0 1 6.5 3)" d="M-0.9 3.5L-0.9 6L0.9 6L0.9 3.5Q0.9 2.4478 1.6739 1.6739Q2.4478 0.9 3.5 0.9Q4.5522 0.9 5.3261 1.6739Q6.1 2.4478 6.1 3.5L6.1 6L7.9 6L7.9 3.5Q7.9 1.7022 6.5989 0.4011Q5.2978 -0.9 3.5 -0.9Q1.7022 -0.9 0.4011 0.4011Q-0.9 1.7022 -0.9 3.5Z" fillRule="evenodd"/><circle transform="matrix(1 0 0 1 8.8 11.8)" cx="1.2" cy="1.2" r="1.2"/>
    </svg>
  );
};

export default LockIcon;
