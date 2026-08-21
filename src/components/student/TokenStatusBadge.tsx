import React from 'react';

interface TokenStatusBadgeProps {
  status: 'WAITING' | 'SERVING' | 'COMPLETED' | 'CANCELLED' | 'SKIPPED' | 'HELD' | 'WAITLISTED' | 'PROMOTED' | 'EXPIRED';
  size?: 'sm' | 'md' | 'lg';
}

export const TokenStatusBadge: React.FC<TokenStatusBadgeProps> = ({ status, size = 'md' }) => {
  let bgColor = '';
  let color = '';

  switch (status) {
    case 'WAITLISTED':
      bgColor = 'rgba(139, 92, 246, 0.2)'; // Purple
      color = '#a78bfa';
      break;
    case 'PROMOTED':
      bgColor = 'rgba(6, 182, 212, 0.2)'; // Cyan
      color = '#22d3ee';
      break;
    case 'EXPIRED':
      bgColor = 'rgba(156, 163, 175, 0.2)'; // Slate
      color = '#9ca3af';
      break;
    case 'WAITING':
      bgColor = 'rgba(59, 130, 246, 0.2)'; // Blue
      color = '#60a5fa';
      break;
    case 'SERVING':
      bgColor = 'rgba(16, 185, 129, 0.2)'; // Green
      color = '#34d399';
      break;
    case 'COMPLETED':
      bgColor = 'rgba(107, 114, 128, 0.2)'; // Gray
      color = '#9ca3af';
      break;
    case 'CANCELLED':
    case 'SKIPPED':
      bgColor = 'rgba(239, 68, 68, 0.2)'; // Red
      color = '#f87171';
      break;
    case 'HELD':
      bgColor = 'rgba(245, 158, 11, 0.2)'; // Yellow
      color = '#fbbf24';
      break;
    default:
      bgColor = 'rgba(107, 114, 128, 0.2)';
      color = '#9ca3af';
  }

  const padding = size === 'sm' ? '0.125rem 0.5rem' : size === 'lg' ? '0.375rem 1rem' : '0.25rem 0.75rem';
  const fontSize = size === 'sm' ? '0.7rem' : size === 'lg' ? '0.875rem' : '0.75rem';

  return (
    <span style={{
      backgroundColor: bgColor,
      color: color,
      padding: padding,
      borderRadius: 'var(--radius-full)',
      fontSize: fontSize,
      fontWeight: 700,
      textTransform: 'uppercase',
      display: 'inline-flex',
      alignItems: 'center',
      border: `1px solid ${color}40`
    }}>
      {status}
    </span>
  );
};
