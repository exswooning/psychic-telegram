import React, { useEffect, useState } from 'react'
import { Chip } from '@mui/material'
import { fetchLifecycle, LifecycleView } from '@/api/controlPlane'
import { lifecycleLabel } from '@/utils/lifecycle'

/** One account's end-of-life state as a chip -- Accounts (admin) and GCP Teardown. */
const LifecycleChip: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [view, setView] = useState<LifecycleView | null>(null)
  useEffect(() => {
    fetchLifecycle(accountId).then(setView).catch(() => setView(null))
  }, [accountId])
  if (!view) return null
  const l = lifecycleLabel(view.state)
  return <Chip size="small" variant="outlined" color={l.tone} label={l.label} title={l.title}
               data-testid={`lifecycle-${accountId}`} />
}

export default LifecycleChip
