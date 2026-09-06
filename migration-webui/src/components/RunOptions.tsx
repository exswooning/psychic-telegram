/**
 * The choices that decide what a run actually does, in front of the button
 * that starts it.
 *
 * All of these were already in the server's run state and readable over GET
 * /api/toggles. Only the console had controls for them, so from this app the
 * most consequential decision in a migration -- who carries the mail -- was
 * invisible, and its default silently won every time.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Box, FormControlLabel, Paper, Stack, Switch, TextField,
  ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material'
import { fetchToggles, patchToggles } from '@/api/client'
import type { MailTransport, RunToggles } from '@/api/client'

/** Consequence, not restatement. An operator picking a transport is really
 *  choosing between speed and whether Drive links in migrated mail survive
 *  the source tenant being deleted. */
const TRANSPORTS: { value: MailTransport; label: string; blurb: string }[] = [
  {
    value: 'engine',
    label: 'This engine',
    blurb: 'Every message passes through here, so Drive links inside them can '
      + 'be rewritten to point at the target. Slowest: Gmail caps inserts at '
      + '3/sec per account.',
  },
  {
    value: 'dms',
    label: "Google's DMS",
    blurb: 'Google copies mail server-to-server. Much faster, and nothing can '
      + 'be rewritten on the way through -- this engine never holds the '
      + 'message. Links keep pointing at the source tenant.',
  },
  {
    value: 'split',
    label: 'Split',
    blurb: 'This engine carries only the mail that contains a Drive link and '
      + 'rewrites it; DMS carries the rest and skips what was already moved, '
      + 'because it dedupes on Message-ID. Run the engine pass first.',
  },
]

export const RunOptions: React.FC = () => {
  const [t, setT] = useState<RunToggles | null>(null)
  const [err, setErr] = useState('')

  const load = useCallback(() => {
    fetchToggles().then((r) => setT(r.toggles)).catch((e) => setErr(String(e)))
  }, [])
  useEffect(load, [load])

  // Responses can land out of order, and each one replaces the whole toggle
  // state. Typing a user list and then flipping a switch 2s later had the
  // slower first response arrive last and silently undo the switch -- the
  // control snapped back and the run went out with the old setting.
  //
  // It also used to disable every control while any request was in flight.
  // The user list is debounced, so its request fires a second or two after
  // typing stops -- landing on whatever switch you reached for next, which
  // then ignored the click in silence. Nothing said why, and the run went
  // out with the old setting. Ordering is handled here instead, so no
  // control has to be dead while another one saves.
  const seq = useRef(0)

  const send = async (patch: Partial<RunToggles>) => {
    const mine = ++seq.current
    setErr('')
    try {
      // Render what the server came back with, never what was asked for. It
      // refuses some combinations outright (rewriting under DMS) and turns
      // others on for you (rewriting under split), and a control that shows
      // the request rather than the result would be lying in both cases.
      const r = await patchToggles(patch)
      if (mine === seq.current) setT(r.toggles)
    } catch (e) {
      if (mine === seq.current) setErr(String(e))
    }
  }

  if (!t) return null
  const transport: MailTransport = t.mail_transport ?? 'engine'
  const chosen = TRANSPORTS.find((x) => x.value === transport)
  const rewriteBlocked = transport === 'dms'

  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }} data-testid="run-options">
      <Typography variant="h6" gutterBottom>Before you run</Typography>

      <Stack spacing={2}>
        <Box>
          <Typography variant="body2" color="text.secondary" gutterBottom>
            Mail carried by
          </Typography>
          <ToggleButtonGroup
            size="small" exclusive value={transport}
            onChange={(_, v: MailTransport | null) => v && send({ mail_transport: v })}
            data-testid="mail-transport"
          >
            {TRANSPORTS.map((x) => (
              <ToggleButton key={x.value} value={x.value}
                            data-testid={`transport-${x.value}`}
                            sx={{ textTransform: 'none' }}>
                {x.label}
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
          {chosen && (
            <Typography variant="caption" color="text.secondary"
                        sx={{ display: 'block', mt: 1, maxWidth: '60ch' }}>
              {chosen.blurb}
            </Typography>
          )}
        </Box>

        <Stack direction="row" spacing={3} flexWrap="wrap" alignItems="center">
          {/* Not a duplicate of Job control's "Dry run" button, which starts
              one run dry. This is the standing flag every action launched
              from this app reads, and until now the app could neither show
              nor change it -- so a flag set once on the console made every
              later Migrate silently do nothing while reporting success. */}
          <FormControlLabel
            control={
              <Switch
                size="small" checked={!!t.dry_run}
                onChange={(e) => send({ dry_run: e.target.checked })}
                inputProps={{ 'data-testid': 'dry-run' } as never}
              />
            }
            label="Dry run everything"
          />

          <Tooltip title={rewriteBlocked
            ? "DMS never hands the message to this engine, so there is nothing to rewrite"
            : ''}>
            <FormControlLabel
              control={
                <Switch
                  size="small" disabled={rewriteBlocked}
                  checked={!!t.rewrite_drive_links}
                  onChange={(e) => send({ rewrite_drive_links: e.target.checked })}
                  inputProps={{ 'data-testid': 'rewrite-drive-links' } as never}
                />
              }
              label="Rewrite Drive links"
            />
          </Tooltip>

          {/* Most useful on the delta pass: the bulk migration is where mail
              gets copied before anyone turns rewriting on, and delta is what
              you run afterwards. */}
          <Tooltip title={t.rewrite_drive_links
            ? 'Trashes the old copy on the target and inserts a corrected one. Only messages a rewrite would change are touched.'
            : 'Needs Drive-link rewriting on -- otherwise it would replace each message with an identical copy'}>
            <FormControlLabel
              control={
                <Switch
                  size="small" disabled={!t.rewrite_drive_links}
                  checked={!!t.redo_unrewritten_links}
                  onChange={(e) => send({ redo_unrewritten_links: e.target.checked })}
                  inputProps={{ 'data-testid': 'redo-unrewritten' } as never}
                />
              }
              label="Redo mail whose links were never rewritten"
            />
          </Tooltip>

          {/* _RUN_STATE["users"] scopes anything webui launches -- the
              phased actions and the delta pass. Job control's row ticks
              are a DIFFERENT scope: they go to api_server's migrate/start
              and never reach this. Two scoping mechanisms, and only one of
              them had a control, which is how a settings audit pointed at
              the wrong one and still passed. */}
          <TextField
            size="small" label="Only these users"
            sx={{ width: 300 }}
            defaultValue={t.users ?? ''}
            placeholder="blank = every user"
            inputProps={{ 'data-testid': 'run-users' }}
            onBlur={(e) => {
              if (e.target.value.trim() !== (t.users ?? '').trim()) {
                send({ users: e.target.value.trim() })
              }
            }}
          />

          <TextField
            size="small" type="number" label="Delta window (days)"
            sx={{ width: 180 }}
            defaultValue={t.delta_days ?? 2}
            inputProps={{ min: 1, 'data-testid': 'delta-days' }}
            onBlur={(e) => {
              const v = parseInt(e.target.value, 10)
              if (v > 0 && v !== t.delta_days) send({ delta_days: v })
            }}
          />
        </Stack>

        {t.last_note && (
          <Alert severity="info" data-testid="run-options-note">{t.last_note}</Alert>
        )}
        {err && <Alert severity="error">{err}</Alert>}
      </Stack>
    </Paper>
  )
}

export default RunOptions
