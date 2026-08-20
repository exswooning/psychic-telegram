import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Button, Checkbox, Chip, Collapse, FormControlLabel, Stack,
  TextField, Typography,
} from '@mui/material'
import {
  Autorenew as ReprovisionIcon, ExpandMore as ExpandIcon,
} from '@mui/icons-material'
import { fetchScopeOptions, ScopeOptions } from '@/api/controlPlane'

/**
 * Re-provision a tenant's Cloud project, and choose what gets granted.
 *
 * Why re-provisioning is offered at all
 * ------------------------------------
 * Setup accepts UPLOADED service-account keys, and an upload carries no
 * relationship to who owns the project behind it. A key can therefore point
 * at a project the Workspace admin holds no IAM role on -- seen live, where
 * every console-driven step (Chat app configuration) failed because the
 * admin could not load its own project's page, and there was no way to
 * grant access from this side. The only project the admin reliably controls
 * is one it creates itself, so "build a new one" has to be reachable.
 *
 * Why the required scopes are locked rather than unchecked
 * -------------------------------------------------------
 * A delegated token request is all-or-nothing: ask for a scope the Admin
 * Console has not authorised and the WHOLE exchange fails, for every
 * feature. So deselecting a required scope does not produce a narrower
 * migration -- it produces a tenant that cannot migrate at all, diagnosed
 * much later as a delegation gap. The server unions them back in whatever
 * the UI sends; showing them as fixed is what keeps that from being a
 * surprise.
 */

const shortName = (scope: string) => scope.replace(/^https:\/\/www\.googleapis\.com\/auth\//, '')

export interface ReprovisionPanelProps {
  side: 'source' | 'target'
  domain: string
  busy?: boolean
  /** Called with the chosen scopes once the domain has been typed back. */
  onReprovision: (scopes: string[]) => void
}

export const ReprovisionPanel: React.FC<ReprovisionPanelProps> = ({
  side, domain, busy = false, onReprovision,
}) => {
  const [open, setOpen] = useState(false)
  const [opts, setOpts] = useState<ScopeOptions | null>(null)
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open || opts) return
    fetchScopeOptions(side)
      .then((o) => {
        setOpts(o)
        setChosen(new Set(o.default))
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [open, opts, side])

  const toggle = (scope: string) => {
    setChosen((prev) => {
      const next = new Set(prev)
      if (next.has(scope)) next.delete(scope)
      else next.add(scope)
      return next
    })
  }

  // An empty domain must never satisfy this.
  //
  // `confirm === domain` is true when BOTH are empty, which enabled the
  // button with nothing typed the moment the wizard did not know the domain
  // -- exactly the state a page loaded from a previous session is in. A
  // typed-confirmation gate that passes on an empty field is not a gate.
  const known = domain.trim().length > 0
  const confirmed = known
    && confirm.trim().toLowerCase() === domain.trim().toLowerCase()

  return (
    <Box sx={{ mt: 2, pt: 2, borderTop: '1px solid', borderColor: 'divider' }}
         data-testid="reprovision-panel">
      <Button size="small" startIcon={<ExpandIcon sx={{
        transform: open ? 'rotate(180deg)' : 'none', transition: '0.2s',
      }} />} onClick={() => setOpen((v) => !v)} data-testid="reprovision-toggle">
        Re-provision Cloud project
      </Button>

      <Collapse in={open}>
        <Box sx={{ mt: 1.5 }}>
          <Alert severity="warning" sx={{ mb: 2 }}>
            This builds a <strong>new</strong> Cloud project, service account
            and client ID for {domain}. The delegation currently in place is
            granted against the old client ID and stops applying — this run
            re-grants it, but don&apos;t do this while a migration is running.
            <br />
            Use it when the key on file belongs to a project this admin
            cannot administer.
          </Alert>

          {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

          {!known && (
            <Alert severity="info" sx={{ mb: 2 }} data-testid="domain-unknown">
              Enter the domain in the sign-in form above before re-provisioning
              — this needs to be confirmed by name, and the page does not know
              it yet.
            </Alert>
          )}

          {opts && (
            <>
              <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
                Scopes to grant
              </Typography>
              <Typography variant="caption" color="text.secondary"
                          sx={{ display: 'block', mb: 1 }}
                          data-testid="required-explainer">
                Required scopes are always granted. A token request fails
                entirely if any requested scope is missing, so leaving one out
                would break the migration rather than narrow it.
              </Typography>

              <Box sx={{ mb: 1 }} data-testid="required-scopes">
                <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.5 }}>
                  {opts.required.map((s) => (
                    <Chip key={s} size="small" label={shortName(s)}
                          variant="filled" />
                  ))}
                </Stack>
              </Box>

              {opts.optional.length > 0 && (
                <Box sx={{ mb: 2 }} data-testid="optional-scopes">
                  <Typography variant="caption" color="text.secondary"
                              sx={{ display: 'block', mb: 0.5 }}>
                    Optional — these unlock extra features and cost nothing if
                    unused
                  </Typography>
                  {opts.optional.map((s) => (
                    <FormControlLabel key={s} sx={{ display: 'block' }}
                      control={
                        <Checkbox size="small" checked={chosen.has(s)}
                                  onChange={() => toggle(s)}
                                  data-testid={`scope-${shortName(s)}`} />
                      }
                      label={<Typography variant="body2">{shortName(s)}</Typography>}
                    />
                  ))}
                </Box>
              )}
            </>
          )}

          <TextField size="small" fullWidth sx={{ mb: 1.5 }}
                     label={`Type ${domain} to confirm`}
                     value={confirm}
                     onChange={(e) => setConfirm(e.target.value)}
                     inputProps={{ 'data-testid': 'confirm-domain' }} />

          <Button variant="contained" color="warning"
                  startIcon={<ReprovisionIcon />}
                  disabled={busy || !confirmed || !opts}
                  data-testid="reprovision-go"
                  onClick={() => onReprovision(Array.from(chosen))}>
            Re-provision {side}
          </Button>
        </Box>
      </Collapse>
    </Box>
  )
}

export default ReprovisionPanel
