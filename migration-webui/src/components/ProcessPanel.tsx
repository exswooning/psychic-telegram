import React, { useEffect, useState } from 'react'
import { Alert, Box, Button, Stack, Typography } from '@mui/material'
import { Search as WhereIcon } from '@mui/icons-material'
import { fetchProcStats, fetchStackDump, ProcStats } from '@/api/client'
import { describeElapsed } from '@/hooks/useRunningJobs'

/**
 * A running job's process, measured on the box: what `ps -o rss,nlwp,etime` and
 * `py-spy dump` answered over SSH. Children are counted in, so a pass split
 * across processes shows its whole footprint. Superadmin only, server-side.
 */
const ProcessPanel: React.FC<{ pid: number }> = ({ pid }) => {
  const [stats, setStats] = useState<ProcStats | null>(null)
  const [dump, setDump] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    const read = () => fetchProcStats(pid).then((s) => { if (live) setStats(s) }).catch(() => {})
    read()
    const id = setInterval(read, 5000)
    return () => { live = false; clearInterval(id) }
  }, [pid])

  const where = async () => {
    setBusy(true)
    try {
      const r = await fetchStackDump(pid)
      setDump(r.ok ? (r.dump || '(nothing printed)') : (r.msg || 'refused'))
    } finally {
      setBusy(false)
    }
  }

  if (stats && !stats.ok) {
    return <Alert severity="info" sx={{ mb: 2 }} data-testid="process-refused">{stats.msg}</Alert>
  }
  return (
    <Box sx={{ mb: 2 }} data-testid="process-panel">
      <Stack direction="row" spacing={3} sx={{ flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
        <Typography variant="body2"><b>pid</b> {pid}</Typography>
        {stats && (
          <>
            <Typography variant="body2" data-testid="process-rss"><b>memory</b> {stats.rss_mb.toLocaleString()} MB</Typography>
            <Typography variant="body2"><b>CPU</b> {stats.cpu_pct}%</Typography>
            <Typography variant="body2"><b>threads</b> {stats.threads}</Typography>
            <Typography variant="body2"><b>running for</b> {describeElapsed(stats.elapsed_s)}</Typography>
            {stats.processes > 1 && (
              <Typography variant="body2"><b>processes</b> {stats.processes}</Typography>
            )}
          </>
        )}
        <Button size="small" variant="outlined" startIcon={<WhereIcon />} disabled={busy}
                onClick={where} data-testid="process-where">
          {busy ? 'Reading…' : 'Where is it?'}
        </Button>
      </Stack>
      {dump !== null && (
        <Box component="pre" data-testid="process-dump" sx={{
          fontSize: 11, p: 1.5, mt: 1, bgcolor: 'action.hover', borderRadius: 1,
          overflowX: 'auto', maxHeight: 360, whiteSpace: 'pre', m: 0,
        }}>{dump}</Box>
      )}
    </Box>
  )
}

export default ProcessPanel
