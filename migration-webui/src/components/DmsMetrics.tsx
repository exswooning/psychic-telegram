import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Box, Button, Chip, Stack, Typography,
} from '@mui/material'
import { Refresh as RefreshIcon } from '@mui/icons-material'
import { fetchDmsMetrics, runAction, DmsMetricsResponse } from '@/api/client'

// The order and grouping Google's console shows them in.
const TASK_FIELDS = ['Discovered tasks', 'Successful', 'Failed', 'Skipped', 'Warning']
const MAIL_FIELDS = ['Users processed', 'Emails discovered', 'Emails imported',
  'Emails skipped', 'Emails failed']

const nf = new Intl.NumberFormat()

function age(seconds: number | null): string {
  if (seconds == null) return 'never'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  return `${Math.round(seconds / 3600)}h ago`
}

const statusColor = (s: string): 'info' | 'success' | 'warning' | 'default' => {
  const t = s.toLowerCase()
  if (t.includes('progress')) return 'info'
  if (t.includes('complete')) return 'success'
  if (t.includes('stopped')) return 'warning'
  return 'default'
}

/**
 * Shows the DMS import's live counters, scraped from the Admin console.
 *
 * The numbers exist only in Google's console (that is the trade-off of
 * handing mail to DMS), so this reads a server-side cache and offers a
 * Refresh that re-scrapes. It never invents a value: if nothing has been
 * scraped yet it says so rather than showing zeros.
 */
const DmsMetrics: React.FC = () => {
  const [resp, setResp] = useState<DmsMetricsResponse | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    try {
      setResp(await fetchDmsMetrics())
    } catch {
      /* keep the last good values on a dropped poll */
    }
  }, [])

  useEffect(() => {
    load()
    pollRef.current = setInterval(load, 15000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [load])

  const refresh = async () => {
    setError(null)
    setRefreshing(true)
    try {
      const r = await runAction('dms_metrics_refresh')
      if (!r.ok) { setError(r.error || 'could not read the console'); return }
      // the scrape is a ~1-minute browser job; re-read a few times as it lands
      for (let i = 0; i < 8; i++) {
        await new Promise((res) => setTimeout(res, 9000))
        await load()
      }
    } finally {
      setRefreshing(false)
    }
  }

  const data = resp?.data
  const m = data?.metrics || {}
  const has = (k: string) => Object.prototype.hasOwnProperty.call(m, k)

  const Cell: React.FC<{ label: string }> = ({ label }) => (
    has(label) ? (
      <Box sx={{ minWidth: 110 }}>
        <Typography variant="caption" color="text.secondary">{label}</Typography>
        <Typography variant="h6" sx={{ fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}
                    color={/failed/i.test(label) && m[label] > 0 ? 'error.main' : 'text.primary'}>
          {nf.format(m[label])}
        </Typography>
      </Box>
    ) : null
  )

  return (
    <Box sx={{ mt: 2 }} data-testid="dms-metrics">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
          Google DMS import
        </Typography>
        {data && (
          <Chip size="small" color={statusColor(data.status)} label={data.status} />
        )}
        <Box sx={{ flexGrow: 1 }} />
        <Typography variant="caption" color="text.secondary">
          read {age(resp?.ageSeconds ?? null)}
        </Typography>
        <Button size="small" startIcon={<RefreshIcon />} onClick={refresh}
                disabled={refreshing} data-testid="dms-metrics-refresh">
          {refreshing ? 'Reading…' : 'Refresh'}
        </Button>
      </Stack>

      {!data && (
        <Typography variant="body2" color="text.secondary">
          No metrics read yet — press Refresh to read the counters from the
          Admin console.
        </Typography>
      )}

      {data && (
        <Stack spacing={1.5}>
          <Stack direction="row" spacing={3} flexWrap="wrap" useFlexGap>
            {TASK_FIELDS.map((f) => <Cell key={f} label={f} />)}
          </Stack>
          <Stack direction="row" spacing={3} flexWrap="wrap" useFlexGap>
            {MAIL_FIELDS.map((f) => <Cell key={f} label={f} />)}
          </Stack>
        </Stack>
      )}

      {error && (
        <Typography variant="caption" color="error" sx={{ display: 'block', mt: 0.5 }}>
          {error}
        </Typography>
      )}
    </Box>
  )
}

export default DmsMetrics
