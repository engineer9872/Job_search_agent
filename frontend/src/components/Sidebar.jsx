import React from 'react';

export default function Sidebar({ activeTab, setActiveTab }) {
  return (
    <aside className="sidebar">
      {/* BRAND LOGO */}
      <div className="sidebar-brand">
        <div className="brand-icon-purity">
          <i className="fa-solid fa-atom"></i>
        </div>
        <div>
          <h2 className="brand-name">TalentSphere <span className="brand-ai">AI</span></h2>
          <div className="brand-subtitle">Autonomous Job Intelligence</div>
        </div>
      </div>

      {/* USER PROFILE CARD */}
      <div className="sidebar-user-card">
        <div className="avatar-wrapper">
          <img src="https://api.dicebear.com/7.x/avataaars/svg?seed=Wade" alt="User Avatar" className="avatar-img" />
          <span className="online-indicator"></span>
        </div>
        <div className="user-info">
          <div className="user-name">Esther Howard</div>
          <div className="user-role">Senior Full-Stack Candidate</div>
        </div>
      </div>

      {/* NAVIGATION MENU */}
      <nav className="sidebar-nav">
        <div className="nav-section-title">MENU</div>
        
        <button
          className={`nav-item ${activeTab === 'search' ? 'active' : ''}`}
          onClick={() => setActiveTab('search')}
        >
          <i className="fa-solid fa-compass nav-icon"></i>
          <span>Job Search</span>
        </button>

        <button
          className={`nav-item ${activeTab === 'platforms' ? 'active' : ''}`}
          onClick={() => setActiveTab('platforms')}
        >
          <i className="fa-solid fa-layer-group nav-icon"></i>
          <span>Job Platforms (10)</span>
          <span className="badge-count">10</span>
        </button>

        <button
          className={`nav-item ${activeTab === 'analytics' ? 'active' : ''}`}
          onClick={() => setActiveTab('analytics')}
        >
          <i className="fa-solid fa-chart-pie nav-icon"></i>
          <span>Analytics & Trends</span>
        </button>



        <div className="nav-section-title" style={{ marginTop: '20px' }}>LEGAL & GOVERNANCE</div>

        <div className="nav-section-title" style={{ marginTop: '20px' }}>SUPPORT</div>

        <button className="nav-item disabled">
          <i className="fa-solid fa-book nav-icon"></i>
          <span>Documentation & FAQ</span>
        </button>
      </nav>
    </aside>
  );
}
