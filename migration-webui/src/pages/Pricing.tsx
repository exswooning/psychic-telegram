import React from 'react'
import { Link as RouterLink } from 'react-router-dom'
import {
  Box, Container, Typography, Stack, Button, Paper, Chip, Link,
  List, ListItem, ListItemIcon, ListItemText,
} from '@mui/material'
import { RocketLaunch as BrandIcon, Check as CheckIcon } from '@mui/icons-material'

interface Tier {
  id: string
  name: string
  tagline: string
  highlight?: boolean
  features: string[]
}

const TIERS: Tier[] = [
  {
    id: 'starter',
    name: 'Starter',
    tagline: 'One migration project, done right.',
    features: [
      'One source + one target tenant',
      'Drive, Gmail, and Calendar migration',
      'Coverage audit and ACL fidelity verification',
      'Functional OAuth scope checks, not a checkbox',
      'Email support',
    ],
  },
  {
    id: 'growth',
    name: 'Growth',
    tagline: 'For agencies running several migrations.',
    highlight: true,
    features: [
      'Everything in Starter',
      'Chat, Contacts, and Tasks migration',
      'Shared Drives, with per-drive ACL fidelity',
      'Full benchmark suite (throughput + fidelity gates)',
      'Priority support',
    ],
  },
  {
    id: 'scale',
    name: 'Scale',
    tagline: 'For MSPs running migrations concurrently.',
    features: [
      'Everything in Growth',
      'Isolated tenant config, keys, and database per account',
      'Concurrent migrations — no queueing behind other work',
      'Dedicated onboarding',
      'Support SLA',
    ],
  },
]

const Pricing: React.FC = () => {
  return (
    <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
      <Box sx={{ borderBottom: '1px solid', borderColor: 'divider', py: 2 }}>
        <Container maxWidth="lg">
          <Stack direction="row" alignItems="center" justifyContent="space-between">
            <Stack direction="row" spacing={1.25} alignItems="center" component={RouterLink}
                   to="/login" sx={{ textDecoration: 'none', color: 'inherit' }}>
              <Box sx={{
                width: 32, height: 32, borderRadius: '50%', bgcolor: 'primary.main',
                color: 'primary.contrastText', display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                <BrandIcon sx={{ fontSize: 18 }} />
              </Box>
              <Typography sx={{ fontFamily: '"Plus Jakarta Sans", sans-serif', fontWeight: 700, fontSize: 18 }}>
                Bitport
              </Typography>
            </Stack>
            <Link component={RouterLink} to="/login" underline="hover" sx={{ fontWeight: 500 }}>
              Sign in
            </Link>
          </Stack>
        </Container>
      </Box>

      <Container maxWidth="lg" sx={{ py: { xs: 6, md: 9 } }}>
        <Stack spacing={1.5} sx={{ textAlign: 'center', mb: 6 }} alignItems="center">
          <Typography variant="h3" sx={{ fontWeight: 700, maxWidth: '20ch' }}>
            Migrate with proof, not promises
          </Typography>
          <Typography variant="body1" color="text.secondary" sx={{ maxWidth: '55ch' }}>
            Every plan gets its own isolated tenant config, keys, and migration
            ledger. No card required to start — billing goes live before your
            trial ends, and you'll be asked first.
          </Typography>
        </Stack>

        <Box sx={{
          display: 'grid', gap: 3,
          gridTemplateColumns: { xs: '1fr', md: 'repeat(3, 1fr)' },
          alignItems: 'stretch',
        }}>
          {TIERS.map((tier) => (
            <Paper
              key={tier.id}
              variant="outlined"
              sx={{
                p: 4, borderRadius: 3, display: 'flex', flexDirection: 'column', gap: 2.5,
                position: 'relative',
                borderColor: tier.highlight ? 'primary.main' : 'divider',
                borderWidth: tier.highlight ? 2 : 1,
              }}
            >
              {tier.highlight && (
                <Chip label="Most popular" color="primary" size="small"
                      sx={{ position: 'absolute', top: -12, left: 24, fontWeight: 600 }} />
              )}
              <Box>
                <Typography variant="h5" sx={{ fontWeight: 700 }}>{tier.name}</Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                  {tier.tagline}
                </Typography>
              </Box>

              <List dense disablePadding sx={{ flexGrow: 1 }}>
                {tier.features.map((f) => (
                  <ListItem key={f} disableGutters sx={{ py: 0.5 }}>
                    <ListItemIcon sx={{ minWidth: 30 }}>
                      <CheckIcon sx={{ fontSize: 18, color: 'secondary.main' }} />
                    </ListItemIcon>
                    <ListItemText primary={f} primaryTypographyProps={{ fontSize: 14 }} />
                  </ListItem>
                ))}
              </List>

              <Button
                component={RouterLink} to={`/signup?plan=${tier.id}`}
                variant={tier.highlight ? 'contained' : 'outlined'} size="large" fullWidth
                sx={{ py: 1.25 }}
              >
                Start free trial
              </Button>
            </Paper>
          ))}
        </Box>

        <Typography variant="body2" color="text.secondary" sx={{ textAlign: 'center', mt: 5 }}>
          Not sure which plan fits? Start on Starter — every plan is the same product,
          just scoped to how many tenants you're actively moving.
        </Typography>
      </Container>
    </Box>
  )
}

export default Pricing
