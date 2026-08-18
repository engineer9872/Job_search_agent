import React from 'react';

export default function Navbar() {
  return (
    <header className="navbar">
      <div className="brand">
        <div className="brand-icon-purity">
          <i className="fa-solid fa-atom"></i>
        </div>
        <div>
          <h1 className="brand-title">TalentSphere <span style={{ color: '#059669' }}>AI</span></h1>
          <div className="brand-subtitle">Autonomous Multi-Portal Job Intelligence System</div>
        </div>
      </div>
      <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
        <span style={{ fontSize: '0.8rem', color: '#64748b', fontWeight: 600, background: '#f1f5f9', padding: '6px 12px', borderRadius: '10px', border: '1px solid #cbd5e1' }}>
          <i className="fa-solid fa-circle" style={{ color: '#059669', fontSize: '8px', marginRight: '6px' }}></i>
          Live Multi-Source Telemetry Active
        </span>
      </div>
    </header>
  );
}

