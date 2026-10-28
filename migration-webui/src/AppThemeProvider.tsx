import React from 'react'
import { ThemeProvider, CssBaseline } from '@mui/material'
import { theme, darkTheme } from '@/theme'
import { workspaceTheme } from '@/theme/workspace'
import { workspaceUI } from '@/workspace'
const nextLight = workspaceTheme(false)
const nextDark = workspaceTheme(true)
import { useMigrationStore } from '@/store'

export const AppThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const darkMode = useMigrationStore((s) => s.darkMode)
  return (
    <ThemeProvider theme={workspaceUI ? (darkMode ? nextDark : nextLight) : (darkMode ? darkTheme : theme)}>
      <CssBaseline />
      {children}
    </ThemeProvider>
  )
}
