import { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Card, CardContent, Checkbox, Chip, FormControlLabel, Link, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography, Accordion, AccordionSummary, AccordionDetails } from '@mui/material'
import { ExpandMore } from '@mui/icons-material'
import { ActionSpec, ConfigurationReport as Report, fetchConfigurationReport } from '@/api/client'
import JobRunner from './JobRunner'

const labels: Record<string, string> = {
  whoCanJoin: 'Who can join', whoCanViewMembership: 'Who can see members', whoCanViewGroup: 'Who can read conversations',
  allowExternalMembers: 'Allow external members', whoCanPostMessage: 'Who can post', allowWebPosting: 'Allow posting on the web',
  primaryLanguage: 'Primary language', isArchived: 'Conversation history', archiveOnly: 'Archive only',
  messageModerationLevel: 'Message moderation', spamModerationLevel: 'Spam moderation', replyTo: 'Reply destination',
  customReplyTo: 'Custom reply address', includeCustomFooter: 'Include custom footer', customFooterText: 'Custom footer',
  sendMessageDenyNotification: 'Notify rejected senders', defaultMessageDenyNotificationText: 'Rejection notice',
  membersCanPostAsTheGroup: 'Members can post as the group', includeInGlobalAddressList: 'Show in global address list',
  whoCanLeaveGroup: 'Who can leave', whoCanContactOwner: 'Who can contact owners', whoCanModerateMembers: 'Who can manage members',
  whoCanModerateContent: 'Who can moderate conversations', whoCanAssistContent: 'Who can assist with conversations',
  enableCollaborativeInbox: 'Collaborative inbox', whoCanDiscoverGroup: 'Who can find the group',
}
const field = (key: string): string => {
  if (key.startsWith('settings:')) return labels[key.slice(9)] || key.slice(9)
  if (key.startsWith('member:')) return `Member role: ${key.slice(7)}`
  if (key.startsWith('alias:')) return `Alias: ${key.slice(6)}`
  return labels[key] || key
}
const value = (v: unknown): string => {
  if (v == null) return 'Not reported'
  if (typeof v === 'object') return JSON.stringify(v)
  if (v === true || v === 'true') return 'On'
  if (v === false || v === 'false') return 'Off'
  const s = String(v)
  return /^[A-Z][A-Z_]+$/.test(s) ? s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, ' ') : s
}
const bytes = (v: string | null | undefined): string => v == null ? 'Not reported' : `${v} bytes`

