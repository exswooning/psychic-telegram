/**
 * "Server-side copy safety test": does copying files server-side (files.copy by a source
 * user into a staging drive on the target) change anything on the source? One user, a few
 * files of each type; the report says per type, with evidence. Sandbox sources only: the
 * server refuses a government domain.
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, MenuItem, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Typography,
} from '@mui/material'
import { fetchCopySafety, startCopySafety } from '@/api/controlPlane'
import type { CopySafetyReport } from '@/api/controlPlane'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

const COLOR: Record<string, 'success' | 'error' | 'warning' | 'default'> = {
  SAFE: 'success', UNSAFE: 'error', UNKNOWN: 'warning',
}

export const CopySafetyCard: React.FC<{ accountId: number; users: string[] }> = ({ accountId, users }) => {
  const [user, setUser] = useState('')
  const [perType, setPerType] = useState(2)
  const [settle, setSettle] = useState(10)
  const [report, setReport] = useState<CopySafetyReport | null>(null)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState('')

  const load = useCallback(() => {
    fetchCopySafety(accountId).then(setReport).catch(() => {})
  }, [accountId])
  useEffect(load, [load])

  const confirm = async (reason: string) => {
    setBusy(true); setError(null)
    try {
      const r = await startCopySafety(reason, user, perType, settle, accountId)
      if (!r.ok) setError(r.detail || 'refused')
      else { setAsking(false); setNote(r.detail || 'started') }
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="copy-safety-card">
      <Typography variant="h6">Server-side copy safety test</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        Copies a few files of each type, as one source user, into a throwaway shared drive on the
        target, then compares that user&apos;s Drive before and after and reads Google&apos;s audit log.
        The source is never written to; if it changes, the report says what and leaves it. Never run
        against a government domain.
      </Typography>
      <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 1, mb: 1 }}>
        <TextField select size="small" label="Source user" value={user} sx={{ minWidth: 240 }}
                   onChange={(e) => setUser(e.target.value)} inputProps={{ 'data-testid': 'cs-user' }}>
          {users.map((u) => <MenuItem key={u} value={u}>{u}</MenuItem>)}
        </TextField>
        <TextField size="small" type="number" label="Files per type" value={perType} sx={{ width: 130 }}
                   onChange={(e) => setPerType(Math.max(1, Number(e.target.value) || 1))} />
        <TextField size="small" type="number" label="Settle minutes" value={settle} sx={{ width: 130 }}
                   onChange={(e) => setSettle(Math.max(0, Number(e.target.value) || 0))} />
        <Button variant="contained" disabled={!user} data-testid="cs-start" onClick={() => setAsking(true)}>Start</Button>
        <Button onClick={load}>Refresh</Button>
      </Stack>
      {note && <Alert severity="info" sx={{ mb: 1 }}>{note}</Alert>}
      {report && (
        <Box data-testid="cs-report">
          <Typography variant="subtitle2" sx={{ mt: 1 }}>
            Latest: {report.user}, {report.started} &mdash;{' '}
            <Chip size="small" label={report.overall} color={COLOR[report.overall] ?? 'default'} data-testid="cs-overall" />
            {!report.auditReadable && ' (audit log unreadable)'}
            {!report.cleanup?.ok && ' (cleanup incomplete: delete the BITPORT-SAFETY drive by hand)'}
          </Typography>
          {report.unattributed?.length > 0 && (
            <Alert severity="error" sx={{ my: 1 }}>
              Changes on the source nobody can be blamed for: {report.unattributed.map((c) => `${c.name} (${c.id}, ${c.modifiedTime})`).join('; ')}
            </Alert>
          )}
          <Table size="small">
            <TableHead>
              <TableRow><TableCell>Type</TableCell><TableCell>Sample</TableCell><TableCell>Copied</TableCell>
                <TableCell>Verdict</TableCell><TableCell>Evidence</TableCell></TableRow>
            </TableHead>
            <TableBody>
              {report.rows.map((r, i) => (
                <TableRow key={i}>
                  <TableCell>{r.type}</TableCell><TableCell>{r.name}</TableCell>
                  <TableCell>{r.copied ? 'yes' : 'no'}</TableCell>
                  <TableCell><Chip size="small" label={r.verdict} color={COLOR[r.verdict] ?? 'default'} /></TableCell>
                  <TableCell sx={{ maxWidth: 360, wordBreak: 'break-word' }}>
                    {typeof r.evidence === 'string' ? r.evidence
                      : Array.isArray(r.evidence) && r.evidence.length
                        ? r.evidence.map((e: { kind?: string; name?: string; id?: string; modifiedTime?: string }) =>
                          `${e.kind} ${e.name ?? ''} ${e.id ?? ''} ${e.modifiedTime ?? ''}`).join('; ')
                        : 'no change on the source'}
                  </TableCell>
                </TableRow>
              ))}
              {Object.entries(report.verdicts).filter(([, v]) => v === 'no sample').map(([t]) => (
                <TableRow key={`ns-${t}`}><TableCell>{t}</TableCell><TableCell colSpan={4}>no sample</TableCell></TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      )}
      <ReasonCodeDialog
        open={asking} busy={busy} error={error}
        title={`Copy-safety test as ${user}`}
        description={<>Makes a temporary shared drive on the target, copies up to {perType} files of each
          type into it as {user}, waits {settle} minutes, compares, then deletes the drive. Writes nothing
          to the source other than those copies.</>}
        onCancel={() => { setAsking(false); setError(null) }}
        onConfirm={confirm}
      />
    </Paper>
  )
}

export default CopySafetyCard
