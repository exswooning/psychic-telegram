import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Card, CardContent, Checkbox, FormControlLabel, Stack,
  Typography,
} from '@mui/material'
import { Hub as ServicesIcon } from '@mui/icons-material'
import {
  fetchActions, fetchToggles, setToggles, ActionSpec,
} from '@/api/client'
import JobRunner from '@/components/JobRunner'
import DmsImportButton from '@/components/DmsImportButton'
import DmsMetrics from '@/components/DmsMetrics'

/**
 * The migration capabilities that the per-user run does not cover.
 *
 * All six had ACTIONS entries and no button anywhere in the app, so the
 * only way to move a shared drive or an SSO profile was to SSH in and run
 * the script. Shared Drives in particular is not a niche: a tenant's shared
 * drives hold the files that belong to no single person, which is exactly
 * what a per-user migration cannot reach by definition.
 *
 * Grouped rather than scattered because they share a shape: each is a
 * tenant-wide action with a read-only inventory alongside it, and the
 * inventory is the one you run first.
 */
const Services: React.FC = () => {
  const [actions, setActions] = useState<Record<string, ActionSpec>>({})
  const [err, setErr] = useState<string | null>(null)
  // The phased actions below read these from the server, not from the
  // per-run --services flag. They default OFF, so without this the
  // full-scope button silently skipped Chat, Contacts and Tasks and
  // nothing on screen said so.
  const [svc, setSvc] = useState<Record<string, boolean>>({})
  const [dry, setDry] = useState(false)

  useEffect(() => {
    fetchActions().then(setActions).catch((e) => setErr(String(e)))
    fetchToggles()
      .then((t) => { setSvc(t.toggles.services || {}); setDry(!!t.toggles.dry_run) })
      .catch(() => { /* the actions still work without the switches */ })
  }, [])

  const flip = async (key: string, on: boolean) => {
    const next = { ...svc, [key]: on }
    setSvc(next)
    try {
      const t = await setToggles(next, dry)
      setSvc(t.toggles.services || next)
    } catch (e) {
      setErr(String(e))
    }
  }

  const has = (k: string) => Boolean(actions[k])

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <ServicesIcon color="action" />
        <Typography variant="h4" sx={{ fontWeight: 700 }}>Other services</Typography>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Tenant-wide migration steps that the per-user run does not cover.
        Run the inventory first — each one is read-only and tells you what
        the migrate step would touch.
      </Typography>

      {err && <Alert severity="warning" sx={{ mb: 3 }}>{err}</Alert>}
      {Object.keys(actions).length === 0 && !err && (
        <Typography variant="body2" color="text.secondary">Loading…</Typography>
      )}

      {(has('shared_drives_inventory') || has('shared_drives_migrate')) && (
        <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
          <CardContent>
            <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>
              Shared drives
            </Typography>
            <Alert severity="info" sx={{ mb: 2 }}>
              A shared drive belongs to no single person, so a per-user
              migration cannot reach one by definition. Membership is
              restored organizer-first, because a drive with no organizer
              cannot be administered afterwards.
            </Alert>
            <Alert severity="warning" sx={{ mb: 2 }}>
              Being a domain admin lets you <em>list</em> every shared drive,
              but not read inside one you are not a member of. Each drive is
              therefore read as one of its own members. A drive with no user
              member is reported as unreadable and skipped rather than
              failing the run.
            </Alert>
            <Stack spacing={2}>
              {has('shared_drives_inventory') &&
                <JobRunner name="shared_drives_inventory"
                           spec={actions.shared_drives_inventory} />}
              {has('shared_drives_migrate') &&
                <JobRunner name="shared_drives_migrate"
                           spec={actions.shared_drives_migrate} />}
            </Stack>
          </CardContent>
        </Card>
      )}

      {(has('sso_inventory') || has('sso_migrate')) && (
        <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
          <CardContent>
            <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>
              SSO profiles
            </Typography>
            <Alert severity="info" sx={{ mb: 2 }}>
              Inbound SAML profiles are recreated on the target
              <strong> unassigned</strong> — assigning them is deliberately
              left to a person, because a wrong assignment locks users out
              of the tenant you just migrated them into. &quot;Sign in with
              Google&quot; grants can only be listed, not moved.
            </Alert>
            <Stack spacing={2}>
              {has('sso_inventory') &&
                <JobRunner name="sso_inventory" spec={actions.sso_inventory} />}
              {has('sso_migrate') &&
                <JobRunner name="sso_migrate" spec={actions.sso_migrate} />}
            </Stack>
          </CardContent>
        </Card>
      )}

      {(has('phased_count_only') || has('phased_migrate')) && (
        <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
          <CardContent>
            <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>
              Full-scope run
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Which services these two cover. Drive, Gmail and Calendar are on
              by default; Chat, Contacts and Tasks each widen the OAuth grant,
              and a scope the Admin console has not authorised fails every
              call for that service — so they start off.
            </Typography>
            <Stack direction="row" spacing={2} sx={{ mb: 1, flexWrap: 'wrap' }}>
              {['drive', 'gmail', 'calendar', 'chat', 'contacts', 'tasks'].map((k) => (
                <FormControlLabel
                  key={k}
                  control={<Checkbox size="small" checked={!!svc[k]}
                                     data-testid={`toggle-${k}`}
                                     onChange={(e) => flip(k, e.target.checked)} />}
                  label={<Typography variant="body2">{k}</Typography>}
                />
              ))}
            </Stack>
            {svc.gmail === false && (
              <Alert severity="warning" sx={{ mb: 2 }} data-testid="gmail-off">
                Gmail is switched off, so the full-scope run will not touch
                mailboxes. That is the right setting while Google&apos;s own
                Data Import tool is doing the mail — and the wrong one if
                nothing else is.
              </Alert>
            )}
            {!Object.values(svc).some(Boolean) && (
              <Alert severity="error" sx={{ mb: 2 }} data-testid="nothing-on">
                Nothing is selected. Turn at least one service on — an empty
                selection runs every phase, not none.
              </Alert>
            )}
            <Alert severity="info" sx={{ mb: 2 }}>
              Every service in order, each reconciled against the tenants
              directly rather than trusted from the ledger. Reconcile counts
              both sides and moves nothing — it is the honest answer to
              &quot;did that migration actually land?&quot;, and it reads the
              services from the ledger, so it covers what the run did rather
              than whatever the toggles currently say.
            </Alert>
            <Stack spacing={2}>
              {has('phased_count_only') &&
                <JobRunner name="phased_count_only" spec={actions.phased_count_only} />}
              {has('phased_migrate') &&
                <JobRunner name="phased_migrate" spec={actions.phased_migrate} />}
            </Stack>
            {has('dms_import') && (
              <Box sx={{ mt: 2, pt: 2, borderTop: '1px solid', borderColor: 'divider' }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
                  Mail via Google DMS
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                  Run the full-scope migration with Gmail switched off (above),
                  then hand the mail to Google&apos;s Data Migration Service.
                  It runs in parallel and spends none of this project&apos;s
                  Gmail quota; watch per-user progress in the Admin console.
                </Typography>
                <DmsImportButton spec={actions.dms_import} />
                <DmsMetrics />
              </Box>
            )}
          </CardContent>
        </Card>
      )}
    </Box>
  )
}

export default Services
