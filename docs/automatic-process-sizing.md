# Automatic migration process sizing

A migration pass estimates its process budget from current CPU/RAM and the other admitted jobs. Automatic allocation is now also capped at the number of eligible users for that specific pass, including its user filter and per-service completion state. One eligible user runs in the parent process. No eligible users use the ordinary empty-pass path. Explicit process overrides, dry runs and existing child shards retain their previous behavior.

The scheduler and dispatcher share the same eligibility function. This preserves service-specific resumption, delta and redo behavior rather than counting all directory users or trusting per-user DONE alone. The change does not alter transfer methods, source protections, admission or shutdown propagation.

Why: the authorized 29 October s. to t. one-user UI test automatically selected eight processes. Seven immediately reported no users; the remaining process still carried PROCESS_SHARE=8, which divides its resource/quota budget as though all eight had work. The new cap prevents this idle partitioning on subsequent passes/runs after deployment.

Observed before the fix: the Performance UI reported 11 workers, direct Drive copy calls, adaptive source/target rate increases from 5 to 7 calls/sec, and successful file/folder transfers. Those observations establish startup sizing and adaptive API pacing, not arbitrary continuous resizing of a running thread pool. Existing thread pools re-probe between passes; this patch does not claim live resizing within a pass.

Qualification: 148 focused process, resource, adaptive-worker, phase and scope tests passed. Changed production code and focused test lint passed; an existing unused `outer` variable in tests/test_scope_guard.py remains outside this patch. Do not deploy over an active migration. The current sandbox run stays on its original version until an idle deployment is possible.
