import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, Chip, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material'
import { fetchUserProfiles, startUserProfileSync, UserProfileCheck } from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

export default function UserProfilesPanel({ accountId, busy }: { accountId: number; busy: boolean }) {
  const [rows, setRows] = useState<UserProfileCheck[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [dialog, setDialog] = useState<'preview' | 'apply' | null>(null)
  const [starting, setStarting] = useState(false)
  const refresh = useCallback(() => {
    fetchUserProfiles(accountId).then(r => { setRows(r.profiles); setError(null) })
      .catch(e => setError(String(e.message ?? e)))
  }, [accountId])
  useEffect(refresh, [refresh])
  return <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
      <Typography variant="subtitle1" sx={{ fontWeight: 700, flexGrow: 1 }}>User profiles</Typography>
      <Button size="small" onClick={refresh}>Refresh</Button>
      <Button size="small" disabled={busy || starting} onClick={() => setDialog('preview')}>Compare profiles</Button>
      <Button size="small" variant="outlined" disabled={busy || starting} onClick={() => setDialog('apply')}>Sync target profiles</Button>
    </Stack>
    <Typography variant="body2" color="text.secondary" sx={{ my: 1 }}>
      Copies supported profile details from the source: names, job information, phones, addresses and other profile metadata.
      Existing target accounts are included. Source accounts are read only.
    </Typography>
    <Typography variant="caption" color="text.secondary" display="block">
      Photos, aliases, custom fields, organizational units and account security need separate checks. These results cover supported profile fields.
    </Typography>
    {busy && <Typography variant="caption" display="block">Available when the current account job finishes.</Typography>}
    {error && <Alert severity="warning" sx={{ mt: 1 }}>{error}</Alert>}
    {notice && <Alert severity="info" sx={{ mt: 1 }}>{notice}</Alert>}
    {!rows.length && <Typography variant="body2" sx={{ mt: 1 }}>No profile comparison recorded yet.</Typography>}
    {!!rows.length && <>
      <Box sx={{ overflow: 'auto', maxHeight: 320, mt: 1 }}>
        <Table size="small" stickyHeader><TableHead><TableRow>
          <TableCell>Source user</TableCell><TableCell>Source name</TableCell><TableCell>Target name</TableCell><TableCell>Profile result</TableCell>
        </TableRow></TableHead><TableBody>{rows.map(row => <TableRow key={row.source}>
          <TableCell>{row.source}</TableCell>
          <TableCell>{[row.sourceName?.givenName, row.sourceName?.familyName].filter(Boolean).join(' ') || 'Unavailable'}</TableCell>
          <TableCell>{[row.targetName?.givenName, row.targetName?.familyName].filter(Boolean).join(' ') || 'Unavailable'}</TableCell>
          <TableCell><Chip size="small" label={row.status.replace(/_/g, ' ')}
            color={row.status === 'ERROR' || row.status === 'NEEDS_ATTENTION' ? 'warning' : 'default'} />
            <Typography variant="caption" display="block">{row.error || row.changedFields.join(', ')}</Typography>
            {row.checkedAt && <Typography variant="caption" color="text.secondary" display="block">{new Date(row.checkedAt).toLocaleString()}</Typography>}
          </TableCell>
        </TableRow>)}</TableBody></Table>
      </Box>
      <Button size="small" onClick={() => {
        const csv = [['Source email', 'Target email', 'Source first name', 'Source last name', 'Target first name', 'Target last name', 'Status'],
          ...rows.map(r => [r.source, r.target, r.sourceName?.givenName ?? '', r.sourceName?.familyName ?? '', r.targetName?.givenName ?? '', r.targetName?.familyName ?? '', r.status])]
          .map(row => row.map(value => `"${String(/^[=+\-@\t\r\n]/.test(value) ? `'${value}` : value).replace(/"/g, '""')}"`).join(',')).join('\r\n')
        const url = URL.createObjectURL(new Blob(['\ufeff', csv], { type: 'text/csv;charset=utf-8;' }))
        const link = document.createElement('a'); link.href = url; link.download = 'user-profile-comparison.csv'; link.click(); URL.revokeObjectURL(url)
      }}>Export comparison CSV</Button>
    </>}
    <ReasonCodeDialog open={dialog !== null} busy={starting} error={null}
      title={dialog === 'apply' ? 'Sync target user profiles' : 'Compare user profiles'}
      description={dialog === 'apply'
        ? 'Replaces supported target profile fields with source values for mapped users, including clearing target-only values in those fields. Checks each update afterward. Passwords, recovery settings, administrator privileges and source accounts are unchanged.'
        : 'Reads both directories and records differences. Does not modify either tenant. The comparison CSV is for review, not a bulk account import.'}
      onCancel={() => setDialog(null)} onConfirm={async reason => {
        setStarting(true)
        try {
          const result = await startUserProfileSync(reason, accountId, dialog !== 'apply')
          if (!result.ok) throw new Error(result.detail)
          setNotice(result.detail + '. View progress on Jobs, then refresh this comparison.')
          setDialog(null)
        } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
        finally { setStarting(false) }
      }} />
  </Paper>
}
