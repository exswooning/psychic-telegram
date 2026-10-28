# Running Bitport without a rented VPS

Reviewed 28 October 2026. This is a hosting plan, not an automatic cloud provisioning action.

## Recommended first deployment

Run the existing Bitport API, UI, SQLite ledgers and migration workers on the customer-owned Linux computer. Keep credentials and data on that computer. A terminal can launch Bitport, but the computer executing that terminal supplies the CPU, memory, disk and network; the terminal is not a hosting service.

Use the existing systemd services for restart after reboot, keep the machine awake and connected, and back up its ledgers, configuration and credentials to customer-controlled encrypted storage. Build updates elsewhere and install a versioned release only while migration/verification/repair jobs are idle. Keep the previous release for rollback. A backup must not leak into the public source repository.

## Cloudflare Tunnel

Cloudflare Tunnel supplies HTTPS access to the local panel through an outbound connection. It does not host the workers or transfer Google data for them. Migrations continue to call Google directly from the customer computer.

Requirements:

1. A Cloudflare account and a domain/zone that the customer controls; choose an unused hostname such as `bitport.example.com` after confirming ownership.
2. An authorized Cloudflare dashboard session to create a named tunnel and DNS route.
3. A local connector (`cloudflared`) running as a system service on the Bitport computer. Its credentials stay on that computer with restricted permissions.
4. Route only the selected hostname to the local web entry point, normally `http://127.0.0.1:81`. Use the existing Bitport login, and put the administrative panel behind an Access policy for the intended operators. Keep session cookies, redirects and upload/WebSocket paths working through the public hostname.
5. Check HTTPS, login, authorized access, reconnection and reboot recovery before sharing the URL. No router port-forward or public IP is needed.

Do not guess the customer's domain, replace an existing DNS record, expose the bare API, or publish a temporary anonymous tunnel as the permanent administrative endpoint. Cloudflare DNS authorization is still required even when the connector is installed.

Official documentation: [Tunnel overview](https://developers.cloudflare.com/tunnel/), [setup](https://developers.cloudflare.com/tunnel/get-started/), [signed Linux packages](https://pkg.cloudflare.com/index.html).

## Google Cloud alternatives

The current free trial provides $300 for 90 days to an eligible new Google Cloud customer. It is attached to the trial billing account, not automatically awarded to every Workspace mailbox, domain or project. Do not design the product around repeated signups. Each genuinely eligible customer can evaluate its own offer, billing ownership and remaining credit.

A temporary Compute Engine VM is a viable alternative for a defined migration window. Estimate VM hours, disks, outbound network, backups and API-related services before creating it. For orientation, the listed US E2 standard-4 compute price is about $0.134/hour, or $96.50 for 30 continuous days, before disk/network/tax; region and pricing changes matter. This is an illustration, not a quote for a specific migration or a guarantee that $300 covers it. Stop/delete the VM and unused disks when the approved job is complete, after exporting the required ledgers and reports.

The ongoing Compute Engine free tier offers a much smaller E2 micro with limited disk and outbound transfer. Its roughly 1 GiB memory and shared CPU are unsuitable for the existing large, concurrent Bitport workload without major limits. A free-tier allowance is not free unlimited migrations.

Cloud Shell is an interactive development/administration environment. It has a default 50-hour weekly quota, 5 GB persistent home storage, automatic non-interactive termination and a maximum session length. It is useful for installing tools, building a release or provisioning a worker, but is unsuitable for reliable unattended migrations, repair schedules or hosting a permanent panel. Selecting a project in its prompt does not grant IAM access to that project. The screenshot's additional-access error must be resolved by the actual project owner or by selecting a project the customer is authorized to use; activating billing does not fix IAM.

Sources: [trial eligibility](https://cloud.google.com/signup-faqs), [free program](https://docs.cloud.google.com/free/docs/free-cloud-features), [VM prices](https://cloud.google.com/products/compute/pricing/general-purpose), [Cloud Shell limits](https://docs.cloud.google.com/shell/docs/quotas-limits).

## Customer panel or hosting account

An ordinary shared-hosting panel usually does not provide enough persistent CPU/RAM, long-running Python processes, service management or durable local SQLite access for this application. It can work only if the underlying account explicitly permits these resources and background jobs. A browser-only frontend cannot replace the worker.

The practical next product step is a customer-hosted installer/agent with versioned updates and health/status reporting. The existing node/worker functionality should be audited for durable queue ownership, credential isolation and reconnect/resume behavior before it is offered as a fleet product. Avoid running the same ledger from multiple computers. For a later cloud control plane, separate the small UI/status service from customer-owned workers; do not put the current SQLite ledger on an ephemeral multi-instance web host.
