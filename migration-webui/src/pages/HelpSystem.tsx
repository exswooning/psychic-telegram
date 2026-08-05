import React, { useState } from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  CardHeader,
  TextField,
  InputAdornment,
  List,
  ListItem,
  ListItemIcon,
  ListItemText,
  ListItemSecondaryAction,
  IconButton,
  Chip,
  Paper,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Stack,
} from '@mui/material'
import {
  Help as HelpIcon,
  Search as SearchIcon,
  ExpandMore as ExpandMoreIcon,
  Lightbulb as LightbulbIcon,
  Warning as WarningIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  Info as InfoIcon,
} from '@mui/icons-material'

const HelpSystem: React.FC = () => {
  const [search, setSearch] = useState('')

  const helpTopics = [
    {
      id: 1,
      question: 'What is happening right now?',
      answer: 'The migration is currently copying Gmail messages from the source tenant to the target tenant. This is the largest data type and takes the most time.',
      icon: <InfoIcon color="primary" />,
      color: 'primary',
    },
    {
      id: 2,
      question: 'Do I need to do anything?',
      answer: "No action is required. You can safely leave this running. The migration will automatically retry if Google rate-limits any API calls.",
      icon: <CheckCircleIcon color="success" />,
      color: 'success',
    },
    {
      id: 3,
      question: 'Why is the migration paused?',
      answer: 'The migration was paused because memory usage exceeded the safe threshold (85% of available RAM). This is a protective measure to prevent the system from running out of memory. The migration will automatically resume when resources recover.',
      icon: <WarningIcon color="warning" />,
      color: 'warning',
    },
    {
      id: 4,
      question: 'How do I fix a rate limit error?',
      answer: 'Rate limit errors are handled automatically. The migration will pause, wait for the Google quota to reset, and then resume. You do not need to take any action. If the error persists for more than 5 minutes, check the Google Cloud Console for quota limits.',
      icon: <LightbulbIcon color="warning" />,
      color: 'warning',
    },
    {
      id: 5,
      question: 'What does "Needs Attention" mean?',
      answer: 'This status means the migration encountered an error that requires human intervention. Check the Error Handling page for details on what went wrong and how to fix it. Common causes include permission issues, quota limits, or network problems.',
      icon: <ErrorIcon color="error" />,
      color: 'error',
    },
    {
      id: 6,
      question: 'How long will the migration take?',
      answer: 'Migration time depends on the amount of data and network speed. As a rough guide: 1 GB of data takes approximately 10-15 minutes. The dashboard shows an estimated completion time based on current throughput.',
      icon: <InfoIcon color="primary" />,
      color: 'primary',
    },
  ]

  const filtered = helpTopics.filter(
    (topic) =>
      topic.question.toLowerCase().includes(search.toLowerCase()) ||
      topic.answer.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Help</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Contextual help for every step of the migration process</Typography>

      <Paper elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', p: 2, mb: 3 }}>
        <TextField
          fullWidth
          placeholder="Search for help topics..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          InputProps={{
            startAdornment: <InputAdornment position="start"><SearchIcon color="action" /></InputAdornment>,
          }}
        />
      </Paper>

      <Typography variant="subtitle1" fontWeight={600} sx={{ mb: 2 }}>
        {filtered.length} topic{filtered.length !== 1 ? 's' : ''} found
      </Typography>

      <Stack spacing={2}>
        {filtered.map((topic) => (
          <Accordion key={topic.id} elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                {topic.icon}
                <Typography fontWeight={500}>{topic.question}</Typography>
                <Chip label={topic.color} size="small" color={topic.color as any} variant="outlined" />
              </Box>
            </AccordionSummary>
            <AccordionDetails>
              <Typography variant="body1">{topic.answer}</Typography>
            </AccordionDetails>
          </Accordion>
        ))}
      </Stack>
    </Box>
  )
}

export default HelpSystem