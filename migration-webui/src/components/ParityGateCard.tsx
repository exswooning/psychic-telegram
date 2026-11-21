/**
 * The parity gate on the migration itself: one verdict (PARITY / NOT PARITY / NOT VERIFIED), what fails it
 * by kind -- and whether Bitport repairs that on its own or a person must act -- what is accepted, and what
 * was not migrated with its reason. Read from `/api/v2/parity-gate`, computed from the ledger each time.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Chip, Paper, Stack, Table, TableBody, TableCell, TableRow, Typography } from '@mui/material'
import { fetchParityGate } from '@/api/controlPlane'
import type { ParityGate } from '@/api/controlPlane'

const LOOK = {
  PARITY: { label: 'Parity', color: 'success' as const, says: 'Every user and service is identical, or what differs is accepted.' },
  NOT_PARITY: { label: 'Not at parity', color: 'error' as const, says: 'Something differs that is not accepted.' },
  UNVERIFIED: { label: 'Not verified', color: 'warning' as const, says: 'A check is missing, unfinished, or older than the last change.' },
}

export const ParityGateCard: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [gate, setGate] = useState<ParityGate | null>(null)
  const [error, setError] = useState('')
  const load = useCallback(() => {
    fetchParityGate(accountId).then((g) => { setGate(g); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [load])

  if (error) return <Alert severity="warning" sx={{ mb: 2 }} data-testid="parity-gate-error">{error}</Alert>
  if (!gate || !gate.users) return null
  const look = LOOK[gate.verdict]
  const kinds = gate.differenceKinds ?? []
  const unverified = gate.unverified ?? []

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="parity-gate">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <Typography variant="h6" sx={{ fontWeight: 700, flexGrow: 1 }}>Parity gate</Typography>
        <Chip color={look.color} label={look.label} data-testid="parity-gate-verdict" />
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {look.says} {gate.usersAtParity ?? 0} of {gate.users} users at parity.
      </Typography>
      {unverified.length > 0 && (
        <Typography variant="body2" sx={{ mb: 1 }} data-testid="parity-gate-unverified">
          {unverified.length} check{unverified.length === 1 ? '' : 's'} to run again
          {' '}({[...new Set(unverified.map((u) => u.service))].join(', ')}).
        </Typography>
      )}
      {kinds.length > 0 && (
        <Table size="small" sx={{ mb: 1 }}>
          <TableBody>
            {kinds.slice(0, 10).map((k) => (
              <TableRow key={k.kind} data-testid={`parity-gate-kind-${k.kind}`}>
                <TableCell sx={{ fontVariantNumeric: 'tabular-nums', width: 80 }}>{k.count.toLocaleString()}</TableCell>
                <TableCell><code>{k.kind}</code></TableCell>
                <TableCell>
                  <Chip size="small" variant="outlined" color={k.how === 'auto' ? 'info' : 'warning'}
                        label={k.how === 'auto' ? 'Bitport repairs it' : 'A person must act'} />
                </TableCell>
                <TableCell sx={{ color: 'text.secondary' }}>
                  {k.users.slice(0, 4).map((u) => u.split('@')[0]).join(', ')}{k.users.length > 4 ? ` +${k.users.length - 4}` : ''}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {(gate.remainder ?? []).length > 0 && (
        <Box data-testid="parity-gate-remainder">
          <Typography variant="body2" sx={{ fontWeight: 600 }}>Not migrated, with the reason</Typography>
          {(gate.remainder ?? []).map((r) => (
            <Typography key={r.status} variant="body2" color="text.secondary">
              {r.count.toLocaleString()} — {r.why}
            </Typography>
          ))}
        </Box>
      )}
    </Paper>
  )
}

export default ParityGateCard
