/**
 * Choose the active source and target from every domain already set up,
 * instead of overwriting a slot.
 *
 * Each account holds one source and one target slot, and setup writes into
 * one of them -- so setting a new domain up evicts whatever was there. This
 * is the other way to arrive at a pair: pick two domains that ALREADY exist
 * (under any account) and make them this account's active source -> target,
 * reusing their keys. Delegation is granted for a key's client id, not the
 * account holding it, so nothing is re-granted; the previous slot's key is
 * backed up first, so the swap is reversible.
 */
import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  Divider, Link, Stack, TextField, Typography,
} from '@mui/material'
import {
  fetchAllDomains, ConfiguredDomain, linkDomains,
} from '@/api/controlPlane'

export const LinkDomainsDialog: React.FC<{
  open: boolean
  onClose: () => void
  onLinked: () => void
  /** "Start a migration" on the Migrations page, "Choose source & target"
   *  on Identities -- the mechanism is the same either way. */
  title?: string
}> = ({ open, onClose, onLinked, title = 'Choose source & target' }) => {
  const navigate = useNavigate()
  const [domains, setDomains] = useState<ConfiguredDomain[]>([])
  const [src, setSrc] = useState('')
  const [tgt, setTgt] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  // Set only when linking itself succeeded but the pair looks under-licensed
  // -- distinct from err, which means the link did not happen at all. Kept
  // open rather than auto-closed like a clean success, because the whole
  // point is that this is easy to miss: live, a target that hit its
  // licence ceiling was not discovered until three hours into a migration
  // that had already started writing data.
  //
  // Kept WITH the pair it describes, and shown only while the selection still
  // is that pair. It used to be a bare string, so once it appeared the dialog
  // sat in a "done" state with only Close: changing the target to a different
  // tenant left the old pair's warning over it and no way to link the new one.
  const [warned, setWarned] = useState<{ pair: string; text: string } | null>(null)
  const warning = warned && warned.pair === `${src}|${tgt}` ? warned.text : ''

  useEffect(() => {
    if (!open) return
    setErr(''); setWarned(null); setSrc(''); setTgt(''); setReason('')
    fetchAllDomains()
      // hasKey only. A superseded domain was filtered out too, which made
      // the one thing 009_superseded_configs.sql kept it for -- re-linking
      // it -- impossible: a pair whose source had since been evicted from
      // its slot simply could not be expressed here, and the domain looked
      // gone. Its key backup is on disk and its delegation is still granted
      // against that key's client id, so it links like any other.
      .then((r) => setDomains(r.domains.filter((d) => d.hasKey)))
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [open])

  // The superseded row id is part of the identity, not decoration: several
  // rows can share one (accountId, side), and without it they collapse into
  // one option that resolves to whichever domain holds the slot now.
  const key = (d: ConfiguredDomain) =>
    `${d.accountId}:${d.side}:${d.supersededId ?? ''}`
  const byKey = (k: string) => domains.find((d) => key(d) === k)
  const label = (d: ConfiguredDomain) =>
    `${d.domain} — ${d.side}${d.superseded ? ' (replaced)' : ''} · `
    + `${d.accountEmail || `account #${d.accountId}`}`

  const ready = src && tgt && src !== tgt && reason.trim().length >= 3

  const connect = async () => {
    const s = byKey(src); const t = byKey(tgt)
    if (!s || !t) return
    setBusy(true); setErr(''); setWarned(null)
    try {
      const r = await linkDomains(reason.trim(),
        { accountId: s.accountId, side: s.side, supersededId: s.supersededId },
        { accountId: t.accountId, side: t.side, supersededId: t.supersededId })
      if (!r.ok) throw new Error(r.detail || 'could not link the domains')
      onLinked()
      // The pair is live either way -- onLinked() already reflects it.
      // A warning just means "stay open, make sure this was seen" instead
      // of closing on a clean run.
      const idx = r.detail.indexOf('⚠')
      if (idx === -1) { onClose(); return }
      setWarned({ pair: `${src}|${tgt}`, text: r.detail.slice(idx) })
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} fullWidth maxWidth="sm">
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Pick any two domains you have already set up as this migration&apos;s
          source and target — including one marked <em>(replaced)</em>, which
          a later setup evicted from its slot but whose key is still on file.
          It reuses their existing keys — no re-setup, and nothing is
          re-granted, because delegation is tied to the key, not the account.
          The domains you did not pick are left exactly as they are.
        </Typography>
        {err && <Alert severity="error" sx={{ mb: 2 }} data-testid="link-error">{err}</Alert>}
        {warning && (
          <Alert severity="warning" sx={{ mb: 2 }} data-testid="link-warning">
            {warning}
          </Alert>
        )}
        <Stack spacing={2}>
          <TextField select fullWidth label="Source (read from)" value={src}
                     onChange={(e) => { setSrc(e.target.value); setErr('') }}
                     SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}
                     inputProps={{ 'data-testid': 'link-source' }}
                     helperText="the tenant whose data is copied">
            <option value="">select a source…</option>
            {domains.map((d) => (
              <option key={key(d)} value={key(d)}>{label(d)}</option>
            ))}
          </TextField>
          <TextField select fullWidth label="Target (written to)" value={tgt}
                     onChange={(e) => { setTgt(e.target.value); setErr('') }}
                     SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}
                     error={!!tgt && tgt === src}
                     helperText={tgt && tgt === src
                       ? 'source and target must differ'
                       : 'the tenant the data lands in'}
                     inputProps={{ 'data-testid': 'link-target' }}>
            <option value="">select a target…</option>
            {domains.map((d) => (
              <option key={key(d)} value={key(d)}>{label(d)}</option>
            ))}
          </TextField>
          <TextField fullWidth label="Reason" value={reason}
                     onChange={(e) => setReason(e.target.value)}
                     placeholder="e.g. rehearsal: rohitrokaya into the new tenant"
                     inputProps={{ 'data-testid': 'link-reason' }} />
        </Stack>
        <Alert severity="info" sx={{ mt: 2 }}>
          The pair lands on your account as the active source and target,
          replacing its current pair. Any key already there is backed up first,
          so this is reversible.
        </Alert>
        <Divider sx={{ my: 2 }} />
        <Typography variant="body2" color="text.secondary">
          Need to set up a brand-new domain instead?{' '}
          <Link component="button" type="button" onClick={() => navigate('/wizard')}
                data-testid="link-to-wizard">Open the Setup Wizard</Link>.
        </Typography>
      </DialogContent>
      <DialogActions>
        {warning ? (
          <Button variant="contained" onClick={onClose} data-testid="link-close">
            Close
          </Button>
        ) : (
          <>
            <Button onClick={onClose} disabled={busy}>Cancel</Button>
            <Button variant="contained" onClick={connect} disabled={!ready || busy}
                    data-testid="link-connect">
              {busy ? 'Saving…' : 'Set as active pair'}
            </Button>
          </>
        )}
      </DialogActions>
    </Dialog>
  )
}

export default LinkDomainsDialog
