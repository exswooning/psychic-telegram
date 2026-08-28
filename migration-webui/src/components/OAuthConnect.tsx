import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Link,
  Stack, Typography,
} from '@mui/material'
import {
  fetchOAuthStatus, oauthBegin, oauthDisconnect, OAuthStatus,
} from '@/api/client'

/**
 * Connect a tenant by an administrator's OAuth consent instead of by
 * domain-wide delegation.
 *
 * The endpoints have existed from the start and no page ever called them,
 * so this was reachable only by curl. It is offered as an alternative to
 * DWD, not a replacement, and the panel leads with the reason: an OAuth
 * grant acts as the CONSENTING ADMIN, not as an arbitrary user. There is no
 * `subject` to switch into somebody else's mailbox, so a tenant connected
 * this way can migrate exactly one account -- the one that consented.
 *
 * auth.py refuses loudly rather than silently migrating the wrong mailbox,
 * which is right, but by then somebody has already configured a tenant they
 * cannot use. Saying it before the button is the cheaper place.
 */
const OAuthConnect: React.FC = () => {
  const [st, setSt] = useState<OAuthStatus | null>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [url, setUrl] = useState<string | null>(null)

  const refresh = useCallback(() => {
    fetchOAuthStatus().then(setSt).catch((e) => setErr(String(e)))
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const connect = async (tenant: 'source' | 'target') => {
    setBusy(tenant); setErr(null); setUrl(null)
    try {
      const r = await oauthBegin(tenant)
      if (!r.ok || !r.url) throw new Error(r.error || 'could not start the flow')
      setUrl(r.url)
      window.open(r.url, '_blank', 'noopener')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(''); refresh()
    }
  }

  const disconnect = async (tenant: 'source' | 'target') => {
    setBusy(tenant); setErr(null)
    try {
      const r = await oauthDisconnect(tenant)
      if (!r.ok) throw new Error(r.error || 'could not disconnect')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(''); refresh()
    }
  }

  const row = (tenant: 'source' | 'target') => {
    const t = st ? st[tenant] : null
    const on = Boolean(t && (t.connected || t.email))
    return (
      <Stack key={tenant} direction="row" spacing={2} alignItems="center"
             sx={{ flexWrap: 'wrap', gap: 1 }}>
        <Typography variant="body2" sx={{ fontWeight: 600, width: 64 }}>
          {tenant}
        </Typography>
        <Chip size="small" data-testid={`oauth-${tenant}`}
              color={on ? 'success' : 'default'}
              variant={on ? 'filled' : 'outlined'}
              label={on ? (t?.email || 'connected') : 'not connected'} />
        <Box sx={{ flex: 1 }} />
        <Button size="small" variant="outlined"
                disabled={!st?.configured || busy === tenant}
                onClick={() => connect(tenant)}>
          {on ? 'Reconnect' : 'Connect'}
        </Button>
        <Button size="small" color="error" disabled={!on || busy === tenant}
                onClick={() => disconnect(tenant)}>
          Disconnect
        </Button>
      </Stack>
    )
  }

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mt: 3 }}>
      <CardContent>
        <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>
          Connect by OAuth (alternative to delegation)
        </Typography>

        <Alert severity="warning" sx={{ mb: 2 }}>
          <strong>An OAuth grant acts as the admin who consented, not as an
          arbitrary user.</strong> There is no way to switch into somebody
          else&apos;s mailbox with it, so a tenant connected this way can
          migrate exactly one account — the one that consented. For a
          whole-tenant migration you want domain-wide delegation above, or
          the app installed domain-wide from the Marketplace.
        </Alert>

        {st === null && !err && <CircularProgress size={18} />}

        {st && !st.configured && (
          <Alert severity="info" sx={{ mb: 2 }}>
            No OAuth client secrets on file at <code>{st.client_secrets_path}</code>.
            Create one OAuth client ID (Desktop or Web) in any GCP project and
            upload the JSON — done once, by you, not by each tenant.
          </Alert>
        )}

        {st && (
          <Stack spacing={1.5} sx={{ mb: 2 }}>
            <Typography variant="caption" color="text.secondary">
              current auth mode: <strong>{st.auth_mode}</strong>
            </Typography>
            {row('source')}
            {row('target')}
          </Stack>
        )}

        {url && (
          <Alert severity="info" sx={{ mb: 2 }}>
            A consent page was opened. If nothing appeared, use{' '}
            <Link href={url} target="_blank" rel="noopener">this link</Link>.
            The flow redirects to <code>localhost</code>, so it has to finish
            in a browser on the machine running this server — over an SSH
            tunnel, not against the public address.
          </Alert>
        )}

        {err && <Alert severity="error">{err}</Alert>}
      </CardContent>
    </Card>
  )
}

export default OAuthConnect
