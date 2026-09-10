/**
 * Which tenant to count.
 *
 * The inventory was wired to whichever side its host panel happened to be
 * -- source inside the source panel, target inside the target one -- so
 * "what is actually in that domain?" could only be asked from a page you
 * had already navigated to for another reason, and never for the other
 * tenant from where you were standing.
 *
 * Counting is read-only. There is no reason to make it hard to aim.
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Card, CardContent, MenuItem, Stack, TextField, Typography,
} from '@mui/material'
import {
  fetchTenantInventory, fetchTenantConfigStatus,
} from '@/api/controlPlane'
import type { TenantInventory, TenantConfigStatus } from '@/api/controlPlane'
import TenantInventoryPanel from '@/components/TenantInventoryPanel'

export const InventoryPicker: React.FC<{ initialSide?: 'source' | 'target' }> =
  ({ initialSide = 'source' }) => {
    const [side, setSide] = useState<'source' | 'target'>(initialSide)
    const [tenants, setTenants] = useState<Record<string, TenantConfigStatus>>({})
    const [inv, setInv] = useState<TenantInventory | null>(null)
    const [busy, setBusy] = useState(false)
    const [error, setError] = useState('')

    useEffect(() => {
      (['source', 'target'] as const).forEach((s) => {
        fetchTenantConfigStatus(s)
          .then((cfg) => setTenants((prev) => ({ ...prev, [s]: cfg })))
          .catch(() => {})
      })
    }, [])

    const load = useCallback(async (deep: boolean) => {
      setBusy(true); setError('')
      try {
        // The picked side, not the panel's. Passing the wrong one here is
        // how a count of the destination gets read as a count of the source.
        setInv(await fetchTenantInventory(side, 250, deep))
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusy(false)
      }
    }, [side])

    // Clear on switch. Leaving the previous tenant's numbers on screen under
    // a newly-chosen domain is worse than showing nothing: they look like
    // an answer to the question just asked.
    useEffect(() => { setInv(null); setError('') }, [side])

    const label = (s: 'source' | 'target') => {
      const d = tenants[s]?.domain
      return d ? `${d} (${s})` : `${s} — not configured`
    }

    return (
      <Card variant="outlined" sx={{ borderRadius: 2, mb: 3 }}
            data-testid="inventory-picker">
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>
            Tenant inventory
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Counts what is actually in a tenant — accounts, mail, Drive.
            Read-only: it reads the tenant and writes nothing.
          </Typography>

          <Stack direction="row" spacing={2} alignItems="center"
                 sx={{ mb: 1, flexWrap: 'wrap', gap: 1 }}>
            <TextField select size="small" label="Domain" value={side}
                       sx={{ minWidth: 280 }}
                       onChange={(e) => setSide(e.target.value as 'source' | 'target')}
                       inputProps={{ 'data-testid': 'inventory-side' }}>
              {(['source', 'target'] as const).map((s) => (
                <MenuItem key={s} value={s} disabled={!tenants[s]?.domain}>
                  {label(s)}
                </MenuItem>
              ))}
            </TextField>
          </Stack>

          <Box data-testid="inventory-for">
            <TenantInventoryPanel
              inv={inv} busy={busy} error={error}
              domain={tenants[side]?.domain || side}
              onRefresh={() => load(false)}
              onDeepScan={() => load(true)} />
          </Box>
        </CardContent>
      </Card>
    )
  }

export default InventoryPicker
