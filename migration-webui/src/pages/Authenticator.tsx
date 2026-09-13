/**
 * Where a 2-Step seed gets stored, ahead of needing it.
 *
 * This lived only inside the wizard, and only once a 2-Step prompt had
 * already appeared -- so the moment you could reach it was the moment a
 * browser session was timing out waiting for the code. Setting up a second
 * factor is preparation, not something to do under a stopwatch, so it has
 * its own page and the wizard keeps a copy for when the prompt lands.
 */
import React from 'react'
import { Alert, Box, Link, Stack, Typography } from '@mui/material'
import { Key as KeyIcon } from '@mui/icons-material'
import AuthenticatorCode from '@/components/AuthenticatorCode'

export const Authenticator: React.FC = () => (
  <Box sx={{ p: 3 }}>
    <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
      <KeyIcon color="action" />
      <Typography variant="h5" sx={{ fontWeight: 700 }}>Authenticator</Typography>
    </Stack>
    <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
      2-Step codes for the admin accounts this tool signs in as. Store a seed
      here <strong>before</strong> running setup — the wizard drives a real
      browser through Google&apos;s sign-in, and a prompt it cannot answer is
      what stops an unattended run.
    </Typography>

    <Box sx={{ maxWidth: 760 }}>
      <AuthenticatorCode allowAdd />

      <Alert severity="info" sx={{ mt: 3 }}>
        <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>
          Where to find the setup key
        </Typography>
        <Typography variant="body2" component="div">
          In the Google account you sign in as:{' '}
          <Link href="https://myaccount.google.com/signinoptions/two-step-verification"
                target="_blank" rel="noopener">
            Security → 2-Step Verification → Authenticator
          </Link>
          , then <em>Can&apos;t scan it?</em> to see the key as text. That is
          the same secret the QR code carries — a phone app and this page
          generate identical codes from it, so adding one here does not
          replace or disturb an existing app.
        </Typography>
      </Alert>
    </Box>
  </Box>
)

export default Authenticator
