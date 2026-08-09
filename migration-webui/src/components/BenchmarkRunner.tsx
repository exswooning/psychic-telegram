import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, Divider, FormControlLabel, MenuItem, Paper,
  Stack, Switch, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Tooltip, Typography,
} from '@mui/material'
import { Speed as BenchIcon, Circle as DotIcon } from '@mui/icons-material'
import {
  BenchmarkResult, startBenchmark, fetchBenchmarkResults,
  fetchBenchmarkRunning, getOperator,
} from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * Launch and compare benchmark runs.
 *
 * The knobs are exposed rather than hard-coded because the interesting
 * question is which value of `drive_file_workers` is fastest without
 * breaking anything — and the default of 1 reproduces the current serial
 * baseline, so an operator who changes nothing measures the same thing the
 * last run measured rather than accidentally benchmarking a new config.
 *
 * The results table deliberately shows fidelity beside speed. B4 was
 * recorded as a timing result while 20,714 of 20,714 ACL grants were
 * silently failing; a table of elapsed times alone would have made that
 * run look like the winner.
 */
const WORKER_OPTIONS = [
  { v: 1, label: '1 — serial (today’s baseline)' },
  { v: 2, label: '2' },
  { v: 3, label: '3' },
  { v: 4, label: '4 — saturates the 3 writes/sec ceiling' },
]

interface Props { targetDomain?: string }

