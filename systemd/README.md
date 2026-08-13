# systemd units

Four services, meant to run together on the VPS. `xvfb.service`/
`x11vnc.service` back `dwd_helper.py`'s real-browser automation (a virtual
display, and a way to watch/finish 2FA over VNC through the tunnel).
`bitport-webui.service`/`bitport-api.service` run the two servers that used
to be started by hand with `nohup` + a `.pid` file.

## Install (one-time, on the VPS)

```bash
cp systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xvfb x11vnc bitport-webui bitport-api
```

## Redeploy

`sync_vps.sh` runs `systemctl restart bitport-webui bitport-api` after
syncing files -- no manual `nohup`/kill-by-port dance anymore. `xvfb`/
`x11vnc` don't need restarting on a code deploy; they're independent of the
Python processes.

## Why these four, together

`bitport-api.service` sets `DISPLAY=:99` because `full_setup_start`
(`api_server.py`) launches `full_setup.py`/`dwd_helper.py` as a subprocess
that needs a real X display to put Chrome on. `bitport-webui.service` sets
the same for its own `/api/dwd/automate` path, which calls into
`dwd_helper.py` in-process rather than via subprocess.

`bitport-api.service` also sets `CP_OPERATORS=aryan:admin` and
`BITPORT_COOKIE_SECURE=1` -- the second only makes sense once this is
actually reached over real HTTPS (see the Caddyfile at the repo root),
which is what these units are for.
