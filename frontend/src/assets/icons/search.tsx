import React from "react";

const SearchIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`search-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <circle fill="none" stroke="#94A3B8" strokeWidth="1.5" transform="matrix(1 0 0 1 3.33329 3.33334)" cx="5.8333" cy="5.8333" r="5.8333"/><path transform="matrix(1 0 0 1 13.9167 13.9167)" d="M-0.5303 0.5303L3.053 4.1137Q3.2689 4.3424 3.5833 4.3333Q3.8977 4.3424 4.1137 4.1137Q4.3424 3.8977 4.3333 3.5833Q4.3424 3.2689 4.1137 3.053L0.5303 -0.5303Q0.3144 -0.759 0 -0.75Q-0.3144 -0.759 -0.5303 -0.5303Q-0.759 -0.3144 -0.75 0Q-0.759 0.3144 -0.5303 0.5303Z" fillRule="evenodd"/>
    </svg>
  );
};

export default SearchIcon;
