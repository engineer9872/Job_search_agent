import React from 'react';

export default function Pagination({
  currentOffset,
  limit,
  totalJobsCount,
  onPrevPage,
  onNextPage,
}) {
  const currentPage = Math.floor(currentOffset / limit) + 1;
  const totalPages = Math.ceil(totalJobsCount / limit) || 1;

  const isPrevDisabled = currentOffset === 0;
  const isNextDisabled = currentOffset + limit >= totalJobsCount;

  return (
    <div className="pagination-bar">
      <button
        onClick={onPrevPage}
        disabled={isPrevDisabled}
        className="btn-secondary"
      >
        <i className="fa-solid fa-chevron-left"></i> Previous
      </button>
      <span className="page-indicator">
        Page {currentPage} of {totalPages}
      </span>
      <button
        onClick={onNextPage}
        disabled={isNextDisabled}
        className="btn-secondary"
      >
        Next <i className="fa-solid fa-chevron-right"></i>
      </button>
    </div>
  );
}
