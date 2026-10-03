/**
 * Mirror: the target kept as a continuously updated copy of its source (mirror.py).
 *
 * Every cycle reads each service's change feed and carries what changed onto the SAME
 * target items -- new, edited, renamed, moved, sharing, deleted -- then records it here.
 * The rules this page keeps to, the same as every other checking page:
 *   - counts, by kind and by service, never one blended percentage;
 *   - a check that could not be made reads Unknown, in amber, never green -- a pair that
 *     has never finished a good cycle has an Unknown lag, not a small one;
 *   - deletions over the cap are held, and nothing happens to them until a person says so.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Alert, Box, Button, Chip, FormControlLabel, Paper, Radio, RadioGroup, Stack, Switch, Table,
  TableBody, TableCell, TableHead, TableRow, TextField, Typography, MenuItem,
} from '@mui/material'
import { PlayArrow as RunIcon, Refresh as RefreshIcon } from '@mui/icons-material'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'
import {
  decideMirrorDeletions, fetchMe, fetchMirror, runMirrorCycle, saveMirrorSettings, fetchMirrorMigrations, MirrorMigration } from '@/api/controlPlane'
import type { MirrorCycle, MirrorDeletionMode, MirrorView } from '@/api/controlPlane'
import { KINDS, duration } from '@/mirrorKinds'

const STATUS: Record<string, { color: 'success' | 'warning' | 'error' | 'info' | 'default'; label: string }> = {
  ok: { color: 'success', label: 'Good' },
  partial: { color: 'warning', label: 'Partial' },
  failed: { color: 'error', label: 'Failed' },
  stopped: { color: 'default', label: 'Stopped' },
  interrupted: { color: 'warning', label: 'Interrupted' },
  running: { color: 'info', label: 'Running' },
}

const when = (iso: string | null | undefined) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

type Ask = null | 'save' | 'run' | 'apply' | 'keep'

const Mirror: React.FC = () => {
  const [params] = useSearchParams()
  const [accountId, setAccountId] = useState<number | undefined>(Number(params.get('account')) || undefined)
  const [view, setView] = useState<MirrorView | null>(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null)
  const [ask, setAsk] = useState<Ask>(null)
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState<{ enabled: boolean; intervalMin: string; deletionMode: MirrorDeletionMode; capPct: string; users: string[] | null } | null>(null)
  // The migrations this mirror can follow, newest first.
  const [migrations, setMigrations] = useState<MirrorMigration[]>([])

  useEffect(() => {
    if (accountId) return
    fetchMe().then((m) => setAccountId(m.id)).catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])

  const load = useCallback(() => {
    if (!accountId) return
    fetchMirror(accountId).then((v) => { setView(v); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t) }, [load])
  useEffect(() => {
    if (!accountId) return
    Promise.resolve().then(() => fetchMirrorMigrations(accountId))
      .then((r) => setMigrations(r?.migrations ?? [])).catch(() => setMigrations([]))
  }, [accountId])

  const saved = view?.settings
  const form = draft ?? (saved ? {
    enabled: saved.enabled, intervalMin: String(saved.intervalMin),
    deletionMode: saved.deletionMode, capPct: String(saved.capPct), users: saved.users ?? null,
  } : null)
  const minInterval = view?.minIntervalMin ?? 5
  const intervalOk = form !== null && Number(form.intervalMin) >= minInterval
  const capOk = form !== null && Number(form.capPct) > 0 && Number(form.capPct) <= 100
  const dirty = draft !== null

  const last = view?.lastCycle ?? null
  const held = view?.waiting?.held ?? 0
  const lag = view?.lagSeconds
  const byService = useMemo(() => last?.byService ?? {}, [last])
  const services = useMemo(() => Object.keys(byService).sort(), [byService])

  const act = async (reason: string) => {
    if (!ask) return
    setBusy(true)
    try {
      const r = ask === 'save' && form
        ? await saveMirrorSettings(reason, {
          enabled: form.enabled, intervalMin: Number(form.intervalMin),
          deletionMode: form.deletionMode, capPct: Number(form.capPct), users: form.users,
        }, accountId)
        : ask === 'run'
          ? await runMirrorCycle(reason, accountId)
          : await decideMirrorDeletions(reason, ask as 'apply' | 'keep', accountId)
      setNote({ ok: r.ok, text: r.detail || (r.ok ? 'done' : 'refused') })
      if (r.ok) {
        setAsk(null)
        if (ask === 'save') setDraft(null)
        load()
      }
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally { setBusy(false) }
  }

  const lagChip = lag === null || lag === undefined
    ? <Chip size="small" color="warning" label="Lag: Unknown — no good cycle yet" data-testid="lag" />
    : <Chip size="small" color={view?.behind ? 'error' : 'success'} data-testid="lag"
            label={`Lag: ${duration(lag)}${view?.behind ? ' — behind' : ''}`} />

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h5" sx={{ fontWeight: 700, flexGrow: 1 }}>Mirror</Typography>
        <Button size="small" startIcon={<RefreshIcon />} onClick={load}>Refresh</Button>
        <Button size="small" variant="contained" startIcon={<RunIcon />} data-testid="mirror-run"
                disabled={!view || view.running}
                onClick={() => { setNote(null); setAsk('run') }}>
          {view?.running ? 'A cycle is running' : 'Run a cycle now'}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 800 }}>
        {view?.sourceDomain && view?.targetDomain
          ? <><strong>{view.sourceDomain}</strong> → <strong>{view.targetDomain}</strong>. </> : null}
        Every cycle reads what changed on the source since the last one and applies it to the
        same items on the target: new items, edits, renames, moves, sharing and deletions.
        Drive first, then mail and calendar, then the rest. Mail always goes through this tool,
        never the Data Migration Service.
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {note && <Alert severity={note.ok ? 'success' : 'error'} sx={{ mb: 2 }} onClose={() => setNote(null)}>{note.text}</Alert>}

      <Stack direction="row" gap={1} flexWrap="wrap" sx={{ mb: 2 }} data-testid="mirror-state">
        <Chip size="small" color={saved?.enabled ? 'success' : 'default'} data-testid="mirror-on"
              label={saved?.enabled ? `On, every ${saved.intervalMin} min` : 'Off'} />
        {view?.running && <Chip size="small" color="info" label="Cycle running" />}
        {last
          ? <Chip size="small" color={STATUS[last.status]?.color ?? 'default'} data-testid="last-status"
                  label={`Last cycle: ${STATUS[last.status]?.label ?? last.status} · ${when(last.finishedAt ?? last.startedAt)}`} />
          : <Chip size="small" color="warning" data-testid="last-status" label="Last cycle: none yet" />}
        {lagChip}
        <Chip size="small" variant="outlined" data-testid="waiting"
              label={`Waiting: ${view?.waiting?.retry ?? 0} to retry · ${held} deletion(s) held`} />
        {saved?.deletionsPaused && <Chip size="small" color="warning" label="Deletions paused" />}
      </Stack>

      {held > 0 && (
        <Alert severity="warning" sx={{ mb: 2 }} data-testid="held">
          <Typography variant="body2" sx={{ fontWeight: 700 }}>
            {held.toLocaleString()} deletion(s) are waiting for a decision.
          </Typography>
          <Typography variant="body2" sx={{ mb: 1 }}>
            A cycle found more items deleted on the source than this pair&apos;s cap allows, so
            none were applied and deletions are paused. Applying moves each one to the
            target&apos;s bin, recoverable for 30 days. Keeping leaves the target as it is.
          </Typography>
          <Box sx={{ maxHeight: 220, overflow: 'auto', mb: 1 }}>
            <Table size="small">
              <TableHead><TableRow>
                <TableCell>User</TableCell><TableCell>Service</TableCell><TableCell>Item</TableCell><TableCell>Found</TableCell>
              </TableRow></TableHead>
              <TableBody>
                {(view?.held ?? []).map((h) => (
                  <TableRow key={h.id}>
                    <TableCell>{h.sourceUser}</TableCell>
                    <TableCell>{h.service} {h.itemType}</TableCell>
                    <TableCell>{h.name || h.targetId}</TableCell>
                    <TableCell>{when(h.createdAt)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
          <Stack direction="row" spacing={1}>
            <Button size="small" variant="contained" color="warning" data-testid="apply-deletions"
                    onClick={() => { setNote(null); setAsk('apply') }}>Apply these deletions</Button>
            <Button size="small" variant="outlined" data-testid="keep-deletions"
                    onClick={() => { setNote(null); setAsk('keep') }}>Keep them</Button>
          </Stack>
        </Alert>
      )}

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>Settings</Typography>
        {form ? (
          <Stack spacing={1.5} sx={{ maxWidth: 640 }}>
            <FormControlLabel
              control={<Switch checked={form.enabled} inputProps={{ 'aria-label': 'mirror on' }}
                               onChange={(e) => setDraft({ ...form, enabled: e.target.checked })} />}
              label={form.enabled ? 'Mirror is on' : 'Mirror is off'} />
            <TextField select size="small" label="What it follows"
                       value={form.users ? JSON.stringify(form.users) : ''}
                       SelectProps={{ displayEmpty: true }} InputLabelProps={{ shrink: true }}
                       inputProps={{ 'data-testid': 'mirror-follows' }}
                       helperText="One migration's users, kept in step after it; or every user a migration has finished."
                       onChange={(e) => setDraft({ ...form, users: e.target.value ? JSON.parse(e.target.value) : null })}>
              <MenuItem value="">Every migrated user</MenuItem>
              {form.users && !migrations.some((m) => JSON.stringify(m.users) === JSON.stringify(form.users)) && (
                <MenuItem value={JSON.stringify(form.users)}>{form.users.length} chosen user(s)</MenuItem>)}
              {migrations.filter((m) => m.users.length).map((m) => (
                <MenuItem key={m.id} value={JSON.stringify(m.users)} data-testid={`mirror-migration-${m.id}`}>
                  {new Date(m.startedAt).toLocaleString()} · {m.users.length === 1 ? m.users[0] : `${m.users.length} users`} · {m.reason}
                </MenuItem>
              ))}
            </TextField>
            <Stack direction="row" spacing={2}>
              <TextField size="small" type="number" label="Every (minutes)" value={form.intervalMin}
                         error={!intervalOk} helperText={intervalOk ? ' ' : `At least ${minInterval} minutes`}
                         inputProps={{ min: minInterval, 'data-testid': 'mirror-interval' }}
                         onChange={(e) => setDraft({ ...form, intervalMin: e.target.value })} />
              <TextField size="small" type="number" label="Deletion cap (% of mapped items)" value={form.capPct}
                         error={!capOk} helperText={capOk ? 'More in one cycle and none are applied' : 'Above 0, at most 100'}
                         inputProps={{ min: 0.1, max: 100, step: 0.5, 'data-testid': 'mirror-cap' }}
                         onChange={(e) => setDraft({ ...form, capPct: e.target.value })} />
            </Stack>
            <RadioGroup value={form.deletionMode}
                        onChange={(e) => setDraft({ ...form, deletionMode: e.target.value as MirrorDeletionMode })}>
              <FormControlLabel value="mirror" control={<Radio size="small" />}
                label={<Typography variant="body2"><strong>Mirror deletions</strong> — an item deleted on the source goes to the target&apos;s bin (recoverable for 30 days), never deleted outright.</Typography>} />
              <FormControlLabel value="keep" control={<Radio size="small" />}
                label={<Typography variant="body2"><strong>Keep everything</strong> — nothing is ever deleted on the target; it works as a backup.</Typography>} />
            </RadioGroup>
            <Stack direction="row" spacing={1}>
              <Button size="small" variant="contained" disabled={!dirty || !intervalOk || !capOk}
                      data-testid="mirror-save" onClick={() => { setNote(null); setAsk('save') }}>Save</Button>
              {dirty && <Button size="small" onClick={() => setDraft(null)}>Discard changes</Button>}
            </Stack>
          </Stack>
        ) : <Typography variant="body2" color="text.secondary">Loading…</Typography>}
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
          Changes applied in the last cycle
        </Typography>
        {!last ? (
          <Typography variant="body2" color="text.secondary">No cycle has run for this pair yet.</Typography>
        ) : (
          <>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Started {when(last.startedAt)}{last.finishedAt ? `, finished ${when(last.finishedAt)}` : ''}
              {typeof last.calls === 'number' ? ` · ${last.calls.toLocaleString()} Google calls` : ''}
              {` · ${last.deletionsApplied} deletion(s) applied, ${last.deletionsHeld} held · ${last.conflicts} conflict(s)`}
            </Typography>
            <Box sx={{ overflowX: 'auto' }}>
              <Table size="small" data-testid="kinds">
                <TableHead><TableRow>
                  <TableCell>Service</TableCell>
                  {KINDS.map((k) => <TableCell key={k.key} align="right">{k.label}</TableCell>)}
                </TableRow></TableHead>
                <TableBody>
                  {services.map((s) => (
                    <TableRow key={s} data-testid={`kinds-${s}`}>
                      <TableCell>{s}</TableCell>
                      {KINDS.map((k) => (
                        <TableCell key={k.key} align="right">{(byService[s]?.[k.key] ?? 0).toLocaleString()}</TableCell>
                      ))}
                    </TableRow>
                  ))}
                  {services.length === 0 && (
                    <TableRow><TableCell colSpan={KINDS.length + 1}>
                      <Typography variant="body2" color="text.secondary">Nothing changed on the source.</Typography>
                    </TableCell></TableRow>
                  )}
                </TableBody>
              </Table>
            </Box>
            {last.unknown.length > 0 && (
              <Alert severity="warning" sx={{ mt: 1.5 }} data-testid="unknown">
                <Typography variant="body2" sx={{ fontWeight: 700 }}>Not checked in this cycle (Unknown, not a pass):</Typography>
                {last.unknown.map((u, i) => <Typography key={i} variant="body2">{u}</Typography>)}
              </Alert>
            )}
            {last.errors.length > 0 && (
              <Alert severity="error" sx={{ mt: 1.5 }}>
                <Typography variant="body2" sx={{ fontWeight: 700 }}>
                  {last.errors.length} item(s) failed and will be tried again next cycle:
                </Typography>
                {last.errors.slice(0, 10).map((e, i) => (
                  <Typography key={i} variant="body2" sx={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>{e}</Typography>
                ))}
              </Alert>
            )}
            {(last.users?.new?.length || last.users?.suspended?.length || last.users?.gone?.length) ? (
              <Alert severity="info" sx={{ mt: 1.5 }} data-testid="users">
                {last.users.new?.length ? <Typography variant="body2">New on the source, given an account and a full first run: {last.users.new.join(', ')}</Typography> : null}
                {last.users.suspended?.length ? <Typography variant="body2">Suspended on the source (target account left as it is): {last.users.suspended.join(', ')}</Typography> : null}
                {last.users.gone?.length ? <Typography variant="body2">Gone from the source (target account never deleted automatically): {last.users.gone.join(', ')}</Typography> : null}
              </Alert>
            ) : null}
          </>
        )}
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
          Conflicts ({(view?.conflictCount ?? 0).toLocaleString()})
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          An item someone changed on the target since Bitport last wrote it. The source wins:
          the source&apos;s version is put back, and the conflict is recorded here.
        </Typography>
        {(view?.conflicts ?? []).length === 0
          ? <Typography variant="body2" color="text.secondary">None.</Typography>
          : (
            <Table size="small" data-testid="conflicts">
              <TableHead><TableRow>
                <TableCell>When</TableCell><TableCell>User</TableCell><TableCell>Item</TableCell><TableCell>What happened</TableCell>
              </TableRow></TableHead>
              <TableBody>
                {(view?.conflicts ?? []).map((c, i) => (
                  <TableRow key={i}>
                    <TableCell>{when(c.at)}</TableCell><TableCell>{c.sourceUser}</TableCell>
                    <TableCell>{c.name || c.itemId}</TableCell><TableCell>{c.detail}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>Recent cycles</Typography>
        <Table size="small" data-testid="cycles">
          <TableHead><TableRow>
            <TableCell>Started</TableCell><TableCell>Result</TableCell><TableCell align="right">Changes</TableCell>
            <TableCell align="right">Deleted</TableCell><TableCell align="right">Held</TableCell>
            <TableCell align="right">Conflicts</TableCell><TableCell align="right">Calls</TableCell>
          </TableRow></TableHead>
          <TableBody>
            {(view?.cycles ?? []).map((c: MirrorCycle) => (
              <TableRow key={c.id}>
                <TableCell>{when(c.startedAt)}</TableCell>
                <TableCell><Chip size="small" color={STATUS[c.status]?.color ?? 'default'} label={STATUS[c.status]?.label ?? c.status} /></TableCell>
                <TableCell align="right">
                  {KINDS.filter((k) => c.counts[k.key]).map((k) => `${c.counts[k.key]} ${k.label.toLowerCase()}`).join(', ') || '—'}
                </TableCell>
                <TableCell align="right">{c.deletionsApplied}</TableCell>
                <TableCell align="right">{c.deletionsHeld}</TableCell>
                <TableCell align="right">{c.conflicts}</TableCell>
                <TableCell align="right">{typeof c.calls === 'number' ? c.calls.toLocaleString() : '—'}</TableCell>
              </TableRow>
            ))}
            {(view?.cycles ?? []).length === 0 && (
              <TableRow><TableCell colSpan={7}>
                <Typography variant="body2" color="text.secondary">None yet.</Typography>
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>What cannot be mirrored</Typography>
        {(view?.cannotMirror ?? []).map((line, i) => (
          <Typography key={i} variant="body2" sx={{ mb: 0.5 }}>• {line}</Typography>
        ))}
      </Paper>

      <ReasonCodeDialog
        open={ask !== null} busy={busy}
        title={ask === 'save' ? 'Save mirror settings' : ask === 'run' ? 'Run a mirror cycle now'
          : ask === 'apply' ? `Apply ${held} held deletion(s)` : `Keep ${held} held deletion(s)`}
        destructive={ask === 'apply'}
        description={
          ask === 'save'
            ? <>{form?.enabled
              ? `The mirror will run every ${form?.intervalMin} minutes, ${form?.deletionMode === 'keep' ? 'never deleting anything on the target' : `moving deleted items to the target's bin, holding any cycle that would delete more than ${form?.capPct}% of mapped items`}.`
              : 'No further cycles will start. The target keeps everything it has.'}</>
            : ask === 'run'
              ? <>Starts one cycle now, as the job &quot;mirror&quot;. It queues behind other jobs like any
                  other and can be stopped from the Jobs page.</>
              : ask === 'apply'
                ? <>Moves each of the {held} held item(s) to the target&apos;s bin, where it can be
                    restored for 30 days. Deletions for this pair resume afterwards.</>
                : <>Leaves the {held} item(s) on the target. They are not deleted now or later.
                    Deletions for this pair resume afterwards.</>
        }
        onCancel={() => setAsk(null)} onConfirm={act} />
    </Box>
  )
}

export default Mirror
