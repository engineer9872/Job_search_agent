import React, { useState, useEffect } from 'react';

// Static metadata only — never fabricated performance/status data.
// Real status (ACTIVE/DEACTIVATED, job counts, operating layer, last error)
// comes exclusively from /api/status at render time.
const PORTAL_METADATA = {
  linkedin: { name: 'LinkedIn Jobs', category: 'Official Enterprise & API Partners' },
  indeed: { name: 'Indeed', category: 'Official Enterprise & API Partners' },
  glassdoor: { name: 'Glassdoor', category: 'Official Enterprise & API Partners' },
  dice: { name: 'Dice', category: 'Remote Job Boards & Aggregators' },
  ziprecruiter: { name: 'ZipRecruiter', category: 'Remote Job Boards & Aggregators' },
  usajobs: { name: 'USAJOBS', category: 'Remote Job Boards & Aggregators' },
  careerbuilder: { name: 'CareerBuilder', category: 'Remote Job Boards & Aggregators' },
  simplyhired: { name: 'SimplyHired', category: 'Remote Job Boards & Aggregators' },
  weworkremotely: { name: 'We Work Remotely', category: 'Remote Job Boards & Aggregators' },
  hired: { name: 'Hired', category: 'Remote Staffing Vendors' },
};

export default function PlatformsView({ onViewJobs, activeFilters = {} }) {
  const [lastFetchResult, setLastFetchResult] = useState({});
  const [platformsData, setPlatformsData] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);
  const [triggeringPortal, setTriggeringPortal] = useState(null);
  const [filterQuery, setFilterQuery] = useState('');
  const [lastUpdated, setLastUpdated] = useState(null);

  const fetchStatus = async () => {
    setIsLoading(true);
    setFetchError(null);
    try {
      const res = await fetch('/api/status');
      if (!res.ok) {
        throw new Error(`API returned ${res.status}`);
      }
      const data = await res.json();
      const apiList = data.portals_status || [];
      setPlatformsData(apiList);
      setLastUpdated(new Date());
    } catch (err) {
      console.warn('Failed to fetch platform status:', err);
      setFetchError(err.message);
      setPlatformsData([]);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchStatus();
  }, []);

  const [toast, setToast] = useState(null);

  const handleRunPortal = async (portalId) => {
    setTriggeringPortal(portalId);
    setToast({
      type: 'info',
      message: `Live-fetching ${portalId} with your current search filters\u2026 this runs synchronously and can take up to ~60s.`,
    });
    try {
      // PART 4.3: this used to POST to /api/pipeline/five-tier-run, which is a
      // fire-and-forget BackgroundTask returning "ACCEPTED" immediately. The UI
      // then guessed an 8-second delay and refreshed -- so the badge usually
      // showed a stale status and the button looked broken.
      //
      // The dedicated endpoint below runs SYNCHRONOUSLY, is scoped to this one
      // portal, uses whatever filters are active in the search bar right now,
      // and writes its own RunLog so the badge reflects THIS attempt.
      const params = new URLSearchParams();
      if (activeFilters.activeSearch) params.append('q', activeFilters.activeSearch);
      if (activeFilters.jobTitle && activeFilters.jobTitle.length > 0) {
        params.append('title', activeFilters.jobTitle.join(','));
      }
      if (activeFilters.country) params.append('country', activeFilters.country);
      params.append('remote_only', activeFilters.isRemoteOnly ? 'true' : 'false');
      if (activeFilters.datePosted) params.append('date_posted', activeFilters.datePosted);
      if (activeFilters.jobType && activeFilters.jobType !== 'all') {
        params.append('job_type', activeFilters.jobType);
      }

      const res = await fetch(
        `/api/portals/${portalId}/live-fetch?${params.toString()}`,
        { method: 'POST' }
      );
      const data = await res.json();

      if (!res.ok) {
        setToast({ type: 'error', message: data.detail || `HTTP ${res.status}` });
        return;
      }

      setLastFetchResult((prev) => ({ ...prev, [portalId]: data }));

      setToast({
        type: data.status === 'SUCCESS' ? 'success' : data.status === 'ERROR' ? 'error' : 'info',
        message: data.message,
      });

      // Refresh immediately -- the fetch has genuinely completed by now, so
      // there is nothing left to wait for.
      await fetchStatus();
      setTimeout(() => setToast(null), 9000);
    } catch (err) {
      setToast({ type: 'error', message: `Error running live fetch: ${err.message}` });
    } finally {
      setTriggeringPortal(null);
    }
  };

  const filteredPlatforms = platformsData.filter((p) => {
    const meta = PORTAL_METADATA[p.portal_id] || {};
    const name = meta.name || p.portal_name || p.portal_id;
    const category = meta.category || p.type || '';
    const q = filterQuery.toLowerCase();
    return name.toLowerCase().includes(q) || category.toLowerCase().includes(q);
  });

  const renderStatusBadge = (p) => {
    const runtimeStatus = p.runtime_status || 'DEACTIVATED';
    const isActive = runtimeStatus === 'ACTIVE';
    return (
      <span
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '5px',
          padding: '3px 9px',
          borderRadius: '6px',
          fontSize: '0.72rem',
          fontWeight: 700,
          background: isActive ? '#dcfce7' : '#f1f5f9',
          color: isActive ? '#15803d' : '#64748b',
        }}
      >
        <span
          style={{
            width: '7px',
            height: '7px',
            borderRadius: '50%',
            background: isActive ? '#22c55e' : '#94a3b8',
            display: 'inline-block',
          }}
        />
        {runtimeStatus}
      </span>
    );
  };

  // Surfaces the concrete outcome of the most recent manual fetch, so a
  // zero-result run is visibly different from a failed one.
  const renderLastFetch = (portalId) => {
    const r = lastFetchResult[portalId];
    if (!r) return null;
    const tone =
      r.status === 'SUCCESS' ? 'var(--purity-emerald)'
      : r.status === 'ERROR' ? 'var(--accent-rose)'
      : 'var(--text-dim)';
    return (
      <div className="portal-fetch-result" style={{ color: tone }}>
        <i className="fa-solid fa-circle-info" />{' '}
        {r.status === 'SUCCESS'
          ? `${r.raw_jobs_found} found \u00b7 ${r.new_jobs_inserted} new`
          : r.status === 'ERROR'
          ? `Failed: ${r.error}`
          : `0 results for "${r.keyword}"`}
      </div>
    );
  };

  return (
    <div style={{ marginTop: '10px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px', flexWrap: 'wrap', gap: '16px' }}>
        <div>
          <h2 style={{ fontSize: '1.4rem', color: '#0f172a', fontWeight: 800 }}>
            <i className="fa-solid fa-layer-group" style={{ color: '#059669', marginRight: '10px' }}></i>
            Registered Job Platforms (10)
          </h2>
          <div style={{ fontSize: '0.85rem', color: '#64748b', marginTop: '4px' }}>
            Status reflects the most recent actual fetch attempt for each platform.
            {lastUpdated && ` Last refreshed: ${lastUpdated.toLocaleTimeString()}.`}
          </div>
        </div>

        <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
          <input
            type="text"
            placeholder="Filter platforms..."
            className="select-custom"
            style={{ width: '200px' }}
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
          />
          <button className="btn-primary" onClick={fetchStatus} style={{ height: '42px' }}>
            <i className="fa-solid fa-rotate"></i> Refresh Status
          </button>
        </div>
      </div>

      {toast && (
        <div
          style={{
            marginBottom: '16px',
            padding: '10px 16px',
            borderRadius: '8px',
            fontSize: '0.85rem',
            fontWeight: 600,
            background: toast.type === 'error' ? '#fef2f2' : toast.type === 'success' ? '#dcfce7' : '#eff6ff',
            color: toast.type === 'error' ? '#b91c1c' : toast.type === 'success' ? '#15803d' : '#1d4ed8',
            border: `1px solid ${toast.type === 'error' ? '#fecaca' : toast.type === 'success' ? '#bbf7d0' : '#bfdbfe'}`,
          }}
        >
          <i className={`fa-solid ${toast.type === 'error' ? 'fa-triangle-exclamation' : toast.type === 'success' ? 'fa-circle-check' : 'fa-spinner fa-spin'}`} style={{ marginRight: '8px' }}></i>
          {toast.message}
        </div>
      )}

      {isLoading ? (
        <div style={{ textAlign: 'center', padding: '50px', background: '#fff', borderRadius: '16px', border: '1px solid #e2e8f0' }}>
          <div className="spinner"></div>
          <div style={{ color: '#64748b', fontWeight: 600 }}>Fetching real platform status...</div>
        </div>
      ) : fetchError ? (
        <div style={{ textAlign: 'center', padding: '50px', background: '#fef2f2', borderRadius: '16px', border: '1px solid #fecaca' }}>
          <div style={{ color: '#b91c1c', fontWeight: 600 }}>
            <i className="fa-solid fa-triangle-exclamation"></i> Could not load platform status: {fetchError}
          </div>
          <div style={{ color: '#64748b', fontSize: '0.85rem', marginTop: '6px' }}>
            No fallback/cached status is shown — this reflects a real backend connectivity issue.
          </div>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(310px, 1fr))', gap: '16px' }}>
          {filteredPlatforms.map((p, idx) => {
            const meta = PORTAL_METADATA[p.portal_id] || {};
            return (
              <div key={p.portal_id} className="job-card portal-card job-card-enter" style={{ padding: '18px', background: '#ffffff', border: '1px solid #e2e8f0', animationDelay: `${Math.min(idx, 10) * 35}ms` }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '10px' }}>
                  <h3 style={{ fontSize: '1.05rem', fontWeight: 800, color: '#0f172a' }}>
                    {meta.name || p.portal_name || p.portal_id}
                  </h3>
                  {renderStatusBadge(p)}
                </div>

                {renderLastFetch(p.portal_id)}

                <div style={{ fontSize: '0.78rem', color: '#64748b', marginBottom: '14px', display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                  <span className="meta-pill" style={{ background: '#f1f5f9', color: '#334155', fontWeight: 600 }}>
                    {meta.category || p.type || 'Uncategorized'}
                  </span>
                  {p.operating_layer && (
                    <span className="meta-pill" style={{ background: '#eff6ff', color: '#1d4ed8', fontWeight: 600 }}>
                      {p.operating_layer}
                    </span>
                  )}
                </div>

                {p.last_error && (
                  <div style={{ fontSize: '0.73rem', color: '#b91c1c', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: '6px', padding: '5px 8px', marginBottom: '10px' }}>
                    <i className="fa-solid fa-circle-exclamation" style={{ marginRight: '5px' }}></i>
                    {p.last_error}
                  </div>
                )}

                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', paddingTop: '12px', borderTop: '1px solid #e2e8f0' }}>
                  <div>
                    <div style={{ fontSize: '0.7rem', color: '#64748b', fontWeight: 600 }}>
                      JOBS IN DATABASE
                    </div>
                    <div style={{ fontSize: '1.25rem', fontWeight: 800, color: '#059669' }}>
                      {p.total_jobs_in_db ?? 0}
                      {typeof p.visible_jobs_30d === 'number' && (
                        <span style={{ fontSize: '0.72rem', fontWeight: 500, opacity: 0.65, marginLeft: '6px' }}>
                          ({p.visible_jobs_30d} visible in Job Search)
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: '0.68rem', color: '#94a3b8', marginTop: '2px' }}>
                      {p.last_run
                        ? `Last fetch: ${new Date(p.last_run).toLocaleString()}`
                        : 'Never run'}
                    </div>
                  </div>

                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      className="btn-secondary"
                      style={{ height: '36px', padding: '0 12px', fontSize: '0.78rem', fontWeight: 700 }}
                      disabled={triggeringPortal === p.portal_id}
                      onClick={() => handleRunPortal(p.portal_id)}
                    >
                      {triggeringPortal === p.portal_id ? 'Fetching...' : 'Run Live Fetch'}
                    </button>
                    <button
                      className="btn-primary"
                      style={{ height: '36px', padding: '0 12px', fontSize: '0.78rem', fontWeight: 700 }}
                      onClick={() => onViewJobs && onViewJobs(p.portal_id)}
                    >
                      View Jobs <i className="fa-solid fa-arrow-right" style={{ marginLeft: '4px' }}></i>
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
