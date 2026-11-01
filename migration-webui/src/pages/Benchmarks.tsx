import React, { useEffect, useState } from 'react'
import { Alert, Box, Card, CardContent, Stack, Typography } from '@mui/material'
import { useSearchParams } from 'react-router-dom'
import AbBenchmarkPanel from '@/components/AbBenchmarkPanel'
import BenchmarkRunner from '@/components/BenchmarkRunner'
import JobRunner from '@/components/JobRunner'
import { fetchActions, type ActionSpec } from '@/api/client'

/** The regression set (regression.py): only for the account picked in "Viewing account" -- it
 *  makes new target accounts, so it never falls back to whichever account the session is on. */
const RegressionRunner: React.FC = () => {
  const [params] = useSearchParams()
  const account = Number(params.get('account')) || undefined
  const [spec, setSpec] = useState<ActionSpec | null>(null)
  useEffect(() => {
    Promise.resolve().then(() => fetchActions())
      .then((a) => setSpec(a?.regression ?? null)).catch(() => {})
  }, [])
  if (!spec) return null
  return (
    <Card variant="outlined" data-testid="regression-runner">
      <CardContent>
        <Typography variant="h6" sx={{ fontWeight: 700 }}>Regression set</Typography>
        {account === undefined
          ? <Alert severity="info" sx={{ mt: 1 }}>Pick the account in “Viewing account” first.</Alert>
          : <Box sx={{ mt: 1 }}><JobRunner name="regression" spec={spec} accountId={account} /></Box>}
      </CardContent>
    </Card>
  )
}

/** Every benchmark on one page, with live progress, so nobody has to ask how a run is going. */
const Benchmarks: React.FC = () => (
  <Box>
    <Typography variant="h5" sx={{ fontWeight: 700, mb: 2 }}>Benchmarks</Typography>
    <Stack spacing={3}>
      <AbBenchmarkPanel />
      <RegressionRunner />
      <BenchmarkRunner />
    </Stack>
  </Box>
)

export default Benchmarks
