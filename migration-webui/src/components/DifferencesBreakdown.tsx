/**
 * Where one pair's one-to-one differences come from: one bar per kind of finding
 * (known_issues.py; open and fixed, coloured by service) and, per service, how many
 * differ / are missing / were not copied / could not be checked. A bar is a button:
 * it shows that kind's cause, its fix and the users it affects. Plain MUI bars, not a
 * chart library -- each bar has to be a real, keyboard-reachable control with its
 * exact count on it.
 */
import React, { useMemo, useState } from 'react'
import { Alert, Box, ButtonBase, Chip, Stack, Typography } from '@mui/material'
import { alpha } from '@mui/material/styles'
import type { Theme } from '@mui/material/styles'
import { fetchKnownIssueUsers } from '@/api/controlPlane'
import type { KnownIssueKind, KnownIssueUser, OneToOneUser } from '@/api/controlPlane'

type Palette = 'primary' | 'error' | 'info' | 'success' | 'warning' | 'secondary'
const SERVICE_COLOUR: Record<string, Palette> = {
  drive: 'primary', gmail: 'error', calendar: 'info', contacts: 'success',
  tasks: 'warning', chat: 'secondary', directory: 'secondary',
}
const colour = (svc: string) => SERVICE_COLOUR[svc] ?? 'secondary'
const SERVICE_ORDER = ['gmail', 'drive', 'tasks', 'calendar', 'contacts', 'chat', 'directory']
const PARTS = [
  { key: 'differences', label: 'differ' },
  { key: 'missing', label: 'missing' },
  { key: 'notCopied', label: 'copy or access findings' },
  { key: 'errors', label: "couldn't check" },
] as const
const n = (x: number) => x.toLocaleString()

const KindBar: React.FC<{ k: KnownIssueKind; max: number; selected: boolean; onSelect: () => void }> = ({ k, max, selected, onSelect }) => {
  const total = k.open + k.fixed
  const errs = k.openByCategory?.errors ?? 0
  return (
    <ButtonBase onClick={onSelect} focusRipple aria-pressed={selected} data-testid={`kind-bar-${k.id}`}
                sx={{ display: 'block', width: '100%', textAlign: 'left', borderRadius: 1, p: 0.75,
                      outline: selected ? 2 : 0, outlineColor: 'primary.main', outlineStyle: 'solid' }}>
      <Stack direction="row" spacing={1} alignItems="baseline" sx={{ mb: 0.5 }}>
        <Typography variant="body2" sx={{ flexGrow: 1 }}>{k.title}</Typography>
        <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}
                    data-testid={`kind-count-${k.id}`}>
          {n(k.open)} open · {n(k.fixed)} fixed{errs ? ` · ${n(errs)} couldn't check` : ''}
        </Typography>
      </Stack>
      <Box sx={{ display: 'flex', height: 10, width: `${Math.max(2, (total / max) * 100)}%`, borderRadius: 0.5, overflow: 'hidden' }}>
        <Box sx={{ flex: k.open, bgcolor: `${colour(k.service)}.main` }} />
        <Box sx={{ flex: k.fixed, bgcolor: (t: Theme) => alpha(t.palette[colour(k.service)].main, 0.35) }} />
      </Box>
    </ButtonBase>
  )
}

