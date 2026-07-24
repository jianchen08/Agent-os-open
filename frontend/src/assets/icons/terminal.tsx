import React from "react";

const TerminalIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`terminal-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path transform="matrix(1 0 0 1 3.33331 4.16667)" d="M3.9393 5L-0.5303 9.4697Q-0.759 9.6856 -0.75 10Q-0.759 10.3144 -0.5303 10.5303Q-0.3144 10.759 0 10.75Q0.3144 10.759 0.5303 10.5303L5.5303 5.5303Q5.759 5.3144 5.75 5Q5.759 4.6856 5.5303 4.4697L0.5303 -0.5303Q0.3144 -0.759 0 -0.75Q-0.3144 -0.759 -0.5303 -0.5303Q-0.759 -0.3144 -0.75 0Q-0.759 0.3144 -0.5303 0.5303L3.9393 5Z" fill-rule="evenodd"/><path transform="matrix(1 0 0 1 10 15.8333)" d="M6.6667 -0.75L0 -0.75Q-0.3144 -0.759 -0.5303 -0.5303Q-0.759 -0.3144 -0.75 0Q-0.759 0.3144 -0.5303 0.5303Q-0.3144 0.759 0 0.75L6.6667 0.75Q6.9811 0.759 7.197 0.5303Q7.4257 0.3144 7.4167 0Q7.4257 -0.3144 7.197 -0.5303Q6.9811 -0.759 6.6667 -0.75Z" fill-rule="evenodd"/>
    </svg>
  );
};

export default TerminalIcon;