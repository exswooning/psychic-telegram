-- 008_node_directives.sql
--
-- Starting a node's work meant SSH-ing into it and running main.py. The
-- Nodes page could not offer a button for it, because the whole design
-- points the other way: nodes connect OUTWARD to the coordinator and
-- nothing reaches into a node. fleet_agent.py's docstring gives the reason
-- -- a control plane that could reach into its nodes would need SSH
-- credentials for every machine holding service-account keys for both
-- tenants, which turns a dashboard into a lateral-movement path across the
-- whole migration.
--
-- So the button does not reach in. It writes down what the operator WANTS,
-- here, and each node asks. Desired state pulled by the node, never pushed
-- to it -- the same shape as a Kubernetes node reading its spec.
--
-- One row per account, not per node: "migrate this tenant" is a statement
-- about the work, and every node on that tenant should act on it. Which
-- node picks up which user is already decided by user_claims.
CREATE TABLE IF NOT EXISTS node_directives (
    account_id  INTEGER PRIMARY KEY REFERENCES accounts(id),
    -- 0 or 1. A node that finds 0 stops the run it started; it does not
    -- kill it, because main.py finishes the user it is on rather than
    -- stranding a mailbox half-copied (see _renew_until).
    run         INTEGER NOT NULL DEFAULT 0,
    services    TEXT NOT NULL DEFAULT '',
    -- Who asked and when, because this starts writes against a live tenant
    -- and "who turned this on" is the first question after a surprise.
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_by  TEXT NOT NULL DEFAULT ''
);
