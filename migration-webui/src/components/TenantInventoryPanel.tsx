import React from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, LinearProgress,
  Stack, Typography,
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
const Stat: React.FC<{
  id: string; label: string; value: string
  /** Not measured yet. Renders an em dash rather than 0 -- a real measured
   *  zero and "still counting" are different facts, and 0 is the one that
   *  gets believed. */
  pending?: boolean
}> = ({ id, label, value, pending = false }) => (
  <Box data-testid={`stat-${id}`}>
    <Typography sx={{ fontWeight: 700, fontSize: 20, lineHeight: 1.2,
                      fontVariantNumeric: 'tabular-nums',
                      color: pending ? 'text.disabled' : 'text.primary' }}>
      {pending ? '—' : value}
    </Typography>
    <Typography variant="caption" color="text.secondary">{label}</Typography>
  </Box>
)

export interface TenantInventoryPanelProps {
  inv: TenantInventory | null
  busy: boolean
  error: string
  domain: string
  /** Accounts walked so far during a running deep scan. A scan whose only
   *  signal is a spinner is indistinguishable from one that has died --
   *  and they do die: a deploy restarts the server under them. */
  scanProgress?: { done: number; total: number } | null
  onRefresh: () => void
  /** Walks every file to read ACLs -- minutes, not seconds -- so it is a
   *  deliberate action rather than part of the panel's own load. */
  onDeepScan?: () => void
}