const BenchmarkRunner: React.FC<Props> = ({ targetDomain }) => {
  const [label, setLabel] = useState('B5')
  const [confirmDomain, setConfirmDomain] = useState('')
  const [workers, setWorkers] = useState(4)
  const [writeQps, setWriteQps] = useState(3.0)
  const [skipWipe, setSkipWipe] = useState(false)
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [results, setResults] = useState<BenchmarkResult[]>([])
  const [running, setRunning] = useState<{ running: boolean; label?: string; pid?: number }>({ running: false })

  const refresh = useCallback(() => {
    fetchBenchmarkResults().then(setResults).catch(() => {})
    fetchBenchmarkRunning().then(setRunning).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    // A benchmark takes hours and emits nothing until it finishes, so this
    // is a slow liveness check, not a progress feed — per-user progress
    // already arrives on the WebSocket.
    const id = setInterval(refresh, 15_000)
    return () => clearInterval(id)
  }, [refresh])

  const launch = async (reason: string) => {
    setBusy(true); setError(null)
    try {
      const r = await startBenchmark({
        reason, label, confirm_domain: confirmDomain, services: 'drive',
        drive_file_workers: workers, drive_write_qps: writeQps, skip_wipe: skipWipe,
      })
      setMsg(r.detail); setAsk(false); refresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const isAdmin = !!getOperator()
  const canLaunch = isAdmin && label.trim() && confirmDomain.trim() && !running.running

  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <BenchIcon color="action" />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>Benchmarks</Typography>
        {running.running && (
          <Chip size="small" color="primary" icon={<DotIcon sx={{ fontSize: 10 }} />}
                label={`${running.label || 'run'} in flight · pid ${running.pid}`} />
        )}
      </Stack>

      <Typography variant="caption" color="text.secondary">
        Wipes the target, resets the Drive ledger, migrates, audits ACLs, then
        judges the run. A run that loses grants fails no matter how fast it was.
      </Typography>

      <Stack direction="row" spacing={2} sx={{ mt: 2, flexWrap: 'wrap', gap: 2 }}>
        <TextField size="small" label="Label" value={label} sx={{ width: 110 }}
                   onChange={(e) => setLabel(e.target.value)} />
        <TextField
          size="small" label="Type the TARGET domain" sx={{ width: 260 }}
          value={confirmDomain} onChange={(e) => setConfirmDomain(e.target.value)}
          placeholder={targetDomain || 'a.example.com'}
          error={!!confirmDomain && !!targetDomain && confirmDomain.trim().toLowerCase() !== targetDomain.toLowerCase()}
          helperText="Confirms which tenant gets emptied"
        />
        <TextField
          select size="small" label="Drive file workers" value={workers}
          sx={{ width: 280 }} onChange={(e) => setWorkers(Number(e.target.value))}
        >
          {WORKER_OPTIONS.map((o) => (
            <MenuItem key={o.v} value={o.v}>{o.label}</MenuItem>
          ))}
        </TextField>
        <Tooltip title="Google's ceiling is 3/sec per account and is not raiseable. Above it you buy 429s and retry backoff, which is net slower.">
          <TextField size="small" label="Write QPS" type="number" sx={{ width: 120 }}
                     value={writeQps} onChange={(e) => setWriteQps(Number(e.target.value))}
                     inputProps={{ step: 0.5, min: 0.5, max: 10 }} />
        </Tooltip>
        <FormControlLabel
          control={<Switch checked={skipWipe} onChange={(e) => setSkipWipe(e.target.checked)} />}
          label={<Typography variant="body2">Skip wipe (measure only)</Typography>}
        />
      </Stack>

      {workers > 1 && !skipWipe && (
        <Alert severity="warning" sx={{ mt: 2 }}>
          <strong>{workers} workers has not run against a real tenant yet.</strong>{' '}
          Its safety rests on the write limiter holding at {writeQps}/sec. Consider
          a “Skip wipe” run first and check the 429 count before committing hours
          to a full batch.
        </Alert>
      )}

      <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
        <Button variant="contained" disabled={!canLaunch} onClick={() => setAsk(true)}>
          {running.running ? 'Benchmark already running' : 'Run benchmark'}
        </Button>
        {!isAdmin && (
          <Typography variant="caption" color="text.secondary" sx={{ alignSelf: 'center' }}>
            Set an operator name to launch.
          </Typography>
        )}
      </Stack>

      {msg && <Alert severity="success" sx={{ mt: 2 }} onClose={() => setMsg(null)}>{msg}</Alert>}

      <Divider sx={{ my: 2 }} />

      <Typography variant="overline" color="text.secondary">Past runs</Typography>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Run</TableCell>
            <TableCell align="right">W</TableCell>
            <TableCell align="right">s/file</TableCell>
            <TableCell align="right">elapsed</TableCell>
            <TableCell align="right">ACL</TableCell>
            <TableCell align="right">Extra</TableCell>
            <TableCell>Verdict</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {results.map((r) => (
            <TableRow key={r.file}>
              <TableCell>{r.label}</TableCell>
              <TableCell align="right">{r.driveFileWorkers ?? '—'}</TableCell>
              <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums', fontWeight: 700 }}>
                {r.secPerFile?.toFixed(2) ?? '—'}
              </TableCell>
              <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {r.elapsedS ? `${(r.elapsedS / 3600).toFixed(2)}h` : '—'}
              </TableCell>
              <TableCell align="right">{r.fidelityPct != null ? `${r.fidelityPct}%` : '—'}</TableCell>
              <TableCell align="right">
                {r.extraGrants ? (
                  <Typography variant="body2" color="error" fontWeight={700}>{r.extraGrants}</Typography>
                ) : '0'}
              </TableCell>
              <TableCell>
                <Tooltip title={r.failures?.join(' · ') || ''}>
                  <Chip size="small" label={r.passed ? 'PASS' : 'FAIL'}
                        color={r.passed ? 'success' : 'error'}
                        variant={r.passed ? 'outlined' : 'filled'} />
                </Tooltip>
              </TableCell>
            </TableRow>
          ))}
          {!results.length && (
            <TableRow>
              <TableCell colSpan={7}>
                <Typography variant="body2" color="text.secondary" sx={{ py: 2, textAlign: 'center' }}>
                  No completed runs yet.
                </Typography>
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>

      <ReasonCodeDialog
        open={ask} destructive={!skipWipe} confirmPhrase={skipWipe ? undefined : 'WIPE'}
        busy={busy} error={error}
        title={`Run benchmark ${label}`}
        description={
          skipWipe ? (
            <>Measures the current target state without wiping. Safe.</>
          ) : (
            <>
              <strong>This empties the target tenant’s Drive</strong> for{' '}
              <code>{confirmDomain}</code>, resets the Drive ledger, then
              re-migrates everything at {workers} worker(s). Gmail, Calendar and
              Chat are not touched. Expect several hours; it runs detached and
              survives closing this tab.
            </>
          )
        }
        onCancel={() => { setAsk(false); setError(null) }}
        onConfirm={launch}
      />
    </Paper>
  )
}

export default BenchmarkRunner
