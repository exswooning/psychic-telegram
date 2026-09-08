# Running Bitport on more than one machine

The coordination is already built and careful: `user_claims.py` hands each
user to exactly one node under a renewable lease, and a node that cannot
reach the coordinator **stops** rather than quietly deciding a user is
unavailable and moving on. What follows is what that does and does not
protect you from.

## Connecting a machine

On the machine that is joining:

```
./install_node.sh --coordinator http://100.x.y.z:8090 --token "$TOKEN"   # macOS, Linux
.\install_node.ps1 -Coordinator http://100.x.y.z:8090 -Token $env:TOKEN  # Windows
```

The coordinator URL is whatever **that** machine can reach. On a tailnet
that is the coordinator's Tailscale address: no port forwarding, nothing
public, and `BITPORT_NODE_TOKEN` still required on every call. Set the same
token on the coordinator, or `node_auth` refuses every request:

```
BITPORT_NODE_TOKEN=<a long random string>
```

`node_setup.sh` remains the right tool for a fresh Ubuntu server you drive
over SSH from a third machine. These two are for the machine you are sitting
at.

## What this is good for

Memory is what bounds concurrency. Measured on the 3.8 GB VPS: 16 seed
workers, ~2.4 GB resident, ~22 Drive writes/sec across all of them. Drive's
per-**project** quota is around 120 writes/sec, so there is roughly 5x of
headroom — and a second machine with more RAM converts directly into more
concurrent users until that quota binds.

That quota is shared. Every node uses the same service account and the same
Cloud project, so the ceiling is ~120 writes/sec **in total**, not per node.
Past it you buy 429s, not throughput.

## What it does not protect you from

**Each node keeps its own ledger.** `node_setup.sh` copies `migration.db` to
the node; nothing merges them afterwards. That matters differently per
service:

| service | duplicate check | safe across nodes? |
|---|---|---|
| Gmail | asks the **target** for the Message-ID | **yes** |
| Drive | `id_mapping` in the **local** ledger | **no** |

So if a lease lapses mid-user — a laptop sleeping is the obvious way — and
another node picks that user up, the new node has no record of the Drive
files the first one already copied, and copies them again. Gmail in the same
situation deduplicates against the target and is fine.

`main.py`'s `_renew_until` deliberately does not interrupt work whose lease
has moved: killing a user mid-migration is worse than finishing them. That
is the right call for a server and the reason a sleeping laptop is a poor
node.

**Reporting fragments.** Verification, the final report and any delta pass
read the local ledger, so on the coordinator they see only the coordinator's
share of the work.

## Recommendation

- **Gmail across several machines: fine.** Target-side deduplication makes
  handover safe.
- **Drive across several machines: only on machines that stay awake.** A
  server, not a laptop that closes.
- **A laptop node: disable sleep first**, or keep it to bounded runs you
  watch. `caffeinate -dimsu` on macOS, `powercfg /change standby-timeout-ac 0`
  on Windows.
- **Before adding machines, consider more RAM on the one you have.** The
  binding constraint measured here was memory, and a bigger VPS costs less
  than the split-ledger problem above.
