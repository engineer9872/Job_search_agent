import React from 'react';

export default function StatsGrid({ stats }) {
  const { totalJobs, remoteJobs, remotePercentage, platformsCount, dedupRatio } = stats;

  return (
    <section className="stats-grid">
      <div className="stat-card">
        <div className="stat-icon-wrapper" style={{ color: '#00f2fe' }}>
          <i className="fa-solid fa-briefcase"></i>
        </div>
        <div>
          <div className="stat-label">Total Listings</div>
          <div className="stat-value">{totalJobs}</div>
        </div>
      </div>

      <div className="stat-card">
        <div className="stat-icon-wrapper" style={{ color: '#10b981' }}>
          <i className="fa-solid fa-house-laptop"></i>
        </div>
        <div>
          <div className="stat-label">Remote Jobs</div>
          <div className="stat-value">{remoteJobs} ({remotePercentage}%)</div>
        </div>
      </div>

      <div className="stat-card">
        <div className="stat-icon-wrapper" style={{ color: '#a855f7' }}>
          <i className="fa-solid fa-network-wired"></i>
        </div>
        <div>
          <div className="stat-label">Connected Platforms</div>
          <div className="stat-value">{platformsCount} Platforms</div>
        </div>
      </div>

    </section>
  );
}