export const TenantInventoryPanel: React.FC<TenantInventoryPanelProps> = ({
  inv, busy, error, domain, scanProgress, onRefresh, onDeepScan,
}) => {
  const scanning = !!scanProgress
  return (
  <Box sx={{ mt: 2 }} data-testid="tenant-inventory">
    <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
        What&apos;s in {inv?.domain || domain}
      </Typography>
      {busy && <CircularProgress size={14} />}
      {busy && (
        <Typography variant="caption" color="text.secondary"
                    data-testid="scan-progress">
          {scanProgress && scanProgress.total
            ? `scanning ${scanProgress.done.toLocaleString()} of `
              + `${scanProgress.total.toLocaleString()} accounts…`
            : 'reading the tenant…'}
        </Typography>
      )}
      <Box sx={{ flex: 1 }} />
      {onDeepScan && !inv?.deep && (
        <Button size="small" onClick={onDeepScan} disabled={busy}
                data-testid="deep-scan">
          Scan sharing
        </Button>
      )}
      <IconButton size="small" onClick={onRefresh} disabled={busy}
                  aria-label="refresh inventory">
        <RefreshIcon fontSize="small" />
      </IconButton>
    </Stack>

    {scanning && scanProgress.total > 0 && (
      <Box sx={{ mb: 1.5 }} data-testid="scan-banner">
        <LinearProgress variant="determinate" sx={{ mb: 0.5, borderRadius: 1 }}
          value={Math.min(100, Math.round(
            100 * scanProgress.done / scanProgress.total))} />
        <Typography variant="caption" color="text.secondary">
          Reading sharing, Chat and calendar for every account —{' '}
          {scanProgress.done.toLocaleString()} of{' '}
          {scanProgress.total.toLocaleString()} done. Each account&apos;s Drive
          has to be walked, so this takes minutes per account and runs in the
          background; the figures above are already final.
        </Typography>
      </Box>
    )}

    {error && <Alert severity="warning" sx={{ mb: 1 }}>{error}</Alert>}
    {inv?.error && <Alert severity="warning" sx={{ mb: 1 }}>{inv.error}</Alert>}

    {inv && !inv.error && (
      <>
        {/* "messages" was ambiguous next to Chat, which also has messages.
            These are mailbox items, so they are labelled email. */}
        <Stack direction="row" spacing={3} sx={{ mb: 1, flexWrap: 'wrap' }}>
          <Stat id="accounts" label="accounts"
                value={inv.accounts.toLocaleString()} />
          <Stat id="emails" label="email" value={inv.totals.emails.toLocaleString()} />
          <Stat id="drive" label="Drive" value={fmtBytes(inv.totals.driveBytes)} />
          {/* Shown as pending rather than hidden while a scan runs.
              Absent columns read as "this tenant has no sharing"; a dash
              with a scan visibly in flight reads as "not counted yet",
              which is the true state. The alternative was a panel that
              looked like it had less information than it did an hour ago. */}
          {(inv.deep || scanning) && (
            <>
              <Stat id="shared" label="shared files" pending={!inv.deep}
                    value={(inv.totals.shared ?? 0).toLocaleString()} />
              <Stat id="external" label="shared externally" pending={!inv.deep}
                    value={(inv.totals.external ?? 0).toLocaleString()} />
              <Stat id="anyone" label="link-shared to anyone" pending={!inv.deep}
                    value={(inv.totals.anyone ?? 0).toLocaleString()} />
              <Stat id="events" label="calendar events"
                    pending={!inv.deep || inv.totals.calendarEvents == null}
                    value={(inv.totals.calendarEvents ?? 0).toLocaleString()} />
              {/* null here means the scan could not read Chat at all --
                  a different fact from "this tenant has none", and the one
                  that gets believed if it renders as 0. It did: a tenant
                  with a live Chat space reported 0 because the probe 403'd
                  and the count defaulted. */}
              <Stat id="chat" label="Chat messages"
                    pending={!inv.deep || inv.totals.chatMessages == null}
                    value={(inv.totals.chatMessages ?? 0).toLocaleString()} />
              <Stat id="spaces" label="Chat spaces"
                    pending={!inv.deep || inv.totals.chatSpaces == null}
                    value={(inv.totals.chatSpaces ?? 0).toLocaleString()} />
            </>
          )}
        </Stack>

        {inv.deep && inv.deepSampled < inv.accounts && (
          <Typography variant="caption" color="warning.main"
                      data-testid="sample-note"
                      sx={{ display: 'block', mb: 1 }}>
            Sharing figures are from {inv.deepSampled.toLocaleString()} of{' '}
            {inv.accounts.toLocaleString()} accounts. Re-run the scan to cover
            every account — it walks each one&apos;s Drive to read sharing, so
            it takes minutes per account and runs in the background.
          </Typography>
        )}

        {/* Plans. Read with their own single-scope credential, so a tenant
            that has never granted it still gets the rest of the panel --
            and "could not read" is said out loud rather than rendering as
            an empty set, which would read as "this tenant has no licences". */}
        <Box sx={{ mb: 1 }} data-testid="licence-row">
          <Typography variant="caption" color="text.secondary"
                      sx={{ display: 'block', mb: 0.5 }}>
            licences
          </Typography>
          {Object.keys(inv.licenseCounts || {}).length > 0 ? (
            <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
              {Object.entries(inv.licenseCounts).map(([sku, n]) => (
                <Chip key={sku} size="small" variant="outlined"
                      label={`${sku} · ${n.toLocaleString()}`} />
              ))}
            </Stack>
          ) : (
            <Typography variant="caption" color="text.secondary"
                        data-testid="licence-unavailable">
              {inv.licenseError
                ? `not available — ${inv.licenseError}`
                : 'no licence assignments returned for this tenant'}
            </Typography>
          )}
        </Box>

        {/* Drive composition, only meaningful once a deep scan has walked
            the files. */}
        {inv.deep && Object.keys(inv.totals.driveKinds || {}).length > 0 && (
          <Box sx={{ mb: 1 }} data-testid="drive-kinds">
            <Typography variant="caption" color="text.secondary"
                        sx={{ display: 'block', mb: 0.5 }}>
              Drive contents
            </Typography>
            <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
              {Object.entries(inv.totals.driveKinds || {}).map(([k, n]) => (
                <Chip key={k} size="small" variant="outlined"
                      label={`${k} · ${n.toLocaleString()}`} />
              ))}
            </Stack>
          </Box>
        )}

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
                <Box component="th" sx={thSx}>licence</Box>
                <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>email</Box>
                <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>Drive</Box>
                {inv.deep && (
                  <>
                    <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>shared</Box>
                    <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>external</Box>
                    <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>anyone</Box>
                    <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>events</Box>
                    <Box component="th" sx={{ ...thSx, textAlign: 'right' }}>Chat</Box>
                  </>
                )}
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
                  <Box component="td" sx={tdSx}>
                    {u.license || <Box component="span" sx={{ color: 'text.disabled' }}>—</Box>}
                  </Box>
                  <Box component="td" sx={numSx}>
                    {u.emails === null ? '—' : u.emails.toLocaleString()}
                  </Box>
                  <Box component="td" sx={numSx}>
                    {u.driveBytes === null ? '—' : fmtBytes(u.driveBytes)}
                  </Box>
                  {inv.deep && (
                    <>
                      <Box component="td" sx={numSx}>{u.shared ?? '—'}</Box>
                      <Box component="td" sx={numSx}>{u.external ?? '—'}</Box>
                      <Box component="td" sx={numSx}>{u.anyone ?? '—'}</Box>
                      <Box component="td" sx={numSx}>{u.calendarEvents ?? '—'}</Box>
                      <Box component="td" sx={numSx}>{u.chatMessages ?? '—'}</Box>
                    </>
                  )}
                </Box>
              ))}
            </Box>
          </Box>
        </Box>
      </>
    )}
  </Box>
  )
}

export default TenantInventoryPanel