export default function ConfigurationReport({ accountId, actions }: {
  accountId: number; actions: Record<string, ActionSpec>
}) {
  const [report, setReport] = useState<Report | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [reviewed, setReviewed] = useState(false)
  const load = useCallback(async () => {
    setError('')
    try { const r = await fetchConfigurationReport(accountId); setReport(r.report); setReviewed(false) }
    catch (e) { setReport(null); setError(String(e)) }
    finally { setLoading(false) }
  }, [accountId])
  useEffect(() => { setReport(null); setLoading(true); void load() }, [load])
  const unread = report && Object.values(report.groupInventory).some(r => r.status !== 'read')
  const blocked = unread || report?.groups.some(g => g.blocked.length > 0)
  const changes = report?.groups.reduce((n, g) => n + g.operations.length, 0) ?? 0
  return <Card variant="outlined" sx={{ mb: 3 }} data-testid="configuration-report">
    <CardContent>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="h6">Groups and storage configuration</Typography>
        <Chip size="small" label="Read, compare, review" variant="outlined" />
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Inspect both tenants before copying settings. This report keeps the evidence here;
        opening this page reads saved results and does not scan Google in the background.
      </Typography>
      {actions.configuration_check ? <JobRunner accountId={accountId} name="configuration_check"
        spec={actions.configuration_check} onDone={load} /> : <Alert severity="warning">
        The configuration checker is not installed on this server. Update the backend to use these checks.
      </Alert>}
      {loading && <Typography sx={{ mt: 2 }}>Loading saved configuration…</Typography>}
      {error && <Alert severity="error" sx={{ mt: 2 }}>Configuration evidence unavailable: {error}</Alert>}
      {!loading && !report && !error && <Alert severity="info" sx={{ mt: 2 }}>
        Not checked yet. Inspect groups and storage to capture source and target values.
      </Alert>}
      {report && <Box sx={{ mt: 3 }}>
        <Typography variant="subtitle2">{report.sourceDomain} → {report.targetDomain}</Typography>
        <Typography variant="caption" color="text.secondary">Saved {new Date(report.checkedAt).toLocaleString()}. Values can change after this check.</Typography>
        <Typography variant="body2" sx={{ mt: 1 }}>Group lists: source {report.groupInventory.source.status} ({report.groupInventory.source.value?.length ?? 'unknown'}), target {report.groupInventory.target.status} ({report.groupInventory.target.value?.length ?? 'unknown'}).</Typography>
        {(['source', 'target'] as const).map(side => report.groupInventory[side].status !== 'read' &&
          <Alert key={side} severity="warning" sx={{ mt: 1 }}>{side} group inventory not checked: {report.groupInventory[side].error || 'Not available'}</Alert>)}
        <Typography variant="subtitle1" sx={{ mt: 2 }}>{report.groups.length} groups inspected · {changes} proposed changes</Typography>
        {!!report.targetOnlyGroups?.length && <Alert severity="info">Existing target-only groups retained: {report.targetOnlyGroups.join(', ')}</Alert>}
        {!report.groups.length && !unread && <Alert severity="info">Both group lists were read. The source has no groups.</Alert>}
        {report.groups.map(g => <Accordion key={g.source} disableGutters elevation={0} sx={{ borderBottom: '1px solid', borderColor: 'divider' }}>
          <AccordionSummary expandIcon={<ExpandMore />}>
            <Stack sx={{ width: '100%' }} direction={{ xs: 'column', md: 'row' }} spacing={1} alignItems={{ md: 'center' }}>
              <Box sx={{ flex: 1 }}><Typography variant="body2">{g.source}</Typography><Typography variant="caption" color="text.secondary">To {g.target}</Typography></Box>
              <Chip size="small" color={g.status === 'matched' ? 'success' : 'warning'} label={g.status === 'matched' ? 'Compared fields match' : g.status === 'not_checked' ? 'Checks incomplete' : 'Differences found'} />
              <Typography variant="caption">{g.operations.length} changes</Typography>
            </Stack>
          </AccordionSummary>
          <AccordionDetails>
            <Typography variant="body2" sx={{ mb: 1 }}>Checks cover name, description, editable aliases, mapped member roles and the supported Groups Settings fields below. Non-editable domain aliases and group archive history need separate review.</Typography>
            {(['source', 'target'] as const).map(side => <Box key={side} sx={{ mb: 1 }}>
              <Typography variant="subtitle2">{side === 'source' ? 'Source' : 'Target'} evidence</Typography>
              <Typography variant="body2">Name: {g.metadata?.[side].name || 'Not reported'} · Description: {g.metadata?.[side].description || '(empty)'}</Typography>
              <Typography variant="body2">Aliases: {g.aliases?.[side].join(', ') || 'None reported'}</Typography>
              {g.members?.[side] && <Typography variant="body2">Members: {g.members[side].status} · {g.members[side].checkedAt || 'Target group not created'} · {g.members[side].error || g.members[side].value?.map(m => `${m.email} (${m.role})`).join(', ') || 'No members reported'}</Typography>}
              {g.settings?.[side] && <Typography variant="body2">Settings: {g.settings[side].status} · {g.settings[side].checkedAt || 'Target group not created'}{g.settings[side].error ? ` · ${g.settings[side].error}` : ''}</Typography>}
            </Box>)}
            {g.settings && <Box sx={{ overflowX: 'auto' }}><Table size="small" aria-label={`Compared settings ${g.source}`}>
              <TableHead><TableRow><TableCell>Supported setting</TableCell><TableCell>Source</TableCell><TableCell>Target</TableCell></TableRow></TableHead>
              <TableBody>{Array.from(new Set([...Object.keys(g.settings.source.value || {}), ...Object.keys(g.settings.target.value || {})])).sort().map(k => <TableRow key={k}><TableCell>{field(k)}</TableCell><TableCell>{value(g.settings?.source.value?.[k])}</TableCell><TableCell>{value(g.settings?.target.value?.[k])}</TableCell></TableRow>)}</TableBody>
            </Table></Box>}
            {g.blocked.map((b, i) => <Alert key={i} severity="warning" sx={{ mb: 1 }}>{b}</Alert>)}
            {g.differences.length > 0 && <Box sx={{ overflowX: 'auto' }}><Table size="small" aria-label={`Group differences ${g.source}`}>
              <TableHead><TableRow><TableCell>Setting</TableCell><TableCell>Source / mapped expectation</TableCell><TableCell>Target</TableCell></TableRow></TableHead>
              <TableBody>{g.differences.map((d, i) => <TableRow key={i}><TableCell>{field(d.field)}</TableCell><TableCell sx={{ overflowWrap: 'anywhere' }}>{value(d.source)}</TableCell><TableCell sx={{ overflowWrap: 'anywhere' }}>{value(d.target)}</TableCell></TableRow>)}</TableBody>
            </Table></Box>}
          </AccordionDetails>
        </Accordion>)}
        <Alert severity="warning" sx={{ mt: 2 }}>
          Applying this plan changes target group aliases, member roles, posting, moderation and visibility settings.
          It can grant access to group content. Review each difference before applying. Target-only memberships are retained and need review.
        </Alert>
        <FormControlLabel control={<Checkbox checked={reviewed} onChange={e => setReviewed(e.target.checked)} disabled={!!blocked || changes === 0} />}
          label="I reviewed the target group changes and their access implications" />
        {blocked && <Alert severity="info" sx={{ mb: 1 }}>Apply is blocked until unreadable settings and mapping gaps are resolved. No access is granted automatically.</Alert>}
        {actions.configuration_apply_groups && <JobRunner accountId={accountId} name="configuration_apply_groups"
          spec={actions.configuration_apply_groups} onDone={load} extra={{ review_hash: report.planHash }}
          disabled={!reviewed || !!blocked || changes === 0} />}
        {report.applyResults?.map((r, i) => <Alert key={i} severity={r.status === 'error' ? 'error' : 'info'} sx={{ mt: 1 }}>
          {r.target}: {r.kind} — {r.status}{r.error ? `: ${r.error}` : ''}. Read-back results are shown above.
        </Alert>)}
        <Typography variant="h6" sx={{ mt: 4 }}>Exact reported storage</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          Original byte strings from Google Drive, with each read time. A storage gap is a signal to investigate;
          it does not prove missing files. Ownership, trash, revisions and reporting delay can change charged storage.
        </Typography>
        {!report.storage.length && <Alert severity="warning">No mapped user pairs were checked for storage. Add identity mappings first.</Alert>}
        <Box sx={{ overflowX: 'auto' }}><Table size="small" aria-label="Exact source and target storage">
          <TableHead><TableRow><TableCell>User pair / read time</TableCell><TableCell>Source Drive</TableCell><TableCell>Target Drive</TableCell><TableCell>Target − source</TableCell><TableCell>Reported limits (source / target)</TableCell></TableRow></TableHead>
          <TableBody>{report.storage.map(s => <TableRow key={s.source}>
            <TableCell><Typography variant="body2">{s.source} → {s.target}</Typography><Typography variant="caption">Source: {s.sourceQuota.checkedAt || 'Not checked'}<br />Target: {s.targetQuota.checkedAt || 'Not checked'}</Typography>
              {[s.sourceQuota, s.targetQuota].map((q, i) => q.status !== 'read' && <Alert key={i} severity="warning">{i ? 'Target' : 'Source'} not checked: {q.error}</Alert>)}
            </TableCell>
            <TableCell>{bytes(s.sourceQuota.value?.usageInDrive)}<Typography variant="caption" display="block">Trash: {bytes(s.sourceQuota.value?.usageInDriveTrash)}</Typography></TableCell>
            <TableCell>{bytes(s.targetQuota.value?.usageInDrive)}<Typography variant="caption" display="block">Trash: {bytes(s.targetQuota.value?.usageInDriveTrash)}</Typography></TableCell>
            <TableCell>{bytes(s.targetMinusSourceBytes.usageInDrive)}</TableCell>
            <TableCell>{bytes(s.sourceQuota.value?.limit)} / {bytes(s.targetQuota.value?.limit)}<Typography variant="caption" display="block">May represent pooled capacity; not a per-user policy comparison.</Typography></TableCell>
          </TableRow>)}</TableBody>
        </Table></Box>
        <Alert severity="warning" sx={{ mt: 2 }}>
          <Typography fontWeight={600}>Storage policy: Admin Console review required</Typography>
          {report.storagePolicy.reason}
          <Box component="ul">{report.storagePolicy.items.map(item => <li key={item}>{item}</li>)}</Box>
          <Link href="https://admin.google.com/ac/storage" target="_blank" rel="noreferrer">Open Google Admin storage settings</Link>
          <Typography variant="body2">Compare both tenants using separate signed-in sessions. A lower limit can prevent uploads; review before changing it.</Typography>
        </Alert>
        {report.limits.map(limit => <Typography key={limit} variant="caption" display="block" color="text.secondary" sx={{ mt: .5 }}>{limit}</Typography>)}
      </Box>}
    </CardContent>
  </Card>
}
