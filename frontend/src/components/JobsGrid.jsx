import React from 'react';
import JobCard from './JobCard';

/**
 * Skeleton loader shown while a search is in flight. This matters more now
 * than it used to: a search can trigger a live scrape (see the SearchCache
 * system) and take a few seconds, so a static "Loading..." string would read
 * as a hang. The skeleton cards mirror the real card layout so the grid does
 * not reflow when results land.
 */
function SkeletonGrid({ count = 6 }) {
  return (
    <div className="jobs-grid">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="skeleton-card" style={{ animationDelay: `${i * 70}ms` }}>
          <div className="skeleton-line short" />
          <div className="skeleton-line medium" />
          <div className="skeleton-line" />
          <div className="skeleton-line" />
          <div className="skeleton-line short" />
        </div>
      ))}
    </div>
  );
}

/**
 * Empty state. `emptyReason` comes from the API and distinguishes:
 *   - no_candidates_in_db        : nothing matches this combination YET
 *                                  (often just means a scrape was queued)
 *   - all_candidates_filtered_out: jobs matched the broad search but every
 *                                  one violated a filter the user set --
 *                                  with the top rejection reasons named.
 * That distinction is exactly what was missing during earlier debugging,
 * where a generic "No matching jobs found" was indistinguishable from a bug.
 */
const REASON_LABELS = {
  title_mismatch: "job title didn't match your keywords",
  query_mismatch: "search text not found in the listing",
  stale_date: 'posted outside your date window',
  country_mismatch: 'posted in a different country',
  job_type_mismatch: 'different employment type',
  job_type_mismatch_remote_when_onsite_requested: 'remote roles, but you asked for onsite',
  not_remote: 'not remote roles',
  platform_mismatch: 'from a different platform',
  invalid_or_indirect_url: 'no direct apply link available',
};

function EmptyState({ emptyReason, liveFetchTriggered }) {
  const code = emptyReason?.code;
  const reasons = emptyReason?.top_rejection_reasons || {};
  const reasonEntries = Object.entries(reasons);

  const isPending = code === 'no_candidates_in_db' && liveFetchTriggered;

  return (
    <div className="jobs-grid">
      <div className="state-container fade-in-up">
        <div className={`state-icon ${isPending ? 'state-icon-pulse' : ''}`}>
          <i className={`fa-solid ${isPending ? 'fa-satellite-dish' : code === 'all_candidates_filtered_out' ? 'fa-filter-circle-xmark' : 'fa-folder-open'}`} />
        </div>

        <h3>
          {isPending
            ? 'Fetching fresh listings…'
            : code === 'all_candidates_filtered_out'
            ? 'Your filters excluded every match'
            : 'No jobs match this combination yet'}
        </h3>

        <p className="state-message">
          {emptyReason?.message ||
            'Try broadening your search criteria or widening the date window.'}
        </p>

        {reasonEntries.length > 0 && (
          <ul className="state-reasons">
            {reasonEntries.map(([reason, count]) => (
              <li key={reason}>
                <span className="reason-count">{count}</span>
                {REASON_LABELS[reason] || reason.replace(/_/g, ' ')}
              </li>
            ))}
          </ul>
        )}

        {isPending && (
          <p className="state-hint">
            A live scrape is running in the background. Hit Search again in a few seconds.
          </p>
        )}
      </div>
    </div>
  );
}

export default function JobsGrid({ jobs, isLoading, emptyReason, liveFetchTriggered }) {
  if (isLoading) return <SkeletonGrid />;

  if (!jobs || jobs.length === 0) {
    return <EmptyState emptyReason={emptyReason} liveFetchTriggered={liveFetchTriggered} />;
  }

  return (
    <div className="jobs-grid">
      {jobs.map((job, idx) => (
        <div
          key={job.id || `job_${idx}`}
          className="job-card-enter"
          // Staggered entrance, capped at 12 steps so a 50-card page never
          // takes longer than ~0.4s to finish animating in.
          style={{ animationDelay: `${Math.min(idx, 12) * 30}ms` }}
        >
          <JobCard job={job} />
        </div>
      ))}
    </div>
  );
}
