import React, { useEffect, useRef, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, TextField, Button, Alert,
  LinearProgress, Chip,
} from '@mui/material'
import { DeleteForever as TeardownIcon } from '@mui/icons-material'
import { startTeardown, fetchTeardownStatus, TeardownStatus } from '@/api/controlPlane'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

/**
 * Superadmin-only: delete a GCP project and/or revoke a domain-wide
 * delegation entry -- the reverse of Quick Setup. Built to replace the
 * manual SSH-plus-Playwright cleanup this exact cleanup work required
 * before teardown_tenant.py existed (see provision_gcp.delete_project()
 * and dwd_helper.revoke(), both new this pass). Either field can be left
 * blank to do only the other half.
 */
const GcpTeardown: React.FC = () => {
  const [project, setProject] = useState('')
  const [clientId, setClientId] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<TeardownStatus | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const poll = () => {
    return fetchTeardownStatus().then(setStatus).catch(() => {})
  }

  useEffect(() => {
    poll()
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  useEffect(() => {
    if (status?.running && !pollRef.current) {
      pollRef.current = setInterval(poll, 3000)
    } else if (!status?.running && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [status?.running])

  const canLaunch = (project.trim() || clientId.trim()) && email.trim() && password && !status?.running

  const launch = async (reason: string) => {
    setBusy(true); setError(null)
    try {
      const r = await startTeardown(reason, email.trim(), password, {
        project: project.trim(), clientId: clientId.trim(),
      })
      if (!r.ok) throw new Error(r.detail || 'could not start')
      setAsk(false)
      await poll()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setPassword('')
      setBusy(false)
    }
  }

  const result = status?.result

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <TeardownIcon color="action" />
        <Typography variant="h4" sx={{ fontWeight: 700 }}>GCP Teardown</Typography>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Delete a throwaway GCP project and/or revoke its domain-wide
        delegation entry. The project delete is soft (30-day recovery); the
        delegation revoke is not.
      </Typography>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent>
          <Stack spacing={2} sx={{ maxWidth: 480 }}>
            <TextField size="small" label="GCP project ID (optional)"
                       placeholder="wsmig-src-12345"
                       value={project} onChange={(e) => setProject(e.target.value)} />
            <TextField size="small" label="DWD client ID (optional)"
                       placeholder="107479933434636662752"
                       value={clientId} onChange={(e) => setClientId(e.target.value)} />
            <TextField size="small" label="Super admin email"
                       value={email} onChange={(e) => setEmail(e.target.value)} />
            <TextField size="small" label="Super admin password" type="password"
                       value={password} onChange={(e) => setPassword(e.target.value)} />
            <Button variant="outlined" color="error" disabled={!canLaunch}
                    onClick={() => setAsk(true)}>
              {status?.running ? 'Running…' : 'Tear down'}
            </Button>
          </Stack>

          {status?.running && (
            <Box sx={{ mt: 3, maxWidth: 480 }}>
              {typeof status.progressPct === 'number' ? (
                <LinearProgress variant="determinate" value={status.progressPct} />
              ) : (
                <LinearProgress />
              )}
              <Stack direction="row" justifyContent="space-between" sx={{ mt: 0.5 }}>
                <Typography variant="caption" color="text.secondary">
                  {status.progressLabel || 'working…'}
                </Typography>
                {typeof status.progressPct === 'number' && (
                  <Typography variant="caption" color="text.secondary"
                              sx={{ fontVariantNumeric: 'tabular-nums' }}>
                    {status.progressPct}%
                  </Typography>
                )}
              </Stack>
            </Box>
          )}

          {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}

          {result && (
            <Box sx={{ mt: 3 }}>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>Result</Typography>
                <Chip size="small" label={result.ok ? 'ok' : 'failed'}
                      color={result.ok ? 'success' : 'error'}
                      variant={result.ok ? 'outlined' : 'filled'} />
              </Stack>
              <Box component="pre" sx={{
                fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
                overflowX: 'auto', maxHeight: 260, whiteSpace: 'pre-wrap', m: 0,
              }}>
                {result.phases.map((p) =>
                  `${p.status === 'ok' ? 'ok  ' : p.status === 'failed' ? 'FAIL' : '..  '} `
                  + `${p.name}${p.detail ? '  ' + p.detail : ''}`
                ).join('\n')}
              </Box>
            </Box>
          )}
        </CardContent>
      </Card>

      <ReasonCodeDialog
        open={ask} busy={busy} error={error}
        destructive confirmPhrase="TEARDOWN"
        title="Tear down GCP project / DWD delegation"
        description={
          <>
            {project.trim() && <>Deletes project <strong>{project.trim()}</strong> (soft-deleted, recoverable for 30 days). </>}
            {clientId.trim() && <>Revokes client ID <strong>{clientId.trim()}</strong>'s domain-wide delegation entry (not undoable). </>}
            Opens a browser and signs in as <strong>{email || 'the admin'}</strong> to do it. The password is used once and never stored.
          </>
        }
        onCancel={() => { setAsk(false); setError(null) }}
        onConfirm={launch}
      />
    </Box>
  )
}

export default GcpTeardown
