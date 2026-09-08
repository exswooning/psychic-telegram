/**
 * Whether the target can actually receive this migration, asked before it
 * starts.
 *
 * An unlicensed target account has no Drive and no Gmail. Google says so
 * with 401 "Active session is invalid. Error code: 4" and 400 "Mail service
 * not enabled" -- neither of which contains the word licence. Live, a tenant
 * holding 201 accounts against 200 seats produced exactly that, twice, and
 * it was read as an outage both times.
 *
 * Counted per PAIR, not per tenant: what matters is whether the specific
 * target address a source user is mapped to holds a licence. How many seats
 * the tenant owns in total is not readable at all without the Reseller API,
 * which a directly-managed customer does not have -- so this shows what is
 * knowable and says so when it cannot read even that.
 */
import React, { useEffect, useState } from 'react'
import {
  Alert, AlertTitle, Box, Chip, Collapse, Link, Paper, Stack, Typography,
} from '@mui/material'
import { fetchLicencePreflight } from '@/api/client'
import type { LicencePreflight } from '@/api/client'

const Count: React.FC<{ label: string; value: React.ReactNode; hint?: string }> = ({
  label, value, hint,
}) => (
  <Box sx={{ minWidth: 130 }}>
    <Typography variant="caption" color="text.secondary">{label}</Typography>
    <Typography sx={{ fontWeight: 700, fontSize: 20, lineHeight: 1.2,
                      fontVariantNumeric: 'tabular-nums' }}>
      {value}
    </Typography>
    {hint && <Typography variant="caption" color="text.disabled">{hint}</Typography>}
  </Box>
)

export const LicenceReadiness: React.FC = () => {
  const [pf, setPf] = useState<LicencePreflight | null>(null)
  const [showList, setShowList] = useState(false)

  useEffect(() => { fetchLicencePreflight().then(setPf).catch(() => setPf(null)) }, [])
  if (!pf) return null

  // Cannot read licences at all. Saying so is the honest answer; rendering a
  // confident 0 shortfall from an empty list is not.
  const blind = !!(pf.error || pf.targetError)
  const short = pf.shortfall > 0

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 2 }} data-testid="licence-readiness">
      <Typography variant="overline" color="text.secondary">
        Licences on {pf.targetDomain || 'the target'}
      </Typography>

      {blind ? (
        <Alert severity="info" sx={{ mt: 1 }}>
          <AlertTitle>Licences could not be read</AlertTitle>
          {pf.error || pf.targetError}
          <Box sx={{ mt: 0.5 }}>
            Nothing here is a verdict on the migration — this check simply
            could not run. Grant the licensing scope to see it.
          </Box>
        </Alert>
      ) : (
        <>
          <Stack direction="row" flexWrap="wrap" gap={3} sx={{ mt: 1, mb: short ? 2 : 0 }}>
            <Count label="Users to migrate" value={pf.pairs.toLocaleString()}
                   hint={pf.sourceDomain} />
            <Count label="Licensed on target"
                   value={pf.targetLicensed.toLocaleString()}
                   hint={pf.targetDomain} />
            <Count label="Would migrate nothing"
                   value={
                     <Box component="span" sx={{ color: short ? 'error.main' : 'success.main' }}>
                       {pf.shortfall.toLocaleString()}
                     </Box>}
                   hint={short ? 'target has no licence' : 'every target is licensed'} />
          </Stack>

          {short && (
            <Alert severity="warning">
              <AlertTitle>
                {pf.shortfall.toLocaleString()} of {pf.pairs.toLocaleString()} users
                are mapped to a target account with no licence
              </AlertTitle>
              An account with no licence has no Drive and no Gmail, so those
              users migrate nothing. The errors will not mention licensing —
              Google answers <code>401 Active session is invalid</code> and{' '}
              <code>400 Mail service not enabled</code>. If the tenant is out
              of seats, assigning one fails with{' '}
              <code>412 There aren’t enough available licenses</code>.

              <Typography sx={{ fontWeight: 600, mt: 1.5 }}>
                Two ways forward
              </Typography>
              <Box component="ul" sx={{ pl: 2.5, m: 0, mt: 0.5 }}>
                <li>
                  <strong>Migrate fewer users.</strong> Buy or free seats for the
                  ones that matter now, and leave the rest mapped but
                  unlicensed — they stay PENDING and are picked up by a later
                  run, not marked done.
                </li>
                <li>
                  <strong>Merge several source users onto one target.</strong>{' '}
                  Point more than one source address at the same licensed
                  target in the identity map. Each one’s files are nested under
                  a folder named for them, so nothing interleaves.
                </li>
              </Box>

              <Link component="button" type="button" underline="hover"
                    sx={{ mt: 1.5, display: 'block' }}
                    onClick={() => setShowList((v) => !v)}>
                {showList ? 'Hide' : 'Show'} the {pf.unlicensedTargets.length} unlicensed
                target{pf.unlicensedTargets.length === 1 ? '' : 's'}
              </Link>
              <Collapse in={showList}>
                <Box sx={{ mt: 1, maxHeight: 180, overflowY: 'auto' }}>
                  {pf.unlicensedTargets.map((t) => (
                    <Chip key={t} label={t} size="small"
                          sx={{ mr: 0.5, mb: 0.5 }} />
                  ))}
                </Box>
              </Collapse>
            </Alert>
          )}

          {pf.mergedTargets.length > 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
              Already merging:{' '}
              {pf.mergedTargets.map((m) => `${m.target} (${m.sources})`).join(', ')}
            </Typography>
          )}
        </>
      )}
    </Paper>
  )
}

export default LicenceReadiness
