/**
 * The source guard (source_guard.py): the source's counts taken before anything moved,
 * compared every 10 minutes while the migration is open, and Google's own audit log read
 * for anything this tool's addresses changed. Read-only on the source.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, Chip, Collapse, Paper, Stack, Typography } from '@mui/material'
import ReasonCodeDialog from './ReasonCodeDialog'
import { fetchSourceGuard, runSourceGuard } from '@/api/controlPlane'
import type { SourceGuard } from '@/api/controlPlane'

const VERDICT = {
  CLEAN: { color: 'success' as const, label: 'Unchanged' },
  CHANGED: { color: 'error' as const, label: 'Changed' },
  INCOMPLETE: { color: 'warning' as const, label: 'Could not check all of it' },
  NO_SNAPSHOT: { color: 'default' as const, label: 'No baseline' },
}

const when = (s?: string | null) => (s ? new Date(s).toLocaleString() : '—')

const SourceGuardPanel: React.FC<{ accountId?: number; sourceDomain?: string }> = ({ accountId, sourceDomain }) => {
  const [g, setG] = useState<SourceGuard | null>(null)
  const [open, setOpen] = useState(false)
  const [asking, setAsking] = useState<'check' | 'baseline' | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null)
  const load = useCallback(() => {
    fetchSourceGuard(accountId).then(setG).catch(() => setG(null))
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [load])
  if (!g) return null

  const r = g.latest
  const v = !g.snapshotTakenAt ? VERDICT.NO_SNAPSHOT : r ? VERDICT[r.verdict] : null
  const mine = r?.auditLog.byBitport ?? []
  const others = r?.auditLog.deletionsByOthers ?? []
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="source-guard">
      <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>Source guard</Typography>
        {v && <Chip size="small" color={v.color} label={v.label} data-testid="source-guard-verdict" />}
        {r && mine.length > 0 && <Chip size="small" color="error" label={`Bitport changed ${mine.length}`} />}
        <Box sx={{ flexGrow: 1 }} />
        <Button size="small" onClick={() => { setNote(null); setAsking('check') }} disabled={!g.snapshotTakenAt}>
          Check now
        </Button>
        <Button size="small" onClick={() => { setNote(null); setAsking('baseline') }}>
          {g.snapshotTakenAt ? 'Retake baseline' : 'Take baseline'}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, maxWidth: 820 }}>
        {g.snapshotTakenAt
          ? `${sourceDomain || 'The source'} was counted ${when(g.snapshotTakenAt)} (${g.snapshotUsers} users): every Drive file, `
            + 'mail, calendar and contacts. Every 10 minutes it is compared again and Google\'s own audit log is read '
            + 'for anything this tool changed. '
          : 'No baseline yet: one is taken when a migration starts, before anything moves. '}
        {r ? `Last checked ${when(r.checkedAt)}.` : ''}
      </Typography>
      {note && <Alert severity={note.ok ? 'success' : 'error'} sx={{ mt: 1 }} onClose={() => setNote(null)}>{note.text}</Alert>}
      {r && (r.changeCount > 0 || mine.length > 0 || others.length > 0 || r.unread.length > 0 || r.auditLog.error) && (
        <>
          <Button size="small" sx={{ mt: 1, px: 0 }} onClick={() => setOpen(!open)} data-testid="source-guard-details">
            {open ? 'Hide details' : `Show details (${r.changeCount} difference(s) since the baseline)`}
          </Button>
          <Collapse in={open} unmountOnExit>
            <Box sx={{ mt: 1 }}>
              {mine.length > 0 && (
                <Alert severity="error" sx={{ mb: 1 }}>
                  Google's audit log shows {mine.length} change(s) made from this tool's own addresses:
                  {mine.slice(0, 10).map((x) => (
                    <Typography key={`${x.at}${x.id}${x.event}`} variant="caption" sx={{ display: 'block' }}>
                      {when(x.at)} · {x.event} · {x.item} ({x.actor})
                    </Typography>
                  ))}
                </Alert>
              )}
              {r.auditLog.error && <Alert severity="warning" sx={{ mb: 1 }}>Audit log not read: {r.auditLog.error}</Alert>}
              {r.unread.map((u) => (
                <Typography key={u.user} variant="caption" color="warning.main" sx={{ display: 'block' }}>
                  {u.user}: could not read {Object.keys(u.errors).join(', ')}
                </Typography>
              ))}
              {r.changes.slice(0, 50).map((c, i) => (
                <Typography key={`${c.user}${c.id}${i}`} variant="caption" sx={{ display: 'block', overflowWrap: 'anywhere' }}>
                  {c.user} · {c.what}{c.name ? ` · ${c.name}` : ''}{c.from !== undefined ? ` · ${String(c.from)} → ${String(c.to)}` : ''}
                </Typography>
              ))}
              {others.length > 0 && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                  Deleted or binned by people (not this tool): {others.slice(0, 10).map((x) => `${x.item} (${x.actor})`).join('; ')}
                </Typography>
              )}
            </Box>
          </Collapse>
        </>
      )}
      <ReasonCodeDialog
        open={!!asking} busy={busy}
        title={asking === 'check' ? 'Check the source now' : 'Retake the baseline'}
        description={asking === 'check'
          ? 'Reads the source and Google\'s audit log and compares with the baseline. Writes nothing to the source.'
          : 'Counts the source again and makes that the baseline every later check compares with. Writes nothing to the source.'}
        onCancel={() => setAsking(null)}
        onConfirm={async (reason) => {
          if (!asking) return
          setBusy(true)
          try {
            const res = await runSourceGuard(asking, reason, accountId)
            setNote({ ok: res.ok, text: res.detail })
            if (res.ok) setAsking(null)
          } catch (e) {
            setNote({ ok: false, text: e instanceof Error ? e.message : String(e) })
          } finally { setBusy(false) }
        }} />
    </Paper>
  )
}

export default SourceGuardPanel
