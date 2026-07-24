import React from "react";

const AlertIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`alert-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path fill="#FABF24" transform="matrix(1 0 0 1 2.5 3)" d="M15.8694 12.7672Q15.8389 12.6528 15.7796 12.5503L8.2796 -0.4497Q8.1004 -0.782 7.7332 -0.8693Q7.3716 -0.9775 7.0503 -0.7796Q6.9474 -0.7204 6.8636 -0.6364Q6.7796 -0.5526 6.7204 -0.4497L-0.7796 12.5503Q-0.9775 12.8716 -0.8693 13.2332Q-0.782 13.6004 -0.4497 13.7796Q-0.3472 13.8389 -0.2328 13.8694Q-0.1184 13.9001 0 13.9L15 13.9Q15.3773 13.9109 15.6364 13.6364Q15.9109 13.3773 15.9 13Q15.9001 12.8816 15.8694 12.7672ZM13.4417 12.1L7.5 1.801L1.5583 12.1L13.4417 12.1Z" fill-rule="evenodd"/><path fill="#FABF24" transform="matrix(1 0 0 1 10 8)" d="M0.9 3.5L0.9 0Q0.9109 -0.3773 0.6364 -0.6364Q0.3773 -0.9109 0 -0.9Q-0.3773 -0.9109 -0.6364 -0.6364Q-0.9109 -0.3773 -0.9 0L-0.9 3.5Q-0.9109 3.8773 -0.6364 4.1364Q-0.3773 4.4109 0 4.4Q0.3773 4.4109 0.6364 4.1364Q0.9109 3.8773 0.9 3.5Z" fill-rule="evenodd"/><circle fill="#FABF24" transform="matrix(1 0 0 1 9 12.8)" cx="1" cy="1" r="1"/>
    </svg>
  );
};

export default AlertIcon;