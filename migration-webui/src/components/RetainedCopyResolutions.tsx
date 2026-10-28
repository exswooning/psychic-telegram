import React from 'react'
import { Box, Link, Typography } from '@mui/material'
import type { RetainedCopyResolution } from '@/api/controlPlane'

const contentLabels: Record<string, string> = {
  MATCHED_BYTES: 'Content matched byte for byte',
  MATCHED_CHECKSUM: 'File size and checksum matched',
  MATCHED_SUPPORTED_NATIVE: 'Supported native feature checks matched',
  MATCHED_EXPORT: 'Export comparison matched (limited scope)',
  DIFFERENCES: 'Content differences remain',
  NOT_VERIFIED: 'Content equality not verified',
}

export default function RetainedCopyResolutions({ rows }: { rows?: RetainedCopyResolution[] | null }) {
  if (!rows?.length) return null
  return <Box sx={{ my: 1 }} aria-label="Retained target copies">
    {rows.slice(0, 8).map((r) => <Box key={`${r.source}:${r.target}`} sx={{ mb: 1 }}>
      <Typography variant="body2" fontWeight={600}>{r.label}</Typography>
      {r.state === 'COPY_EXISTS' && <Link href={`https://drive.google.com/file/d/${encodeURIComponent(r.target)}/view`}
        target="_blank" rel="noopener noreferrer">Open retained copy{r.name ? `: ${r.name}` : ''}</Link>}
      <Typography variant="caption" display="block">{contentLabels[r.contentState] ?? 'Content equality not verified'} — {r.contentScope}</Typography>
      <Typography variant="caption" display="block" color="text.secondary">
        Original sharing and authorization remain unresolved. Checked {new Date(r.checkedAt).toLocaleString()}.
      </Typography>
    </Box>)}
    {rows.length > 8 && <Typography variant="caption">{rows.length - 8} additional copy checks are in the full report.</Typography>}
  </Box>
}
