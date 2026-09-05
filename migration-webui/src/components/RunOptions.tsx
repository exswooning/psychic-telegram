/**
 * The choices that decide what a run actually does, in front of the button
 * that starts it.
 *
 * All of these were already in the server's run state and readable over GET
 * /api/toggles. Only the console had controls for them, so from this app the
 * most consequential decision in a migration -- who carries the mail -- was
 * invisible, and its default silently won every time.
 */
import React, { useCallback, useEffect, useState } from 'react'
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
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(() => {
    fetchToggles().then((r) => setT(r.toggles)).catch((e) => setErr(String(e)))
  }, [])
  useEffect(load, [load])

  const send = async (patch: Partial<RunToggles>) => {
    setBusy(true); setErr('')
    try {
      // Render what the server came back with, never what was asked for. It
      // refuses some combinations outright (rewriting under DMS) and turns
      // others on for you (rewriting under split), and a control that shows
      // the request rather than the result would be lying in both cases.
      const r = await patchToggles(patch)
      setT(r.toggles)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
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
            size="small" exclusive value={transport} disabled={busy}
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
          <Tooltip title={rewriteBlocked
            ? "DMS never hands the message to this engine, so there is nothing to rewrite"
            : ''}>
            <FormControlLabel
              control={
                <Switch
                  size="small" disabled={busy || rewriteBlocked}
                  checked={!!t.rewrite_drive_links}
                  onChange={(e) => send({ rewrite_drive_links: e.target.checked })}
                  inputProps={{ 'data-testid': 'rewrite-drive-links' } as never}
                />
              }
              label="Rewrite Drive links"
            />
          </Tooltip>

          <TextField
            size="small" type="number" label="Delta window (days)"
            sx={{ width: 180 }} disabled={busy}
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