const DifferencesBreakdown: React.FC<{ accountId: number; kinds: KnownIssueKind[]; users: OneToOneUser[] }> = ({ accountId, kinds, users }) => {
  const [picked, setPicked] = useState<KnownIssueKind | null>(null)
  const [who, setWho] = useState<KnownIssueUser[] | null>(null)
  const seen = useMemo(() => kinds.filter((k) => k.open > 0 || k.fixed > 0)
    .sort((a, b) => (b.open + b.fixed) - (a.open + a.fixed) || a.title.localeCompare(b.title)), [kinds])
  const byService = useMemo(() => SERVICE_ORDER.map((svc) => {
    const rows = users.flatMap((u) => u.services.filter((s) => s.service === svc))
    return { svc, ...Object.fromEntries(PARTS.map((p) => [p.key, rows.reduce((t, s) => t + (s.counts?.[p.key] ?? 0), 0)])) } as Record<string, number> & { svc: string }
  }).filter((r) => PARTS.some((p) => r[p.key] > 0)), [users])
  const maxKind = Math.max(1, ...seen.map((k) => k.open + k.fixed))
  const maxSvc = Math.max(1, ...byService.map((r) => PARTS.reduce((t, p) => t + r[p.key], 0)))
  const sampled = users.some((u) => u.services.some((s) => s.sampledOf != null))
  const legacy = users.some((u) => u.services.some((s) => (s.scopeVersion ?? 0) < 2))

  const pick = (k: KnownIssueKind) => {
    setPicked(k); setWho(null)
    fetchKnownIssueUsers(k.id, accountId).then((r) => setWho(r.users)).catch(() => setWho([]))
  }

  return (
    <Box data-testid="differences-breakdown">
      {(sampled || legacy) && (
        <Alert severity="info" sx={{ mb: 1.5 }}>
          {sampled && 'Some results come from a sample, not every item. '}
          {legacy && 'Some rows were checked before kinds of finding were recorded; they were classified afterwards from their stored detail and read as incomplete until checked again.'}
        </Alert>
      )}

      <Typography variant="subtitle2" sx={{ mb: 0.5 }}>By service</Typography>
      <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
        {PARTS.map((p, i) => (
          <Stack key={p.key} direction="row" spacing={0.5} alignItems="center">
            <Box sx={{ width: 10, height: 10, borderRadius: 0.5, bgcolor: (t: Theme) => alpha(t.palette.text.primary, 0.85 - i * 0.2) }} />
            <Typography variant="caption">{p.label}</Typography>
          </Stack>
        ))}
      </Stack>
      {byService.length === 0 && <Typography variant="body2" color="text.secondary">No differences recorded for any service.</Typography>}
      {byService.map((r) => {
        const total = PARTS.reduce((t, p) => t + r[p.key], 0)
        return (
          <Box key={r.svc} sx={{ mb: 1 }} data-testid={`svc-bar-${r.svc}`}>
            <Stack direction="row" spacing={1} alignItems="baseline">
              <Chip size="small" color={colour(r.svc)} variant="outlined" label={r.svc} />
              <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {PARTS.map((p) => `${n(r[p.key])} ${p.label}`).join(' · ')}
              </Typography>
            </Stack>
            <Box sx={{ display: 'flex', height: 10, mt: 0.5, width: `${Math.max(2, (total / maxSvc) * 100)}%`, borderRadius: 0.5, overflow: 'hidden' }}>
              {PARTS.map((p, i) => (
                <Box key={p.key} sx={{ flex: r[p.key], bgcolor: (t: Theme) => alpha(t.palette[colour(r.svc)].main, 1 - i * 0.22) }} />
              ))}
            </Box>
          </Box>
        )
      })}

      <Typography variant="subtitle2" sx={{ mt: 2, mb: 0.5 }}>By kind of finding</Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
        Solid: open. Pale: fixed (counted only when a fresh, complete check no longer finds it). Colour: service.
      </Typography>
      {seen.length === 0 && <Typography variant="body2" color="text.secondary">No findings recorded yet.</Typography>}
      <Stack spacing={0.5}>
        {seen.map((k) => <KindBar key={k.id} k={k} max={maxKind} selected={picked?.id === k.id} onSelect={() => pick(k)} />)}
      </Stack>

      {picked && (
        <Box sx={{ mt: 1.5, p: 1.5, borderRadius: 1, border: '1px solid', borderColor: 'divider' }} data-testid="kind-detail">
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
            <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>{picked.title}</Typography>
            <Chip size="small" variant="outlined" color={picked.auto ? 'success' : 'warning'}
                  label={picked.auto ? 'Fixed automatically' : 'Needs a person'} />
          </Stack>
          <Typography variant="body2"><strong>Why:</strong> {picked.cause || '—'}</Typography>
          <Typography variant="body2" sx={{ mt: 0.5 }}><strong>How it is fixed:</strong> {picked.fix}</Typography>
          <Typography variant="body2" sx={{ mt: 1 }}><strong>Users affected:</strong></Typography>
          {who === null ? <Typography variant="caption" color="text.secondary">Loading…</Typography> : (
            who.length === 0 ? <Typography variant="caption" color="text.secondary">None recorded.</Typography> : (
              <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
                {who.map((u) => (
                  <li key={u.user} data-testid={`kind-user-${u.user}`}>
                    <Typography variant="caption" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                      {u.user}: {n(u.open)} open · {n(u.fixed)} fixed
                    </Typography>
                  </li>
                ))}
              </Box>
            ))}
        </Box>
      )}
    </Box>
  )
}

export default DifferencesBreakdown
