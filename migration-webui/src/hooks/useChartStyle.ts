import { useTheme } from '@mui/material'

/** Theme-derived chart colours, so every chart reads in light and dark. */
export function useChartStyle() {
  const t = useTheme()
  return {
    grid: t.palette.divider,
    axis: t.palette.text.secondary,
    tooltip: {
      background: t.palette.background.paper,
      border: `1px solid ${t.palette.divider}`, fontSize: 12,
    },
    c: {
      primary: t.palette.primary.main, success: t.palette.success.main,
      warning: t.palette.warning.main, error: t.palette.error.main,
      info: t.palette.info.main, muted: t.palette.text.disabled,
    },
  }
}
