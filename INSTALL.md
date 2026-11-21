# Install Bitport

Bitport moves a company's Google Workspace (Drive files, Gmail, Calendar, Contacts and Tasks) from one
Google account to another. This page gets Bitport running on a server. It takes about 30 minutes, and
longer if the server's internet is slow. You do not need to know how to program.

## What you need

- **A server that stays switched on.** Ask your hosting company for "an Ubuntu server (version 22.04 or
  24.04) with 4 GB of memory and 50 GB of disk". This is sometimes called a VPS.
  - Ordinary website hosting (cPanel / shared hosting) will **not** work. Bitport has to run for many
    hours and needs full control of its server.
- **The Bitport folder on that server.** If you have it on your own computer, copy it up with:
  `scp -r bitport root@YOUR-SERVER-ADDRESS:/root/`
- **Later, the Google admin logins** for the account you are moving from and the one you are moving to.
  You do not need them yet.

## Step 1: log in to the server

Open a terminal (on a Mac: the app called Terminal) and type:

    ssh root@YOUR-SERVER-ADDRESS

## Step 2: run the installer

    cd bitport
    sudo bash install.sh

It asks a few questions. If you are not sure, press **Enter** to take the answer in brackets.

| Question | What to do |
|---|---|
| Install directory | Press Enter. |
| Public domain for HTTPS | Press Enter if you do not have a web address for the server. |
| First superadmin email | Type your email address. This is your login. |
| Superadmin password | Press Enter to have one made for you. It is shown at the end. |
| Enable browser automation | Press Enter (yes). |
| Make SSH key-only | Press Enter (yes). The installer tells you at the end if it left passwords on to keep you from being locked out. |

Then wait. The screen shows each step. A line like `... installing Python packages: still working,
12 min so far` means everything is fine; it is only telling you it has not stopped. You can close the
window and the install carries on by itself.

## Step 3: when it says "Bitport is installed"

The last lines tell you three things: the address to open, your login email, and your password. Write
the password down. It is also saved in `/root/bitport-admin-password.txt`.

1. Open the address in your web browser.
2. Sign in.
3. Click **Setup Wizard** in the menu on the left and follow it. It connects your two Google accounts.

## If something goes wrong

| What you see | What it means and what to do |
|---|---|
| `The install stopped before it finished` | Read the last lines above it, or run `tail -50 /var/log/bitport-install.log`. Run `sudo bash install.sh` again: it is safe to repeat and continues where it stopped. |
| The installer seems frozen | Look for the `still working` lines. If one appears every 30 seconds, it is working. |
| The address does not open | Run `bitport status`. If something is not `active`, run `bitport logs` and send the last lines to whoever supports you. Check that your server's firewall allows the port in the address. |
| You forgot the password | It is in `/root/bitport-admin-password.txt` if the installer made it for you. |

## Handy commands

Type these on the server (you do not need to be root, and you do not need to remember any folders):

- `bitport ui` a live dashboard of your migration. Press `q` to leave it.
- `bitport status` is Bitport running?
- `bitport url` the web address again.
- `bitport logs` watch what Bitport is doing. Press Ctrl+C to stop watching.
