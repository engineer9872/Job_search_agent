import React, { useState, useEffect } from 'react';

export default function PortalHealthModal({ isOpen, onClose }) {
  const [healthData, setHealthData] = useState(null);
  const [isLoading, setIsLoading] = useState(false);

  const fetchHealth = async () => {
    setIsLoading(true);
    try {
      const res = await fetch('/api/status');
      if (res.ok) {
        const data = await res.json();
        setHealthData(data);
      }
    } catch (err) {
      console.error('Failed to fetch pipeline status health:', err);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchHealth();
    }
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div className="health-modal-overlay" onClick={onClose}>
      <div className="health-modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="health-modal-header">
          <div>
            <h2 style={{ fontSize: '1.3rem', fontWeight: 700, color: 'var(--accent-cyan)' }}>
              <i className="fa-solid fa-shield-heart"></i> 4-Layer Pipeline Health Monitor
            </h2>
            <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
              Tracking 20+ Remote Contract Portals & Fallback Layer Operating Status
            </div>
          </div>
          <button className="ai-close-btn" onClick={onClose}>&times;</button>
        </div>

        <div className="health-modal-body">
          {isLoading ? (
            <div style={{ textAlign: 'center', padding: '40px' }}>
              <div className="spinner"></div>
              <div>Fetching portal health metrics...</div>
            </div>
          ) : healthData ? (
            <div>
              <div style={{ display: 'flex', gap: '12px', marginBottom: '20px' }}>
                <div className="meta-pill" style={{ background: 'rgba(0, 242, 254, 0.1)', color: 'var(--accent-cyan)' }}>
                  Total Configured Portals: <strong>{healthData.total_configured_portals}</strong>
                </div>
                <div className="meta-pill" style={{ background: 'rgba(16, 185, 129, 0.1)', color: 'var(--accent-emerald)' }}>
                  Status: <strong>Active Multi-Layer Fallback</strong>
                </div>
              </div>

              <div className="health-table-wrapper" style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border-card)', textAlign: 'left', color: 'var(--text-muted)' }}>
                      <th style={{ padding: '10px' }}>Portal Name</th>
                      <th style={{ padding: '10px' }}>Category</th>
                      <th style={{ padding: '10px' }}>Active Layer</th>
                      <th style={{ padding: '10px' }}>Health Status</th>
                      <th style={{ padding: '10px' }}>Jobs in DB</th>
                      <th style={{ padding: '10px' }}>ToS Guardrail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {healthData.portals_status.map((p) => (
                      <tr key={p.portal_id} style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.05)' }}>
                        <td style={{ padding: '10px', fontWeight: 600, color: '#fff' }}>{p.portal_name}</td>
                        <td style={{ padding: '10px', color: 'var(--text-muted)' }}>{p.type}</td>
                        <td style={{ padding: '10px' }}>
                          <span style={{
                            padding: '3px 8px',
                            borderRadius: '6px',
                            fontSize: '0.75rem',
                            fontWeight: 700,
                            background: p.operating_layer === 'Layer 1' ? 'rgba(16, 185, 129, 0.2)' : p.operating_layer === 'Layer 2' ? 'rgba(0, 242, 254, 0.2)' : p.operating_layer === 'Layer 3' ? 'rgba(245, 158, 11, 0.2)' : 'rgba(244, 63, 94, 0.2)',
                            color: p.operating_layer === 'Layer 1' ? '#10b981' : p.operating_layer === 'Layer 2' ? '#00f2fe' : p.operating_layer === 'Layer 3' ? '#f59e0b' : '#f43f5e',
                          }}>
                            {p.operating_layer}
                          </span>
                        </td>
                        <td style={{ padding: '10px' }}>
                          <span style={{
                            color: p.status === 'HEALTHY' ? '#10b981' : '#f59e0b',
                            fontWeight: 600,
                          }}>
                            {p.status}
                          </span>
                        </td>
                        <td style={{ padding: '10px', fontWeight: 600 }}>{p.total_jobs_in_db}</td>
                        <td style={{ padding: '10px' }}>
                          {p.tos_requires_api ? (
                            <span style={{ fontSize: '0.72rem', color: '#f59e0b', background: 'rgba(245, 158, 11, 0.1)', padding: '2px 6px', borderRadius: '4px' }}>
                              OAuth API Only
                            </span>
                          ) : (
                            <span style={{ fontSize: '0.72rem', color: '#10b981' }}>Standard</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <div>No status available.</div>
          )}
        </div>
      </div>
    </div>
  );
}
