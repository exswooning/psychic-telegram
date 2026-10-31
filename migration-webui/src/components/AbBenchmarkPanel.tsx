import React from "react";
import {
  Alert,
  Box,
  Chip,
  LinearProgress,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { CompareArrows as AbIcon } from "@mui/icons-material";
import { fmtDuration, useAb } from "@/hooks/useAb";
import type { AbArm } from "@/api/controlPlane";

const armLabel = (c: { mode: string; mailMode: string | null }) =>
  c.mode + (c.mailMode ? ` + ${c.mailMode}` : "");

/** Services whose one-to-one verdict was not identical, each with what differed (counts). Shown
 *  in the row itself: a tooltip is invisible until someone hovers, and that hid every cause. */
const differing = (a: {
  verdicts: Record<string, string>;
  differences?: AbArm["differences"];
}) =>
  Object.entries(a.verdicts)
    .filter(([, v]) => v !== "IDENTICAL" && v !== "COMPLETE")
    .map(([k, v]) => {
      const counts = Object.entries(a.differences?.[k]?.counts ?? {}).filter(
        ([, n]) => n,
      );
      return { key: k, verdict: v, counts, kinds: a.differences?.[k]?.kinds ?? [] };
    });

/**
 * The controlled A/B, always on screen: which arm is running, how far along, how long is left,
 * and which arm is fastest among those that finished clean. The bar is a real fraction -- the arm
 * count is fixed before the run starts -- and the ETA only appears once one arm has finished.
 */
const AbBenchmarkPanel: React.FC = () => {
  const view = useAb();
  const p = view?.progress ?? null;
  const total = p?.total ?? 0,
    done = p?.done ?? 0;
  const best = view?.best ?? null;

  return (
    <Paper
      variant="outlined"
      sx={{ borderRadius: 2, p: 2 }}
      data-testid="ab-panel"
    >
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <AbIcon color="action" />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>
          Fastest way to migrate
        </Typography>
        {view?.running && <Chip size="small" color="primary" label="running" />}
      </Stack>
      <Typography variant="caption" color="text.secondary">
        The same user&#39;s data, once per transfer mode and mail mode, the
        target emptied between arms. Ranked by errors (failed items plus every
        difference the arm&#39;s own one-to-one check found), then by time. An arm
        whose check did not run is listed but never wins.
      </Typography>

      {!view && (
        <Typography variant="body2" sx={{ mt: 2 }}>
          Loading…
        </Typography>
      )}
      {view && !p && (
        <Alert severity="info" sx={{ mt: 2 }} data-testid="ab-none">
          No A/B has run on this box yet.
        </Alert>
      )}

      {p && (
        <Box sx={{ mt: 2 }} data-testid="ab-progress">
          <Stack
            direction="row"
            spacing={2}
            alignItems="baseline"
            sx={{ flexWrap: "wrap", gap: 1 }}
          >
            <Typography variant="body2" sx={{ fontWeight: 700 }}>
              {p.alive
                ? `Arm ${Math.min(done + 1, total)} of ${total}`
                : `${done} of ${total} arms done`}
            </Typography>
            {p.alive && p.arms && p.arms.length > 0 ? (
              <Typography variant="body2" data-testid="ab-arms">
                {p.phase ? `${p.phase}: ` : ""}
                {p.arms
                  .map((a) => `${a.arm}${a.user ? ` (${a.user})` : ""} · ${a.stage}`)
                  .join("; ")}
              </Typography>
            ) : (
              p.alive &&
              p.current && (
                <Typography variant="body2">
                  {armLabel(p.current)} · {p.current.stage}
                </Typography>
              )
            )}
            {p.alive && p.etaSeconds != null && (
              <Typography
                variant="body2"
                color="text.secondary"
                data-testid="ab-eta"
              >
                about {fmtDuration(p.etaSeconds)} left (from {p.armsFinished}{" "}
                finished,{" "}
                {p.meanArmSeconds != null ? fmtDuration(p.meanArmSeconds) : "?"}{" "}
                each)
              </Typography>
            )}
            {p.alive && p.etaSeconds == null && p.etaNote && (
              <Typography
                variant="body2"
                color="text.secondary"
                data-testid="ab-eta-note"
              >
                {p.etaNote}
              </Typography>
            )}
            {!p.alive && p.finishedAt && (
              <Typography variant="body2" color="text.secondary">
                finished {p.finishedAt}
              </Typography>
            )}
          </Stack>
          {total > 0 && (
            <LinearProgress
              variant="determinate"
              value={(done / total) * 100}
              sx={{ mt: 1, height: 6, borderRadius: 1 }}
            />
          )}
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{ display: "block", mt: 0.5 }}
          >
            {p.users?.join(", ")}
            {p.corpus
              ? ` · ${p.corpus.files.toLocaleString()} files, ${(p.corpus.bytes / 1e9).toFixed(2)} GB`
              : ""}
          </Typography>
        </Box>
      )}

      {p?.error && (
        <Alert severity="error" sx={{ mt: 2 }} data-testid="ab-error">
          The run stopped before it finished: {p.error}
        </Alert>
      )}

      {best && (
        <Alert severity="success" sx={{ mt: 2 }} data-testid="ab-best">
          Default for new runs: <strong>{best.arm}</strong>: fewest errors (
          {best.errorsPer1k != null
            ? `${best.errorsPer1k} per 1,000 items`
            : best.errors}
          ), then fastest
          {best.secondsPer100 != null
            ? ` (${best.secondsPer100}s per 100 items)`
            : best.seconds != null
              ? ` (${fmtDuration(best.seconds)})`
              : ""}
          . A run that names no transfer or mail mode uses it.
        </Alert>
      )}
      {!best && !view?.running && !!view?.ranking.length && (
        <Alert severity="info" sx={{ mt: 2 }} data-testid="ab-no-best">
          No arm has a completed one-to-one check yet, so new runs keep the
          configured defaults.
        </Alert>
      )}

      {!!p?.skipped?.length && (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ display: "block", mt: 1 }}
          data-testid="ab-skipped"
        >
          Skipped (slow): {p.skipped.join(", ")}. Run it with --include-slow.
        </Typography>
      )}

      {!!view?.ranking.length && (
        <TableContainer sx={{ mt: 2, overflowX: "auto" }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Arm</TableCell>
                <TableCell align="right">time</TableCell>
                <TableCell align="right">errors</TableCell>
                <TableCell align="right">failed</TableCell>
                <TableCell>Finished clean</TableCell>
                <TableCell>What differed</TableCell>
                <TableCell>Google mail import</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {view.ranking.map((a) => {
                const diffs = differing(a);
                return (
                  <TableRow key={a.arm}>
                    <TableCell>
                      {a.arm}
                      {a.corpus && (
                        <Typography
                          variant="caption"
                          color="text.secondary"
                          sx={{ display: "block" }}
                          data-testid="ab-corpus"
                        >
                          {a.corpus.user} · {a.corpus.items.toLocaleString()} items
                          ({a.corpus.files.toLocaleString()} files
                          {a.corpus.messages != null
                            ? `, ${a.corpus.messages.toLocaleString()} messages`
                            : ""}
                          )
                        </Typography>
                      )}
                      {best?.arm === a.arm && (
                        <Chip
                          size="small"
                          color="success"
                          label="default"
                          sx={{ ml: 1 }}
                        />
                      )}
                    </TableCell>
                    <TableCell
                      align="right"
                      sx={{ fontVariantNumeric: "tabular-nums" }}
                    >
                      {a.seconds != null ? fmtDuration(a.seconds) : "—"}
                      {a.timing && (
                        <Typography
                          variant="caption"
                          color="text.secondary"
                          sx={{ display: "block" }}
                        >
                          {a.timing === "alone"
                            ? "timed alone"
                            : "shared the box"}
                          {a.secondsPer100 != null
                            ? ` · ${a.secondsPer100}s per 100 items`
                            : ""}
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell
                      align="right"
                      data-testid="ab-errors"
                      sx={{ fontVariantNumeric: "tabular-nums" }}
                    >
                      {a.judged ? (
                        <>
                          {a.errors}
                          {a.errorsPer1k != null && (
                            <Typography
                              variant="caption"
                              color="text.secondary"
                              sx={{ display: "block" }}
                            >
                              {a.errorsPer1k} per 1,000 items
                            </Typography>
                          )}
                        </>
                      ) : (
                        <Typography variant="body2" color="text.secondary">
                          not ranked{a.why ? `: ${a.why}` : ""}
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell align="right">{a.failed ?? "—"}</TableCell>
                    <TableCell>
                      <Chip
                        size="small"
                        label={a.clean ? "clean" : "not clean"}
                        color={a.clean ? "success" : "warning"}
                        variant={a.clean ? "outlined" : "filled"}
                      />
                    </TableCell>
                    <TableCell data-testid="ab-differences">
                      {diffs.length === 0 ? (
                        <Typography variant="body2" color="text.secondary">
                          {Object.keys(a.verdicts).length
                            ? "nothing"
                            : "not checked"}
                        </Typography>
                      ) : (
                        diffs.map((d) => (
                          <Typography key={d.key} variant="body2">
                            <strong>{d.key}</strong> {d.verdict}
                            {d.counts.length
                              ? ` — ${d.counts.map(([n, x]) => `${x} ${n}`).join(", ")}`
                              : ""}
                            {d.kinds.map((x) => (
                              <Typography
                                key={x.kind}
                                variant="caption"
                                color="text.secondary"
                                sx={{ display: "block", pl: 1.5 }}
                                title={[x.cause, x.fix].filter(Boolean).join(" Fix: ")}
                                data-testid="ab-kind"
                              >
                                {x.count} × {x.title}
                                {x.auto ? " (fixed automatically)" : " (needs a person or cannot be fixed)"}
                              </Typography>
                            ))}
                          </Typography>
                        ))
                      )}
                      {a.failed ? (
                        <Typography variant="body2">
                          {a.failed} item(s) failed
                        </Typography>
                      ) : null}
                    </TableCell>
                    <TableCell data-testid="ab-dms">
                      {a.dms ? (
                        <Typography variant="body2">{a.dms}</Typography>
                      ) : (
                        <Typography variant="body2" color="text.secondary">
                          not run
                        </Typography>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  );
};

export default AbBenchmarkPanel;
