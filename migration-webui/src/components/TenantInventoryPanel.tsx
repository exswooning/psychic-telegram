import React from 'react'
import {
  Alert, Box, CircularProgress, IconButton, Stack, Typography,
} from '@mui/material'
import { Refresh as RefreshIcon } from '@mui/icons-material'
import type { TenantInventory } from '@/api/controlPlane'

/**
 * What is actually in the tenant that was just set up: how many accounts,
 * and how much data each one holds.
 *
 * The honesty rule this is built around: a per-account probe fails for
 * ordinary reasons -- a suspended or never-provisioned mailbox answers
 * 400/401, and a real 201-account tenant had exactly one -- so the totals
 * are frequently built from fewer accounts than the tenant has. A total
 * that does not carry its denominator reads as the whole tenant, and the
 * reader has no way to notice. So `covered` is shown whenever it is not the
 * full headcount, and an unread account renders as an em dash rather than
 * 0: a real measured zero and "could not read" are different facts.
 */

/** Bytes at human scale. Binary units (1024), matching what the Admin
 * console and Drive itself report, so the two agree. */
export const fmtBytes = (n: number): string => {
  if (!n) return '0'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

const thSx = {
  textAlign: 'left' as const, px: 1, py: 0.5, fontWeight: 700,
  borderBottom: '1px solid', borderColor: 'divider',
  color: 'text.secondary', fontSize: 11,
}

const tdSx = { px: 1, py: 0.5, borderBottom: '1px solid', borderColor: 'divider' }
const numSx = { ...tdSx, textAlign: 'right' as const,
                fontVariantNumeric: 'tabular-nums' }

/** One headline number. Value first at a readable size, label under it --
 * the number is what is being scanned for, not the word. */
const Stat: React.FC<{ id: string; label: string; value: string }> =
  ({ id, label, value }) => (
    <Box data-testid={`stat-${id}`}>
      <Typography sx={{ fontWeight: 700, fontSize: 20, lineHeight: 1.2,
                        fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </Typography>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Box>
  )

export interface TenantInventoryPanelProps {
  inv: TenantInventory | null
  busy: boolean
  error: string
  domain: string
  onRefresh: () => void
}

export const TenantInventoryPanel: React.FC<TenantInventoryPanelProps> = ({
  inv, busy, error, domain, onRefresh,
}) => (
  <Box sx={{ mt: 2 }} data-testid="tenant-inventory">
    <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
        What&apos;s in {inv?.domain || domain}
      </Typography>
      {busy && <CircularProgress size={14} />}
      <Box sx={{ flex: 1 }} />
      <IconButton size="small" onClick={onRefresh} disabled={busy}
                  aria-label="refresh inventory">
        <RefreshIcon fontSize="small" />
      </IconButton>
    </Stack>

    {error && <Alert severity="warning" sx={{ mb: 1 }}>{error}</Alert>}
    {inv?.error && <Alert severity="warning" sx={{ mb: 1 }}>{inv.error}</Alert>}

    {inv && !inv.error && (
      <>
        <Stack direction="row" spacing={3} sx={{ mb: 1, flexWrap: 'wrap' }}>
          <Stat id="accounts" label="accounts"
                value={inv.accounts.toLocaleString()} />
          <Stat id="messages" label="messages"
                value={inv.totals.messages.toLocaleString()} />
          <Stat id="drive" label="Drive" value={fmtBytes(inv.totals.driveBytes)} />
        </Stack>

        {(inv.totals.covered < inv.accounts || inv.truncated) && (
          <Typography variant="caption" color="text.secondary"
                      data-testid="coverage-note"
                      sx={{ display: 'block', mb: 1 }}>
            Totals cover {inv.totals.covered.toLocaleString()} of{' '}
            {inv.accounts.toLocaleString()} accounts
            {inv.truncated ? ' (list truncated)' : ''}
            {inv.accounts - inv.totals.covered > 0
              ? ` — ${(inv.accounts - inv.totals.covered).toLocaleString()} could `
                + 'not be read (suspended or never signed in).'
              : '.'}
          </Typography>
        )}

        <Box sx={{ maxHeight: 240, overflowY: 'auto', border: '1px solid',
                   borderColor: 'divider', borderRadius: 1 }}>
          <Box component="table" sx={{ width: '100%', borderCollapse: 'collapse',
                                       fontSize: 12 }}>
            <Box component="thead" sx={{ position: 'sticky', top: 0,
                                         bgcolor: 'background.paper' }}>
              <Box component="tr">
                <Box component="th" sx={thSx}>account</Box>
                <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>messages</Box>
                <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>Drive</Box>
              </Box>
            </Box>
            <Box component="tbody">
              {inv.users.map((u) => (
                <Box component="tr" key={u.email} data-testid={`row-${u.email}`}>
                  <Box component="td" sx={tdSx}>
                    {u.email}
                    {u.error && (
                      <Typography variant="caption" color="warning.main"
                                  sx={{ display: 'block' }}>
                        could not read
                      </Typography>
                    )}
                  </Box>
                  <Box component="td" sx={numSx}>
                    {u.messages === null ? '—' : u.messages.toLocaleString()}
                  </Box>
                  <Box component="td" sx={numSx}>
                    {u.driveBytes === null ? '—' : fmtBytes(u.driveBytes)}
                  </Box>
                </Box>
              ))}
            </Box>
          </Box>
        </Box>
      </>
    )}
  </Box>
)

export default TenantInventoryPanel
