import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, Divider, FormControlLabel, LinearProgress, MenuItem,
  Paper, Stack, Switch, Table, TableBody, TableCell, TableContainer, TableHead,
  TableRow, TextField, Tooltip, Typography,
} from '@mui/material'
import { Speed as BenchIcon, Circle as DotIcon } from '@mui/icons-material'
import {
  BenchmarkResult, BenchmarkRunning, startBenchmark, fetchBenchmarkResults,
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

const PHASE_LABELS: Record<string, string> = {
  wipe: 'Wipe target',
  ledger: 'Reset ledger',
  migrate: 'Migrate',
  audit: 'Audit ACLs',
}

const fmtElapsed = (s: number) => {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m ${s % 60}s`
}

/**
 * A benchmark in flight.
 *
 * Previously this was a single chip reading "run in flight · pid 12345",
 * which is indistinguishable from a hung process for however many hours the
 * run takes. The two questions an operator actually has are "which stage is
 * it on" and "is the file count still going up", so those are the two things
 * on screen.
 *
 * No percentage bar. The total file count is not known until the walk
 * finishes, and inventing a denominator would put a number on screen that
 * moves backwards when the walk finds more files than it guessed.
 */
const LiveRun: React.FC<{ run: BenchmarkRunning }> = ({ run }) => {
  const p = run.progress
  const elapsed = run.elapsedS ?? 0
  // Migrate is the only phase whose rate means anything -- during the wipe
  // the file count is falling, and during the audit it is static.
  const rate = p && elapsed > 0 && run.phase === 'migrate'
    ? (p.files / elapsed).toFixed(2) : null
  const active = run.phases?.indexOf(run.phase ?? '') ?? -1

  return (
    <Box sx={{ mt: 2, p: 2, borderRadius: 2, bgcolor: 'action.hover' }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5, flexWrap: 'wrap', gap: 1 }}>
        <Chip size="small" color="primary" icon={<DotIcon sx={{ fontSize: 10 }} />}
              label={run.label || 'benchmark'} />
        <Typography variant="body2" sx={{ fontWeight: 600 }}>
          {run.phaseLabel ?? 'Running'}
        </Typography>
        <Box sx={{ flexGrow: 1 }} />
        <Typography variant="caption" color="text.secondary"
                    sx={{ fontVariantNumeric: 'tabular-nums' }}>
          {fmtElapsed(elapsed)} elapsed · pid {run.pid}
        </Typography>
      </Stack>

      <Stack direction="row" spacing={0.5} sx={{ mb: 1.5, flexWrap: 'wrap', gap: 0.5 }}>
        {(run.phases ?? []).map((ph, i) => (
          <Chip
            key={ph} size="small"
            label={PHASE_LABELS[ph] ?? ph}
            color={i === active ? 'primary' : 'default'}
            variant={i < active ? 'filled' : i === active ? 'filled' : 'outlined'}
            sx={{ opacity: i < active ? 0.55 : 1 }}
          />
        ))}
      </Stack>

      <LinearProgress sx={{ mb: 1.5, height: 6, borderRadius: 1 }} />

      {p && (
        <Stack direction="row" spacing={3} sx={{ flexWrap: 'wrap', gap: 2 }}>
          <Metric label="files" value={p.files.toLocaleString()} />
          <Metric label="folders" value={p.folders.toLocaleString()} />
          <Metric label="failed" value={p.failed.toLocaleString()}
                  bad={p.failed > 0} />
          <Metric label="acl failed" value={p.aclFailed.toLocaleString()}
                  bad={p.aclFailed > 0} />
          {rate && <Metric label="files/sec" value={rate} />}
        </Stack>
      )}
    </Box>
  )
}

const Metric: React.FC<{ label: string; value: string; bad?: boolean }> =
  ({ label, value, bad }) => (
    <Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
        {label}
      </Typography>
      <Typography variant="body2"
                  color={bad ? 'error.main' : 'text.primary'}
                  sx={{ fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </Typography>
    </Box>
  )

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
  const [running, setRunning] = useState<BenchmarkRunning>({ running: false })

  const refresh = useCallback(() => {
    fetchBenchmarkResults().then(setResults).catch(() => {})
    fetchBenchmarkRunning().then(setRunning).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    // Two rates. A run takes hours, so polling the completed-results list
    // that often would be waste; but while one is in flight the phase and
    // file count are the only thing on screen that changes, and a 15s lag
    // there reads as a hung benchmark.
    const id = setInterval(refresh, running.running ? 5_000 : 20_000)
    return () => clearInterval(id)
  }, [refresh, running.running])

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
      </Stack>

      <Typography variant="caption" color="text.secondary">
        Wipes the target, resets the Drive ledger, migrates, audits ACLs, then
        judges the run. A run that loses grants fails no matter how fast it was.
      </Typography>

      {running.running && <LiveRun run={running} />}

      <Stack direction="row" sx={{ mt: 2, flexWrap: 'wrap', gap: 2 }}>
        <TextField size="small" label="Label" value={label} sx={{ width: { xs: '100%', sm: 110 } }}
                   onChange={(e) => setLabel(e.target.value)} />
        <TextField
          size="small" label="Type the TARGET domain" sx={{ width: { xs: '100%', sm: 260 } }}
          value={confirmDomain} onChange={(e) => setConfirmDomain(e.target.value)}
          placeholder={targetDomain || 'a.example.com'}
          error={!!confirmDomain && !!targetDomain && confirmDomain.trim().toLowerCase() !== targetDomain.toLowerCase()}
          helperText="Confirms which tenant gets emptied"
        />
        <TextField
          select size="small" label="Drive file workers" value={workers}
          sx={{ width: { xs: '100%', sm: 280 } }} onChange={(e) => setWorkers(Number(e.target.value))}
        >
          {WORKER_OPTIONS.map((o) => (
            <MenuItem key={o.v} value={o.v}>{o.label}</MenuItem>
          ))}
        </TextField>
        <Tooltip title="Google's ceiling is 3/sec per account and is not raiseable. Above it you buy 429s and retry backoff, which is net slower.">
          <TextField size="small" label="Write QPS" type="number" sx={{ width: { xs: '100%', sm: 120 } }}
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
      <TableContainer sx={{ overflowX: 'auto' }}>
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
      </TableContainer>

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
