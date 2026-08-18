import React from 'react';

const JOB_TITLE_OPTIONS = [
  'Software Engineer',
  'Data Scientist',
  'Machine Learning Engineer',
  'AI Engineer',
  'DevOps Engineer',
  'Cloud Engineer',
  'Product Manager',
  'Data Engineer',
  'QA Engineer',
  'SRE',
  'Cybersecurity',
  'ServiceNow',
];

function MultiSelectDropdown({ options, selected, onChange, allLabel }) {
  const [open, setOpen] = React.useState(false);
  const wrapperRef = React.useRef(null);

  React.useEffect(() => {
    const handleClickOutside = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const toggleOption = (opt) => {
    if (selected.includes(opt)) {
      onChange(selected.filter((s) => s !== opt));
    } else {
      onChange([...selected, opt]);
    }
  };

  const clearAll = () => onChange([]);

  const displayText =
    selected.length === 0
      ? allLabel
      : selected.length === 1
      ? selected[0]
      : `${selected.length} Job Titles Selected`;

  return (
    <div ref={wrapperRef} style={{ position: 'relative' }}>
      <button
        type="button"
        className="select-custom"
        style={{ textAlign: 'left', cursor: 'pointer', width: '100%' }}
        onClick={() => setOpen((o) => !o)}
      >
        {displayText}
      </button>
      {open && (
        <div
          style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            zIndex: 50,
            background: '#1e1e2e',
            border: '1px solid rgba(255,255,255,0.15)',
            borderRadius: '8px',
            padding: '8px',
            minWidth: '240px',
            maxHeight: '280px',
            overflowY: 'auto',
            marginTop: '4px',
            boxShadow: '0 8px 24px rgba(0,0,0,0.4)',
          }}
        >
          <div
            onClick={clearAll}
            style={{
              padding: '6px 8px',
              cursor: 'pointer',
              fontSize: '0.85rem',
              color: '#e4e4e7',
              opacity: 0.9,
            }}
          >
            {allLabel} {selected.length === 0 ? '✓' : ''}
          </div>
          <hr style={{ opacity: 0.15, margin: '4px 0', borderColor: '#ffffff' }} />
          {options.map((opt) => (
            <label
              key={opt}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                padding: '6px 8px',
                cursor: 'pointer',
                fontSize: '0.85rem',
                color: '#e4e4e7',
              }}
            >
              <input
                type="checkbox"
                checked={selected.includes(opt)}
                onChange={() => toggleOption(opt)}
              />
              {opt}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ControlsPanel({
  searchInput,
  setSearchInput,
  jobTitle,
  setJobTitle,
  platform,
  setPlatform,
  country,
  setCountry,
  isRemoteOnly,
  setIsRemoteOnly,
  datePosted,
  setDatePosted,
  jobType,
  setJobType,
  onSearch,
  onReset,
}) {
  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      onSearch();
    }
  };

  return (
    <section className="controls-panel">
      <div className="search-box-row">
        <div className="search-input-wrapper">
          <i className="fa-solid fa-magnifying-glass search-icon"></i>
          <input
            type="text"
            className="search-input"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Search by title, skills, keyword, or company (e.g., 'Python Developer', 'Stripe', 'Seattle')..."
          />
        </div>
        <button onClick={onSearch} className="btn-primary">
          <i className="fa-solid fa-arrow-right"></i> Search
        </button>
      </div>

      {/* FILTERS */}
      <div className="filters-row">
        {/* Job Title Filter — Multi-Select */}
        <div className="filter-group">
          <label className="filter-label">Job Title:</label>
          <MultiSelectDropdown
            options={JOB_TITLE_OPTIONS}
            selected={jobTitle}
            onChange={setJobTitle}
            allLabel="All Job Titles (All Portals)"
          />
        </div>

        {/* Platform Filter */}
        <div className="filter-group">
          <label className="filter-label" htmlFor="platformSelect">Platform:</label>
          <select
            id="platformSelect"
            className="select-custom"
            value={platform}
            onChange={(e) => setPlatform(e.target.value)}
          >
            <option value="all">All Platforms (10)</option>
            <optgroup label="Official Enterprise &amp; API Partners">
              <option value="linkedin">LinkedIn Jobs</option>
              <option value="indeed">Indeed</option>
              <option value="glassdoor">Glassdoor</option>
            </optgroup>
            <optgroup label="Remote Job Boards &amp; Aggregators">
              <option value="dice">Dice</option>
              <option value="ziprecruiter">ZipRecruiter</option>
              <option value="usajobs">USAJOBS (U.S. Federal)</option>
              <option value="careerbuilder">CareerBuilder</option>
              <option value="simplyhired">SimplyHired</option>
              <option value="weworkremotely">We Work Remotely</option>
            </optgroup>
            <optgroup label="Remote Staffing Vendors">
              <option value="hired">Hired</option>
            </optgroup>
          </select>
        </div>

        {/* Country Filter */}
        <div className="filter-group">
          <label className="filter-label" htmlFor="countrySelect">Country:</label>
          <select
            id="countrySelect"
            className="select-custom"
            value={country}
            onChange={(e) => setCountry(e.target.value)}
          >
            <option value="">All Countries</option>
            <option value="US">United States (US)</option>
            <option value="GB">United Kingdom (GB)</option>
            <option value="IN">India (IN)</option>
            <option value="CA">Canada (CA)</option>
            <option value="AU">Australia (AU)</option>
            <option value="DE">Germany (DE)</option>
          </select>
        </div>

        <label className="toggle-wrapper" title={isRemoteOnly ? "Active: Showing Remote Only Jobs" : "Active: Showing Onsite & Hybrid Roles"}>
          <input
            type="checkbox"
            className="toggle-checkbox"
            checked={isRemoteOnly}
            onChange={(e) => setIsRemoteOnly(e.target.checked)}
          />
          <span className="toggle-switch"></span>
          <span className="toggle-label-text">
            {isRemoteOnly ? 'Remote Only' : 'Onsite / Hybrid'}
          </span>
        </label>

        {/* Date Posted Filter */}
        <div className="filter-group">
          <label className="filter-label" htmlFor="datePostedSelect">Date Posted:</label>
          <select
            id="datePostedSelect"
            className="select-custom"
            value={datePosted}
            onChange={(e) => setDatePosted(e.target.value)}
          >
            {/* Reduced to the four windows the connectors can actually
                honour. Everything finer than 12h was removed: only USAJOBS
                and WeWorkRemotely report exact timestamps, so sub-hour
                options were promising precision the data never had.
                Kept in sync with GET /api/date-filters. */}
            <option value="past_12h">Past 12 Hours</option>
            <option value="past_24h">Past 24 Hours</option>
            <option value="past_7d">Past 7 Days</option>
            <option value="past_30d">Past 30 Days</option>
          </select>
        </div>

        {/* Job Type Filter */}
        <div className="filter-group">
          <label className="filter-label" htmlFor="jobTypeSelect">Job Type:</label>
          <select
            id="jobTypeSelect"
            className="select-custom"
            value={jobType}
            onChange={(e) => setJobType(e.target.value)}
          >
            <option value="all">All Types</option>
            <option value="fulltime">Full-time</option>
            <option value="contractor">Contractor / Freelance</option>
            <option value="parttime">Part-time</option>
            <option value="onsite">Onsite Only</option>
          </select>
        </div>

        {/* Reset Button */}
        <button
          onClick={onReset}
          className="btn-secondary"
          style={{ marginLeft: 'auto' }}
        >
          <i className="fa-solid fa-rotate-left"></i> Reset
        </button>
      </div>
    </section>
  );
}
