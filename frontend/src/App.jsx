import React, { useState, useCallback } from 'react';
import Sidebar from './components/Sidebar';
import Navbar from './components/Navbar';
import StatsGrid from './components/StatsGrid';
import ControlsPanel from './components/ControlsPanel';
import JobsGrid from './components/JobsGrid';
import Pagination from './components/Pagination';
import PortalHealthModal from './components/PortalHealthModal';
import PlatformsView from './components/PlatformsView';
import AnalyticsView from './components/AnalyticsView';

export default function App() {
  const [stats, setStats] = useState({
    totalJobs: 0, remoteJobs: 0, remotePercentage: 0, platformsCount: 4, dedupRatio: '100% Clean',
  });

  const [activeTab, setActiveTab] = useState('search');
  const [jobs, setJobs] = useState([]);
  const [totalJobsCount, setTotalJobsCount] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  // Diagnostic payload from the API so the empty state can explain WHY zero
  // results came back instead of showing a generic "no jobs found".
  const [emptyReason, setEmptyReason] = useState(null);
  const [liveFetchTriggered, setLiveFetchTriggered] = useState(false);
  const [cacheInfo, setCacheInfo] = useState(null);
  // PART 1: a search can return BEFORE its scrape finishes. These track the
  // still-running scrape so the UI can poll and refresh itself instead of
  // treating that early response as final (which is why fetched jobs
  // appeared to never show up).
  const [scrapeStatus, setScrapeStatus] = useState('not_triggered');
  const pollTimerRef = React.useRef(null);
  const pollAttemptsRef = React.useRef(0);
  const [isAggregating, setIsAggregating] = useState(false);
  const [isHealthOpen, setIsHealthOpen] = useState(false);

  const [searchInput, setSearchInput] = useState('');
  const [activeSearch, setActiveSearch] = useState('');
  const [jobTitle, setJobTitle] = useState([]);
  const [platform, setPlatform] = useState('all');
  const [country, setCountry] = useState('');
  const [isRemoteOnly, setIsRemoteOnly] = useState(false);
  const [datePosted, setDatePosted] = useState('past_7d');
  const [jobType, setJobType] = useState('all');
  const [currentOffset, setCurrentOffset] = useState(0);
  const limit = 50;

  const loadStats = useCallback(async () => {
    try {
      const response = await fetch('/api/stats');
      if (response.ok) {
        const data = await response.json();
        setStats({
          totalJobs: data.total_jobs || 0,
          remoteJobs: `${data.remote_jobs || 0}`,
          remotePercentage: data.remote_percentage || 0,
          platformsCount: data.platforms ? Object.keys(data.platforms).length : 10,
          dedupRatio: data.dedup_ratio || '0% Filtered',
        });
      }
    } catch (err) {
      console.warn('API Stats endpoint unavailable:', err);
    }
  }, []);

  const stopScrapePolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    pollAttemptsRef.current = 0;
  }, []);

  // Polls /api/scrape-status until the background scrape reports complete,
  // then silently re-runs the same search so the new rows appear. Capped so a
  // stuck scrape cannot poll forever.
  const startScrapePolling = useCallback((pollKey, searchArgs) => {
    stopScrapePolling();
    const MAX_ATTEMPTS = 40;      // 40 x 3s = 2 minutes
    const INTERVAL_MS = 3000;

    const tick = async () => {
      pollAttemptsRef.current += 1;
      if (pollAttemptsRef.current > MAX_ATTEMPTS) {
        setScrapeStatus('timed_out');
        stopScrapePolling();
        return;
      }
      try {
        const res = await fetch(`/api/scrape-status/${pollKey}`);
        if (res.ok) {
          const st = await res.json();
          if (st.should_refresh) {
            setScrapeStatus(st.status === 'complete' ? 'complete' : st.status);
            stopScrapePolling();
            // Re-run the SAME search to pick up the newly inserted rows.
            fetchJobsRef.current(searchArgs, { silent: true });
            return;
          }
          setScrapeStatus('in_progress');
        }
      } catch (err) {
        console.warn('Scrape status poll failed:', err);
      }
      pollTimerRef.current = setTimeout(tick, INTERVAL_MS);
    };

    pollTimerRef.current = setTimeout(tick, INTERVAL_MS);
  }, [stopScrapePolling]);

  // Ref indirection so the poller can call the latest fetchJobs without
  // being recreated (and restarting the timer) on every render.
  const fetchJobsRef = React.useRef(null);

  React.useEffect(() => stopScrapePolling, [stopScrapePolling]);

  // Fetch jobs -- called ONLY from an explicit user action (Search button,
  // pagination, or "View Jobs" from the Platforms tab). Never auto-runs on
  // mount or on a filter dropdown change alone.
  const fetchJobs = useCallback(
    async (overrides = {}, opts = {}) => {
      // A silent refresh (triggered by the scrape poller) must not flash the
      // skeleton loader -- the user is already looking at real results.
      if (!opts.silent) setIsLoading(true);
      const effective = {
        activeSearch, jobTitle, platform, country, isRemoteOnly, datePosted, jobType, currentOffset,
        ...overrides,
      };
      try {
        const params = new URLSearchParams();
        if (effective.activeSearch) params.append('q', effective.activeSearch);
        if (effective.jobTitle && effective.jobTitle.length > 0) params.append('title', effective.jobTitle.join(','));
        if (effective.platform && effective.platform !== 'all') params.append('platform', effective.platform);
        if (effective.country) params.append('country', effective.country);
        params.append('remote_only', effective.isRemoteOnly ? 'true' : 'false');
        if (effective.datePosted && effective.datePosted !== 'all') params.append('date_posted', effective.datePosted);
        if (effective.jobType && effective.jobType !== 'all') params.append('job_type', effective.jobType);
        params.append('limit', limit.toString());
        params.append('offset', (effective.currentOffset ?? 0).toString());

        const response = await fetch(`/api/jobs?${params.toString()}`);
        if (response.ok) {
          const data = await response.json();
          setJobs(data.jobs || []);
          setTotalJobsCount(data.total || 0);
          setEmptyReason(data.empty_reason || null);
          setLiveFetchTriggered(Boolean(data.live_fetch_triggered));
          setCacheInfo(data.cache || null);
          setScrapeStatus(data.scrape_status || 'not_triggered');
          setIsLoading(false);

          // The scrape is still running -- start polling so late-arriving
          // jobs land in the UI on their own.
          if (data.scrape_status === 'in_progress' && data.scrape_poll_key) {
            startScrapePolling(data.scrape_poll_key, effective);
          }
          return;
        }
      } catch (err) {
        console.warn('API Jobs endpoint error:', err);
      }
      if (opts.silent) { setIsLoading(false); return; }
      setJobs([]);
      setTotalJobsCount(0);
      setEmptyReason({
        code: 'request_failed',
        message: 'Could not reach the search API. Check that the backend is running.',
        top_rejection_reasons: {},
      });
      setIsLoading(false);
    },
    [activeSearch, jobTitle, platform, country, isRemoteOnly, datePosted, jobType, currentOffset]
  );

  React.useEffect(() => { fetchJobsRef.current = fetchJobs; }, [fetchJobs]);

  // Load dashboard stats once on mount -- this is read-only reporting, not
  // scraping, so it's fine to show immediately.
  React.useEffect(() => {
    loadStats();
  }, [loadStats]);

  const handleSearch = () => {
    const trimmed = searchInput.trim();
    setActiveSearch(trimmed);
    setCurrentOffset(0);
    setHasSearched(true);
    fetchJobs({ activeSearch: trimmed, currentOffset: 0 });
  };

  const handleReset = () => {
    setSearchInput('');
    setActiveSearch('');
    setJobTitle([]);
    setPlatform('all');
    setCountry('');
    setIsRemoteOnly(false);
    setDatePosted('past_7d');
    setJobType('all');
    setCurrentOffset(0);
    setHasSearched(false);
    stopScrapePolling();
    setScrapeStatus('not_triggered');
    setJobs([]);
    setTotalJobsCount(0);
    setEmptyReason(null);
    setLiveFetchTriggered(false);
    setCacheInfo(null);
  };

  const handleRunPipeline = async () => {
    setIsAggregating(true);
    try {
      // AUDIT FIX: '/api/pipeline/run' was never implemented on the backend --
      // this button has been silently 404ing. '/api/pipeline/five-tier-run' is
      // the endpoint that actually exists and does what the button implies.
      const response = await fetch('/api/pipeline/five-tier-run', { method: 'POST' });
      if (response.ok) {
        const res = await response.json();
        alert(`Pipeline Triggered!\n${res.message}`);
        setTimeout(() => { loadStats(); if (hasSearched) fetchJobs(); }, 3000);
      }
    } catch (err) {
      alert('Failed to trigger pipeline: ' + err.message);
    } finally {
      setIsAggregating(false);
    }
  };

  const handlePrevPage = () => {
    if (currentOffset >= limit) {
      const newOffset = currentOffset - limit;
      setCurrentOffset(newOffset);
      fetchJobs({ currentOffset: newOffset });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  const handleNextPage = () => {
    if (currentOffset + limit < totalJobsCount) {
      const newOffset = currentOffset + limit;
      setCurrentOffset(newOffset);
      fetchJobs({ currentOffset: newOffset });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  const startItem = totalJobsCount > 0 ? currentOffset + 1 : 0;
  const endItem = Math.min(currentOffset + limit, totalJobsCount);

  return (
    <div className="app-shell">
      <Sidebar activeTab={activeTab} setActiveTab={setActiveTab} />

      <div className="main-content">
        <Navbar onRunPipeline={handleRunPipeline} isAggregating={isAggregating} onOpenHealth={() => setIsHealthOpen(true)} />
        <PortalHealthModal isOpen={isHealthOpen} onClose={() => setIsHealthOpen(false)} />
        <StatsGrid stats={stats} />

        {/* key={activeTab} restarts the fade animation on every tab change,
            so switching tabs cross-fades instead of hard-swapping. */}
        <div className="tab-panel" key={activeTab}>
        {activeTab === 'platforms' ? (
          <PlatformsView
            // PART 4.3: the per-portal live fetch must use whatever filters
            // are active in the search bar right now, not a hardcoded default.
            activeFilters={{ activeSearch, jobTitle, country, isRemoteOnly, datePosted, jobType }}
            onViewJobs={(portalId) => {
              setPlatform(portalId);
              setActiveTab('search');
              setHasSearched(true);
              setCurrentOffset(0);
              fetchJobs({ platform: portalId, currentOffset: 0 });
            }}
          />
        ) : activeTab === 'analytics' ? (
          <AnalyticsView />
        ) : (
          <>
            <ControlsPanel
              searchInput={searchInput}
              setSearchInput={setSearchInput}
              jobTitle={jobTitle}
              setJobTitle={setJobTitle}
              platform={platform}
              setPlatform={setPlatform}
              country={country}
              setCountry={setCountry}
              isRemoteOnly={isRemoteOnly}
              setIsRemoteOnly={setIsRemoteOnly}
              datePosted={datePosted}
              setDatePosted={setDatePosted}
              jobType={jobType}
              setJobType={setJobType}
              onSearch={handleSearch}
              onReset={handleReset}
            />

            <main>
              {!hasSearched ? (
                <div style={{ textAlign: 'center', padding: '60px 20px', color: '#64748b' }}>
                  <i className="fa-solid fa-magnifying-glass" style={{ fontSize: '2rem', marginBottom: '12px', opacity: 0.5 }}></i>
                  <p style={{ fontSize: '1rem', fontWeight: 600 }}>Set your filters and click Search to find jobs.</p>
                  <p style={{ fontSize: '0.85rem', marginTop: '4px' }}>Nothing is fetched until you search.</p>
                </div>
              ) : (
                <>
                  <div className="jobs-section-header">
                    <h2 className="section-title">Aggregated Job Listings</h2>
                    <span className="results-count">
                      {isLoading
                        ? 'Searching\u2026'
                        : `Showing ${startItem}\u2013${endItem} of ${totalJobsCount} jobs`}
                      {!isLoading && cacheInfo && (
                        <span
                          className={`cache-chip ${cacheInfo.scrape_triggered ? 'cache-chip-live' : 'cache-chip-cached'}`}
                          title={
                            cacheInfo.scrape_triggered
                              ? `Live ${cacheInfo.refresh_mode || 'full'} scrape (${cacheInfo.date_bucket} window)`
                              : `Served from cache, last scraped ${cacheInfo.cache_age_minutes ?? 0} min ago`
                          }
                        >
                          <i className={`fa-solid ${cacheInfo.scrape_triggered ? 'fa-tower-broadcast' : 'fa-database'}`} />
                          {cacheInfo.scrape_triggered ? ' Live' : ' Cached'}
                        </span>
                      )}
                    </span>
                  </div>

                  {scrapeStatus === 'in_progress' && (
                    <div className="scrape-banner">
                      <i className="fa-solid fa-tower-broadcast fa-fade" />
                      <span>
                        Still fetching from the job portals&hellip; results will update
                        automatically as they arrive.
                      </span>
                    </div>
                  )}
                  {scrapeStatus === 'timed_out' && (
                    <div className="scrape-banner scrape-banner-warn">
                      <i className="fa-solid fa-triangle-exclamation" />
                      <span>The background fetch is taking unusually long. Search again to retry.</span>
                    </div>
                  )}

                  <JobsGrid
                    jobs={jobs}
                    isLoading={isLoading}
                    emptyReason={emptyReason}
                    liveFetchTriggered={liveFetchTriggered}
                  />

                  {!isLoading && totalJobsCount > limit && (
                    <Pagination
                      currentOffset={currentOffset}
                      limit={limit}
                      totalJobsCount={totalJobsCount}
                      onPrevPage={handlePrevPage}
                      onNextPage={handleNextPage}
                    />
                  )}
                </>
              )}
            </main>
          </>
        )}
        </div>

      </div>
    </div>
  );
}
