import React from 'react'
import { Box, Stack, Typography } from '@mui/material'
import AbBenchmarkPanel from '@/components/AbBenchmarkPanel'
import BenchmarkRunner from '@/components/BenchmarkRunner'

/** Every benchmark on one page, with live progress, so nobody has to ask how a run is going. */
const Benchmarks: React.FC = () => (
  <Box>
    <Typography variant="h5" sx={{ fontWeight: 700, mb: 2 }}>Benchmarks</Typography>
    <Stack spacing={3}>
      <AbBenchmarkPanel />
      <BenchmarkRunner />
    </Stack>
  </Box>
)

export default Benchmarks
