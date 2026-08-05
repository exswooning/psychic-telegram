export function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000)
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)
  const days = Math.floor(hours / 24)
  if (days > 0) return `${days}d ${hours % 24}h`
  if (hours > 0) return `${hours}h ${minutes % 60}m`
  return `${minutes}m`
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}

export function formatSpeed(bytesPerSec: number): string {
  return formatBytes(bytesPerSec) + '/s'
}

export function formatNumber(num: number): string {
  if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M'
  if (num >= 1000) return (num / 1000).toFixed(1) + 'K'
  return num.toString()
}

export function formatPercentage(value: number): string {
  return `${value.toFixed(1)}%`
}

export function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    not_started: 'Not Started',
    waiting: 'Waiting',
    in_progress: 'In Progress',
    retrying: 'Retrying',
    completed: 'Completed',
    failed: 'Failed',
    needs_attention: 'Needs Attention',
    paused: 'Paused',
    verified: 'Verified',
    mismatch: 'Mismatch',
    pending: 'Pending',
  }
  return labels[status] || status
}

export function statusColor(status: string): string {
  const colors: Record<string, string> = {
    not_started: 'default',
    waiting: 'warning',
    in_progress: 'primary',
    retrying: 'warning',
    completed: 'success',
    failed: 'error',
    needs_attention: 'error',
    paused: 'default',
    verified: 'success',
    mismatch: 'warning',
    pending: 'default',
  }
  return colors[status] || 'default'
}

export function statusIcon(status: string): string {
  const icons: Record<string, string> = {
    not_started: '○',
    waiting: '◔',
    in_progress: '◔',
    retrying: '↻',
    completed: '✓',
    failed: '✗',
    needs_attention: '⚠',
    paused: '⏸',
    verified: '✓',
    mismatch: '⚠',
    pending: '○',
  }
  return icons[status] || '○'
}