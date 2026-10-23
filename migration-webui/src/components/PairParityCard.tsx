/**
 * One card per migration pair (source -> target): every user's one-to-one parity and
 * everything the verification has accumulated -- items compared and matched per
 * service, what differed, what could not be checked, the per-user tally, the fixes
 * recorded per kind of issue, and the source guard. Read from the same endpoints as the
 * One-to-one, Tally and source-guard views; nothing here is computed from a sample
 * without saying so.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Box, Button, Card, CardContent, Chip, Collapse, IconButton, Stack, Table, TableBody, TableCell, TableHead,
  TableRow, Typography,
} from '@mui/material'
import { Refresh as RefreshIcon } from '@mui/icons-material'
import {
  fetchKnownIssues, fetchMe, fetchMigrations, fetchOneToOne, fetchSourceGuard, fetchTally, migrationOf,
} from '@/api/controlPlane'
import type { KnownIssues, OneToOneUser, OneToOneView, SourceGuard, TallyView } from '@/api/controlPlane'
import { VERDICT, ORDER, parity } from '@/oneToOneVerdicts'
import DifferencesBreakdown from './DifferencesBreakdown'

const SERVICES = ['drive', 'gmail', 'calendar', 'contacts', 'tasks', 'chat', 'directory']
const COUNT_KEYS = ['differences', 'missing', 'notCopied', 'errors'] as const

const sum = (users: OneToOneUser[], pick: (s: OneToOneUser['services'][number]) => number) =>
  users.reduce((n, u) => n + u.services.reduce((m, s) => m + (pick(s) || 0), 0), 0)

const PairParityCard: React.FC<{ accountId?: number }> = ({ accountId: given }) => {
  const navigate = useNavigate()
  const [accountId, setAccountId] = useState<number | undefined>(given)
  const [view, setView] = useState<OneToOneView | null>(null)
  const [tally, setTally] = useState<TallyView | null>(null)
  const [issues, setIssues] = useState<KnownIssues | null>(null)
  const [guard, setGuard] = useState<SourceGuard | null>(null)
  const [pair, setPair] = useState<{ source: string; target: string } | null>(null)
  const [showWhere, setShowWhere] = useState(false)

  useEffect(() => {
    if (accountId) return
    fetchMe().then((m) => setAccountId(migrationOf(m))).catch(() => {})
  }, [accountId])

  const load = useCallback(() => {
    if (!accountId) return
    fetchOneToOne(accountId).then(setView).catch(() => setView(null))
    fetchTally(accountId).then(setTally).catch(() => setTally(null))
    fetchKnownIssues(accountId).then(setIssues).catch(() => setIssues(null))
    fetchSourceGuard(accountId).then(setGuard).catch(() => setGuard(null))
    // The pair's own domains, as configured -- never inferred or hard-coded.
    fetchMigrations().then((r) => {
      const row = r.migrations.find((m) => m.accountId === accountId)
      if (row) setPair({ source: row.sourceDomain, target: row.targetDomain })
    }).catch(() => {})
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [load])

  const users = useMemo(() => [...(view?.users ?? [])].sort((a, b) =>
    ORDER.indexOf(a.verdict) - ORDER.indexOf(b.verdict) || a.user.localeCompare(b.user)), [view])
  if (!view || !users.length) return null

  const tallyOf = new Map((tally?.users ?? []).map((t) => [t.user, t]))
  const source = pair?.source || users[0].user.split('@')[1]
  const target = pair?.target || (users.find((u) => u.target)?.target ?? '').split('@')[1] || '—'
  const checked = sum(users, (s) => s.checked)
  const identical = sum(users, (s) => s.identical)
  const sampled = users.some((u) => u.services.some((s) => s.sampledOf != null))
  const perService = SERVICES.map((svc) => {
    const rows = users.flatMap((u) => u.services.filter((s) => s.service === svc))
    return {
      svc, users: rows.length,
      checked: rows.reduce((n, s) => n + (s.checked || 0), 0),
      identical: rows.reduce((n, s) => n + (s.identical || 0), 0),
      ...Object.fromEntries(COUNT_KEYS.map((k) => [k, rows.reduce((n, s) => n + (s.counts?.[k] || 0), 0)])),
    } as Record<string, number | string>
  }).filter((r) => (r.users as number) > 0)
  const fixed = (issues?.kinds ?? []).reduce((n, k) => n + k.fixed, 0)
  const openFindings = (issues?.kinds ?? []).reduce((n, k) => n + k.open, 0)
  const guardVerdict = guard?.latest?.verdict ?? (guard?.snapshotTakenAt ? 'not checked yet' : 'no baseline')

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}
          data-testid="pair-parity">
      <CardContent sx={{ p: 3 }}>
        <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
          <Typography variant="h6" sx={{ fontWeight: 700, flexGrow: 1 }} data-testid="pair-title">
            {source} → {target}
          </Typography>
          <Button size="small" variant={showWhere ? 'contained' : 'outlined'} onClick={() => setShowWhere(!showWhere)}
                  aria-expanded={showWhere} data-testid="where-differences">
            Where the differences come from
          </Button>
          <Button size="small" onClick={() => navigate(`/one-to-one?account=${accountId}`)}>Open one-to-one</Button>
          <IconButton size="small" onClick={load} aria-label="refresh pair parity"><RefreshIcon fontSize="small" /></IconButton>
        </Stack>

        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 1.5 }}>
          {ORDER.map((v) => (view.totals[v] ? (
            <Chip key={v} size="small" color={VERDICT[v].color} data-testid={`pair-total-${v}`}
                  label={`${view.totals[v]} ${VERDICT[v].label.toLowerCase()}`} />) : null))}
          <Chip size="small" variant="outlined" data-testid="pair-items"
                label={`${identical.toLocaleString()} of ${checked.toLocaleString()} items identical (${parity(identical, checked)})`} />
          <Chip size="small" variant="outlined" label={`source guard: ${guardVerdict}`}
                color={guard?.latest?.verdict === 'CLEAN' ? 'success' : guard?.latest?.verdict === 'CHANGED' ? 'error' : 'default'} />
          {issues && <Chip size="small" variant="outlined" label={`${fixed.toLocaleString()} findings fixed, ${openFindings.toLocaleString()} open`} />}
          {tally && <Chip size="small" variant="outlined"
                          label={`tally: ${tally.totals.COMPLETE ?? 0} of ${tally.users.length} users complete`} />}
        </Stack>
        <Collapse in={showWhere} unmountOnExit>
          {accountId && (
            <Box sx={{ mb: 2, p: 2, borderRadius: 1, bgcolor: 'action.hover' }}>
              <DifferencesBreakdown accountId={accountId} kinds={issues?.kinds ?? []} users={users} />
            </Box>
          )}
        </Collapse>
        {sampled && (
          <Typography variant="body2" color="warning.main" sx={{ mb: 1 }}>
            Some results come from a sample, not every item: a sample can never read as 1:1.
          </Typography>
        )}

        <Typography variant="subtitle2" sx={{ mt: 1 }}>What the verification has compared</Typography>
        <Box sx={{ overflowX: 'auto', mb: 2 }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Service</TableCell><TableCell align="right">Users</TableCell>
                <TableCell align="right">Compared</TableCell><TableCell align="right">Identical</TableCell>
                <TableCell align="right">Parity</TableCell><TableCell align="right">Differ</TableCell>
                <TableCell align="right">Missing</TableCell><TableCell align="right">Not copied</TableCell>
                <TableCell align="right">Couldn&apos;t check</TableCell>
              </TableRow>
            </TableHead>
            <TableBody sx={{ '& td': { fontVariantNumeric: 'tabular-nums' } }}>
              {perService.map((r) => (
                <TableRow key={r.svc as string} data-testid={`pair-svc-${r.svc}`}>
                  <TableCell>{r.svc}</TableCell>
                  <TableCell align="right">{r.users}</TableCell>
                  <TableCell align="right">{(r.checked as number).toLocaleString()}</TableCell>
                  <TableCell align="right">{(r.identical as number).toLocaleString()}</TableCell>
                  <TableCell align="right">{parity(r.identical as number, r.checked as number)}</TableCell>
                  {COUNT_KEYS.map((k) => <TableCell key={k} align="right">{(r[k] as number).toLocaleString()}</TableCell>)}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>

        <Typography variant="subtitle2">1:1 parity per user</Typography>
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>User</TableCell><TableCell>Verdict</TableCell>
                <TableCell align="right">Parity</TableCell><TableCell>By service (identical / compared)</TableCell>
                <TableCell align="right">Tally</TableCell><TableCell>Checked</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {users.map((u) => {
                const c = u.services.reduce((n, s) => n + (s.checked || 0), 0)
                const i = u.services.reduce((n, s) => n + (s.identical || 0), 0)
                const t = tallyOf.get(u.user)
                return (
                  <TableRow key={u.user} hover data-testid={`pair-user-${u.user}`}>
                    <TableCell>
                      <Typography variant="body2">{u.user}</Typography>
                      {u.target && <Typography variant="caption" color="text.secondary">→ {u.target}</Typography>}
                    </TableCell>
                    <TableCell><Chip size="small" color={VERDICT[u.verdict].color} label={VERDICT[u.verdict].label} /></TableCell>
                    <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>{parity(i, c)}</TableCell>
                    <TableCell>
                      <Typography variant="caption" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                        {u.services.filter((s) => s.checked || s.verdict !== 'IDENTICAL')
                          .map((s) => `${s.service} ${s.identical.toLocaleString()}/${s.checked.toLocaleString()}`).join(' · ') || '—'}
                      </Typography>
                    </TableCell>
                    <TableCell align="right" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                      {t?.countParity != null ? parity(Math.round(t.countParity * 1e6), 1e6) : '—'}
                    </TableCell>
                    <TableCell>{u.verifiedAt ? new Date(u.verifiedAt).toLocaleString() : '—'}</TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </Box>
      </CardContent>
    </Card>
  )
}

export default PairParityCard
