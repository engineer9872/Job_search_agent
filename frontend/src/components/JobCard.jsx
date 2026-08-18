import React from 'react';

function formatSalary(min, max, currency) {
  if (!min && !max) return null;
  const curr = currency || '$';
  if (min && max) return `${curr}${min.toLocaleString()} - ${curr}${max.toLocaleString()}`;
  if (min) return `From ${curr}${min.toLocaleString()}`;
  return `Up to ${curr}${max.toLocaleString()}`;
}

function formatPostedDate(dateStr) {
  if (!dateStr) return 'Posting date unknown';
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return 'Posting date unknown';
  const now = new Date();
  const diffMs = now - d;
  if (diffMs < 0) return 'Posting date unknown'; // future-dated, treat as unreliable

  const diffMinutes = Math.floor(diffMs / (1000 * 60));
  const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  if (diffMinutes < 1) return 'Posted just now';
  if (diffMinutes < 60) return `Posted ${diffMinutes} minute${diffMinutes === 1 ? '' : 's'} ago`;
  if (diffHours < 24) return `Posted ${diffHours} hour${diffHours === 1 ? '' : 's'} ago`;
  if (diffDays === 1) return 'Posted 1 day ago';
  if (diffDays < 30) return `Posted ${diffDays} days ago`;
  return `Posted on ${d.toLocaleDateString()}`;
}

export default function JobCard({ job }) {
  const platform = (job.source_platform || 'adzuna').toLowerCase();
  const platformClass = `platform-${platform}`;
  const salaryStr = formatSalary(job.salary_min, job.salary_max, job.currency);
  // NEVER SHOW A DISCOVERY TIME AS IF IT WERE A POSTING TIME.
  //
  // This card previously rendered "First seen 3 hours ago" for jobs with no
  // published posting date. That reads as "posted 3 hours ago" -- but it only
  // means our scraper found it 3 hours ago. The listing itself could be a
  // month old, which is exactly what it looked like from the outside: a fresh
  // timestamp on a stale job.
  //
  // When the source published no date we now say so plainly, and show the
  // discovery time separately and clearly labelled, so the two can never be
  // mistaken for each other.
  const hasPublishedDate = Boolean(job.posted_date);
  const discoveredAt = job.scraped_at || job.fetched_at;
  const postedStr = hasPublishedDate
    ? formatPostedDate(job.posted_date)
    : 'Posting date not published';
  const discoveredStr = !hasPublishedDate && discoveredAt
    ? formatPostedDate(discoveredAt).replace(/^Posted /, 'we found it ')
    : null;
  const locationStr = job.city
    ? job.country ? `${job.city}, ${job.country}` : job.city
    : job.country || 'Location Unspecified';

  return (
    <div className="job-card">
      <div>
        <div className="card-header">
          <h3 className="job-title">{job.title}</h3>
          <span className={`platform-badge ${platformClass}`}>{platform}</span>
        </div>
        <div className="company-name">
          <i className="fa-regular fa-building"></i> {job.company}
        </div>
        <div className="job-meta-row">
          <span className="meta-pill">
            <i className="fa-solid fa-location-dot"></i> {locationStr}
          </span>
          {job.remote_flag && (
            <span className="meta-pill pill-remote">
              <i className="fa-solid fa-wifi"></i> Remote
            </span>
          )}
          {salaryStr && (
            <span className="meta-pill pill-salary">
              <i className="fa-solid fa-money-bill-wave"></i> {salaryStr}
            </span>
          )}
          {job.job_type && (
            <span className="meta-pill">
              <i className="fa-solid fa-clock"></i> {job.job_type}
            </span>
          )}
        </div>
        <p className="job-snippet">
          {job.description_snippet || 'No description preview available for this listing.'}
        </p>
        {(job.recruiter_name || job.recruiter_email || job.company_contact_email) && (
          <div style={{ marginTop: '10px', fontSize: '0.78rem', color: '#a855f7', display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {job.recruiter_name && <span><i className="fa-solid fa-user-tie"></i> {job.recruiter_name}</span>}
            {job.recruiter_email && <span><i className="fa-solid fa-envelope"></i> {job.recruiter_email}</span>}
            {job.company_contact_email && !job.recruiter_email && <span><i className="fa-solid fa-envelope"></i> {job.company_contact_email}</span>}
          </div>
        )}
      </div>
      <div className="card-footer">
        <span className="posted-date">
          <i className="fa-regular fa-calendar"></i> {postedStr}
          {discoveredStr && (
            <span className="found-at" title="When our scraper first saw this listing. This is not the posting date.">
              {' \u00b7 '}{discoveredStr}
            </span>
          )}
        </span>
        <a
          href={job.apply_url}
          target="_blank"
          rel="noopener noreferrer"
          className="btn-apply"
        >
          Apply Now <i className="fa-solid fa-arrow-up-right-from-square"></i>
        </a>
      </div>
    </div>
  );
}
