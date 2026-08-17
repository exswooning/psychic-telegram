import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, TextField, Button, Alert,
  Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Chip,
  IconButton, Tooltip,
} from '@mui/material'
import { Refresh as RefreshIcon, People as IdentitiesIcon } from '@mui/icons-material'
import { fetchActions, fetchIdentities, saveIdentityPair, IdentityRow, ActionSpec } from '@/api/client'
import JobRunner from '@/components/JobRunner'

/**
 * Operator/superadmin-only: what init-db has actually loaded
 * (identity_map, the read side) plus what it will load next time
 * (identities.csv, via init_db/init_db_auto below -- the write side).
 * Replaces the legacy dashboard's identities tab, which called
 * GET /api/identities and POST /api/identities/save -- neither route
 * existed server-side, so this capability was never actually shipped.
 */
const Identities: React.FC = () => {
  const [rows, setRows] = useState<IdentityRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [actions, setActions] = useState<Record<string, ActionSpec>>({})
  const [source, setSource] = useState('')
  const [target, setTarget] = useState('')
  const [addErr, setAddErr] = useState<string | null>(null)
  const [addOk, setAddOk] = useState<string | null>(null)

  const refresh = useCallback(() => {
    setLoading(true); setError(null)
    fetchIdentities()
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { refresh(); fetchActions().then(setActions) }, [refresh])

  const addPair = async () => {
    setAddErr(null); setAddOk(null)
    const r = await saveIdentityPair(source, target)
    if (r.ok) {
      setAddOk(`saved -- ${r.total} pair(s) in identities.csv`)
      setSource(''); setTarget('')
    } else {
      setAddErr(r.error || 'could not save')
    }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" sx={{ mb: 0.5 }}>
        <IdentitiesIcon color="action" sx={{ mr: 1 }} />
        <Typography variant="h4" sx={{ fontWeight: 700, flexGrow: 1 }}>Identities</Typography>
        <Tooltip title="Re-check">
          <span>
            <IconButton size="small" onClick={refresh} disabled={loading}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        The source→target user mapping every migration action needs.
      </Typography>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 1.5 }}>Load into identity_map</Typography>
          <Stack spacing={3}>
            {actions.init_db_auto && (
              <JobRunner name="init_db_auto" spec={actions.init_db_auto} onDone={refresh} />
            )}
            {actions.init_db && (
              <JobRunner name="init_db" spec={actions.init_db} onDone={refresh} />
            )}
          </Stack>
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 1.5 }}>Add one pair by hand</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Appends to identities.csv -- takes effect next time either
            action above runs, not immediately.
          </Typography>
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
            <TextField size="small" label="Source email" value={source}
                       onChange={(e) => setSource(e.target.value)} sx={{ width: 260 }} />
            <TextField size="small" label="Target email" value={target}
                       onChange={(e) => setTarget(e.target.value)} sx={{ width: 260 }} />
            <Button variant="contained" disabled={!source || !target} onClick={addPair}>
              Add pair
            </Button>
          </Stack>
          {addOk && <Alert severity="success" sx={{ mt: 2 }}>{addOk}</Alert>}
          {addErr && <Alert severity="error" sx={{ mt: 2 }}>{addErr}</Alert>}
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
            Currently loaded ({rows.length})
          </Typography>
          {error && <Alert severity="warning" sx={{ mb: 2 }}>{error}</Alert>}
          <TableContainer sx={{ maxHeight: 480 }}>
            <Table stickyHeader size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600 }}>Source</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Target</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Type</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.source_email}>
                    <TableCell>{r.source_email}</TableCell>
                    <TableCell>{r.target_email}</TableCell>
                    <TableCell>{r.entity_type}</TableCell>
                    <TableCell>
                      <Chip size="small" label={r.status} variant="outlined"
                            color={r.status === 'DONE' ? 'success' : r.status === 'FAILED' ? 'error' : 'default'} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          {!loading && rows.length === 0 && !error && (
            <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: 'center' }}>
              Nothing loaded yet -- run one of the actions above.
            </Typography>
          )}
        </CardContent>
      </Card>
    </Box>
  )
}

export default Identities
