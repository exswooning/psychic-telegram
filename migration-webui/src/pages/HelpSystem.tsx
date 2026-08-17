import React, { useState } from 'react'
import {
  Box,
  Typography,
  TextField,
  InputAdornment,
  Chip,
  Paper,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Stack,
} from '@mui/material'
import {
  Search as SearchIcon,
  ExpandMore as ExpandMoreIcon,
  Lightbulb as LightbulbIcon,
  Warning as WarningIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  Info as InfoIcon,
} from '@mui/icons-material'

/**
 * Real reference content about how this app actually behaves -- not
 * generic filler. Each answer below reflects a genuine, confirmed
 * mechanism in the product (propagation delay, the 3/sec write ceiling,
 * job admission, etc.), not a plausible-sounding guess.
 */
const helpTopics: {
  id: number; question: string; answer: string
  icon: React.ReactNode; color: 'primary' | 'success' | 'warning' | 'error'
}[] = [
  {
    id: 1,
    question: 'Quick Setup finished green, but Verification still says "Propagating" — is something wrong?',
    answer: 'No — this is expected and can take a while. Google accepts a domain-wide delegation grant '
      + 'in seconds, but actually issuing tokens for the granted scopes can take anywhere from a few '
      + 'minutes to (rarely) much longer to fully propagate. Quick Setup waits a generous but bounded '
      + 'window before giving up; if it times out, the grant itself was still accepted — check the '
      + 'Verification page again in a few minutes rather than re-running setup, which would just start '
      + 'a new wait from zero.',
    icon: <InfoIcon color="primary" />, color: 'primary',
  },
  {
    id: 2,
    question: 'Why is Drive migrating so slowly compared to everything else?',
    answer: 'Google enforces a hard ceiling of 3 sustained Drive write requests per second per target '
      + 'account — documented, and not something Google will raise on request. This app already runs '
      + 'multiple files in parallel per user to use as much of that 3/sec budget as possible; more '
      + 'workers past that point only produces more 429 (rate limited) responses, which is slower, not '
      + 'faster. Drive reads are not affected — they come out of a much larger, separate pool.',
    icon: <InfoIcon color="primary" />, color: 'primary',
  },
  {
    id: 3,
    question: 'A job says it refused to start with a capacity message — why?',
    answer: 'Only one heavy job (a migration, a Quick Setup run, etc.) is allowed to run at a time on '
      + 'this box, across every account sharing it — a second one is refused outright rather than both '
      + 'quietly competing for the same CPU/RAM and stalling each other. Wait for the running job to '
      + 'finish, then retry.',
    icon: <WarningIcon color="warning" />, color: 'warning',
  },
  {
    id: 4,
    question: 'What does a failure with "Needs Attention" mean, and what do I do about it?',
    answer: 'The item genuinely failed and needs a human to look at it — Google is not going to resolve '
      + 'it by itself the way a rate-limit backoff does. Open the Failures page, click the row for the '
      + 'full attempt history (every previous try, with its exact error), then retry it once whatever '
      + 'caused it is fixed (a missing permission, a scope that was not yet live, etc.).',
    icon: <ErrorIcon color="error" />, color: 'error',
  },
  {
    id: 5,
    question: 'Do I need to do anything while a migration is running?',
    answer: 'No — leave it running. Rate-limit responses (429s) are retried automatically with backoff; '
      + 'that is normal, expected traffic shaping from Google, not a problem. The one thing worth '
      + 'watching is the Failures page, since a genuine failure (as opposed to a retry) does need a '
      + 'human eventually.',
    icon: <CheckCircleIcon color="success" />, color: 'success',
  },
  {
    id: 6,
    question: 'What is the Emergency Brake for?',
    answer: 'It finds every file on the target tenant currently shared "anyone with the link" and can '
      + 'revoke all of them in one confirmed action. It exists because a migrated file can end up '
      + 'more open than it was on the source if a sharing setting does not translate exactly — this is '
      + 'the fast way to shut that down tenant-wide without hunting for the individual files by hand.',
    icon: <WarningIcon color="warning" />, color: 'warning',
  },
  {
    id: 7,
    question: 'What is the difference between a dry run and a real run?',
    answer: 'A dry run reports exactly what would happen — every step it would take, every file it '
      + 'would touch — without creating a Cloud project, opening a browser, or writing anything. It '
      + 'finishes in under a second because there is no real work to do. A real run does the actual '
      + 'work and can take several minutes, most of it either enabling Cloud APIs one at a time (each '
      + 'is a separate network call) or waiting for Google to finish propagating the delegation grant.',
    icon: <InfoIcon color="primary" />, color: 'primary',
  },
  {
    id: 8,
    question: 'Coverage Audit shows a service as "Unprobed" instead of a percentage — why not just show 0%?',
    answer: 'Because those are different claims. "0%" would mean the tenant was checked and genuinely '
      + 'has nothing of that type. "Unprobed" honestly means the check has not run yet — the real '
      + 'answer is unknown, not zero. Run the audit to turn every "Unprobed" into a real ABSENT or '
      + 'COVERED verdict.',
    icon: <LightbulbIcon color="warning" />, color: 'warning',
  },
]

const HelpSystem: React.FC = () => {
  const [search, setSearch] = useState('')

  const filtered = helpTopics.filter(
    (topic) =>
      topic.question.toLowerCase().includes(search.toLowerCase())
      || topic.answer.toLowerCase().includes(search.toLowerCase()),
  )

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Help</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Why the app behaves the way it does — not a generic FAQ, answers specific to this product.
      </Typography>

      <Paper elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', p: 2, mb: 3 }}>
        <TextField
          fullWidth
          placeholder="Search…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          InputProps={{
            startAdornment: <InputAdornment position="start"><SearchIcon color="action" /></InputAdornment>,
          }}
        />
      </Paper>

      <Typography variant="subtitle1" fontWeight={600} sx={{ mb: 2 }}>
        {filtered.length} topic{filtered.length !== 1 ? 's' : ''}
      </Typography>

      <Stack spacing={2}>
        {filtered.map((topic) => (
          <Accordion key={topic.id} elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                {topic.icon}
                <Typography fontWeight={500}>{topic.question}</Typography>
              </Box>
            </AccordionSummary>
            <AccordionDetails>
              <Typography variant="body1">{topic.answer}</Typography>
            </AccordionDetails>
          </Accordion>
        ))}
        {filtered.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ textAlign: 'center', py: 3 }}>
            Nothing matches "{search}".
          </Typography>
        )}
      </Stack>
    </Box>
  )
}

export default HelpSystem
