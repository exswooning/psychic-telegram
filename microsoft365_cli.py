"""Microsoft worker and host-local certificate installation. Never accepts passwords."""
from __future__ import annotations

import argparse
import os
import signal
import sys

from microsoft365.discovery import Discovery
from microsoft365.graph import GraphError, Stopped, install_certificate
from microsoft365.operations import SandboxOperations
from microsoft365.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cert = sub.add_parser("install-certificate", help="Import a certificate locally; private key never enters a web request")
    cert.add_argument("--account-id", type=int, required=True)
    cert.add_argument("--side", choices=("source", "target"), required=True)
    cert.add_argument("--tenant-id", required=True)
    cert.add_argument("--client-id", required=True)
    cert.add_argument("--private-key", required=True)
    cert.add_argument("--certificate", required=True)
    run = sub.add_parser("run", help="Execute the specific persisted UI job")
    run.add_argument("--account-id", type=int, required=True)
    run.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command == "install-certificate":
        install_certificate(args.account_id, args.side, args.tenant_id, args.client_id,
                            args.private_key, args.certificate)
        print(f"{args.side} certificate installed. Grant application permissions in that tenant and run discovery in Bitport.")
        return 0
    import job_admission
    store = Store(args.account_id)
    job = store.run(args.run_id)
    if job["state"] not in ("QUEUED", "RUNNING", "STOPPING"):
        raise ValueError("that run is terminal; start a new reviewed run from the UI")
    admitted, reason = job_admission.adopt_or_admit(args.account_id, "microsoft365", os.getpid())
    if not admitted:
        store.update_run(args.run_id, "BLOCKED", {"error": reason})
        print(reason)
        return 2

    def stop(*_):
        store.stop(args.run_id)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if store.run(args.run_id)["stop_requested"]:
            raise Stopped("Run stopped before starting")
        if job["kind"] == "discover":
            Discovery(store, args.run_id).run()
        else:
            SandboxOperations(store, args.run_id).run()
        print("Microsoft job finished; inspect its coverage and verification findings.")
        return 0
    except Stopped as exc:
        store.update_run(args.run_id, "STOPPED", {"error": str(exc)})
        print(str(exc))
        return 130
    except (GraphError, ValueError) as exc:
        store.update_run(args.run_id, "ERROR", {"error": str(exc)})
        print(str(exc))
        return 1
    except Exception as exc:
        # Exceptions from HTTP libraries may embed authenticated URLs. Never log their text.
        detail = "Microsoft worker failed: " + type(exc).__name__ + "; inspect credentials and retry from the UI"
        store.update_run(args.run_id, "ERROR", {"error": detail})
        print(detail)
        return 1
    # The API's subprocess waiter releases its reservation after this process
    # actually exits. Releasing here would let a second job start before that
    # waiter, whose release-by-name could then erase the second reservation.
    # A host-invoked worker is reclaimed by admission's dead-PID reaper.


if __name__ == "__main__":
    sys.exit(main())
