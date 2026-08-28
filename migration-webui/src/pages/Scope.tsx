import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, IconButton, Tooltip, Alert,
} from '@mui/material'
import { Refresh as RefreshIcon, Rule as ScopeIcon } from '@mui/icons-material'
import { fetchActions, fetchScope, ScopePayload, ActionSpec } from '@/api/client'
import JobRunner from '@/components/JobRunner'

/**
 * What this engine moves and at what fidelity, plus discovered volume --
 * real data (scope_payload(), already wired), just had zero frontend
 * surface before this. "Export SCOPE.md" (export_scope) is a real
 * existing ACTIONS entry, reused as-is via JobRunner.
 */
const Scope: React.FC = () => {
  const [scope, setScope] = useState<ScopePayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [actions, setActions] = useState<Record<string, ActionSpec>>({})

  const refresh = useCallback(() => {
    setLoading(true); setError(null)
    fetchScope()
      .then(setScope)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { refresh(); fetchActions().then(setActions) }, [refresh])

  return (
    <Box>
      <Stack direction="row" alignItems="center" sx={{ mb: 0.5 }}>
        <ScopeIcon color="action" sx={{ mr: 1 }} />
        <Typography variant="h4" sx={{ fontWeight: 700, flexGrow: 1 }}>Scope</Typography>
        <Tooltip title="Re-check">
          <span>
            <IconButton size="small" onClick={refresh} disabled={loading}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        What migrates, at what fidelity, and the discovered volume behind those numbers.
      </Typography>

      {error && <Alert severity="warning" sx={{ mb: 3 }}>{error}</Alert>}

      {(actions.export_scope || actions.scope) && (
        <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
          <CardContent>
            <Stack spacing={2}>
              {/* "scope" prints what migrates AND the exact OAuth scopes this
                  configuration needs -- the list to paste into the Admin
                  console. It had an ACTIONS entry and no button anywhere. */}
              {actions.scope && <JobRunner name="scope" spec={actions.scope} />}
              {actions.export_scope &&
                <JobRunner name="export_scope" spec={actions.export_scope} />}
            </Stack>
          </CardContent>
        </Card>
      )}

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent>
          {scope ? (
            <Box component="pre" sx={{
              fontSize: 12.5, fontFamily: 'ui-monospace, monospace',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word', m: 0,
            }}>
              {scope.lines.join('\n')}
            </Box>
          ) : (
            <Typography variant="body2" color="text.secondary">
              {loading ? 'Loading…' : 'Nothing to show.'}
            </Typography>
          )}
        </CardContent>
      </Card>
    </Box>
  )
}

export default Scope
