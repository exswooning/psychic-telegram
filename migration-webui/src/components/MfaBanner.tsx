/**
 * The 2-Step prompt, put where the person holding the phone can see it.
 *
 * Every browser sign-in this tool drives runs headless on the server's
 * virtual display. When Google answers the password with "Check your phone
 * -- tap 47", that screen is rendered to a framebuffer nobody is watching:
 * the setup simply stops making progress for up to ten minutes and then
 * reports "likely 2FA/captcha", which is a guess about something it was
 * looking straight at.
 *
 * So the sign-in loops read the prompt off the page, full_setup writes it
 * into the progress checkpoint, and this renders it. Not an Alert among
 * other alerts -- while this is up, it is the only thing on the page that
 * can move, and every second it goes unnoticed is a second of a timeout
 * nobody can get back.
 */
import React from 'react'
import { Box, Button, Stack, Typography, CircularProgress } from '@mui/material'
import { PhonelinkRing as PhoneIcon } from '@mui/icons-material'

export const MfaBanner: React.FC<{ challenge?: string | null }> = ({ challenge }) => {
  if (!challenge) return null
  // The prompt arrives as "Check your phone / Google sent a notification to
  // your Pixel 7 ... tap 47 / 47" -- the joined lines Google itself showed.
  // Split back out so the first line can be the headline it already is.
  const [head, ...rest] = challenge.split(' / ')
  return (
    <Box
      data-testid="mfa-banner"
      role="alert"
      sx={{
        mb: 3, p: 3, borderRadius: 4,
        border: '2px solid', borderColor: 'warning.main',
        bgcolor: 'warning.light',
      }}>
      <Stack direction="row" spacing={2} alignItems="flex-start">
        <PhoneIcon sx={{ color: 'warning.dark', fontSize: 32, mt: 0.5 }} />
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ fontSize: '1.25rem', fontWeight: 500,
                            color: 'warning.dark', mb: 0.5 }}>
            {head}
          </Typography>
          {rest.map((line) => (
            <Typography key={line} variant="body2"
                        sx={{ color: 'warning.dark', lineHeight: 1.6 }}>
              {line}
            </Typography>
          ))}
          <Typography variant="body2"
                      sx={{ color: 'warning.dark', mt: 1.5, fontWeight: 500 }}>
            Answer this on your own device. If it wants something typed into
            the sign-in page itself (a phone number, a code), open the server's
            browser and type it there.
          </Typography>
          {/* x11vnc watches the display the sign-in runs on, bound to the server's
              localhost; connect_vps.sh tunnels it to this Mac's 5900. */}
          <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mt: 1.5, flexWrap: 'wrap', gap: 1 }}>
            <Button size="small" variant="contained" color="warning" href="vnc://localhost:5900"
                    data-testid="mfa-open-server-browser">
              Open the server's browser
            </Button>
            <Typography variant="caption" sx={{ color: 'warning.dark' }}>
              Run ./connect_vps.sh on this Mac first; the password is in /root/.vnc/passwd on the server.
            </Typography>
          </Stack>
        </Box>
        <CircularProgress size={20} sx={{ color: 'warning.dark', mt: 1 }} />
      </Stack>
    </Box>
  )
}

export default MfaBanner
