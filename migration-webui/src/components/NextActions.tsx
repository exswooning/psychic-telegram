import React, { useEffect, useState } from 'react'
import {
  Card, CardContent, Typography, Stack, Chip, Box, Button, Alert, Skeleton,
} from '@mui/material'
import { useNavigate } from 'react-router-dom'

/**
 * The one question forty-three buttons never answered: what should I do now?
 *
 * Every input already existed -- identity_map status, the audit ledger,
 * id_mapping, the run toggles -- and nothing composed them into a sentence.
 * An operator three days in had to hold the whole model in their head to
 * decide whether "Drive is done and mail has not started" or "those thirty
 * failures are stale" was the true statement.
 *
 * Ordered by what blocks what, and the all-clear is withheld until it is
 * earned: a panel that answers this question with silence reads as
 * "nothing to do", which is the one answer it must never give by accident.
 */
type Level = 'blocked' | 'todo' | 'warn' | 'ok'
interface Item { level: Level; title: string; detail: string; action: string | null }

const TONE: Record<Level, 'error' | 'warning' | 'info' | 'success'> = {
  blocked: 'error', warn: 'warning', todo: 'info', ok: 'success',
}
const WORD: Record<Level, string> = {
  blocked: 'blocked', warn: 'needs attention', todo: 'to do', ok: 'fine',
}

// Where each action actually lives. Naming the action without saying where
// to find it just moves the search rather than ending it.
const HOME: Record<string, { route: string; label: string }> = {
  init_db_auto: { route: '/identities', label: 'Identities' },
  migrate: { route: '/mission-control', label: 'Job control' },
  resolve_dry: { route: '/maintenance', label: 'Maintenance' },
  ui_check: { route: '/verification', label: 'Verification' },
  external_shares_notify: { route: '/services', label: 'Other services' },
}

export default function NextActions() {
  const [items, setItems] = useState<Item[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    let alive = true
    fetch('/api/next')
      .then((r) => r.json())
      .then((d) => {
        if (!alive) return
        if (d.error) setError(d.error)
        setItems(d.items || [])
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => { alive = false }
  }, [])

  return (
    <Card sx={{ mb: 3, border: '1px solid', borderColor: 'divider' }}>
      <CardContent sx={{ p: 3 }}>
        <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>
          What to do next
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Read from the ledger, ordered by what blocks what.
        </Typography>

        {/* An error here is stated, never swallowed -- an empty panel would
            read as "all clear", which is the failure this exists to avoid. */}
        {error && <Alert severity="warning" sx={{ mb: 2 }}>Could not work it out: {error}</Alert>}
        {items === null && !error && <Skeleton variant="rounded" height={72} />}

        <Stack spacing={1.5}>
          {(items || []).map((it, i) => {
            const home = it.action ? HOME[it.action] : undefined
            return (
              <Box
                key={i}
                sx={{
                  display: 'flex', gap: 2, alignItems: 'flex-start',
                  p: 1.5, borderRadius: 1,
                  bgcolor: it.level === 'ok' ? 'transparent' : 'action.hover',
                }}
              >
                <Chip
                  size="small"
                  label={WORD[it.level]}
                  color={TONE[it.level]}
                  variant={it.level === 'ok' ? 'outlined' : 'filled'}
                  sx={{ minWidth: 116, fontWeight: 600 }}
                />
                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    {it.title}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {it.detail}
                  </Typography>
                </Box>
                {home && (
                  <Button size="small" onClick={() => navigate(home.route)}
                          sx={{ flexShrink: 0 }}>
                    {home.label}
                  </Button>
                )}
              </Box>
            )
          })}
        </Stack>
      </CardContent>
    </Card>
  )
}
