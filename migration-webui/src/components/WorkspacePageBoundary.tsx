import { Component, ReactNode } from 'react'
import { Alert, Box, Button, Stack, Typography } from '@mui/material'
/** Leave navigation available if a page fails to render. */
export default class WorkspacePageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    if (!this.state.failed) return this.props.children
    return <Box sx={{ p: 3 }}><Alert severity="error"><Typography variant="h6">Couldn’t open this page</Typography><Typography variant="body2">Try loading it again, or choose another tool from the navigation.</Typography></Alert><Stack direction="row" gap={1} sx={{ mt: 2 }}><Button variant="outlined" onClick={() => this.setState({ failed: false })}>Try again</Button><Button onClick={() => window.location.reload()}>Reload workspace</Button></Stack></Box>
  }
}
