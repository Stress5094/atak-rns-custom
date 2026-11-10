# rtak settings server

Pushes radio settings (LoRa preset, frequency and power; the Coverage model;
optionally the network passphrase) to the team's phones, the same way a host
phone does on WiFi, from a Linux machine. Each phone still accepts a change
with its own PIN, unless it chose to trust this server. How it works is in
`docs/USER-GUIDE.md`, section 11.

It is the same code the app runs (`settings_server.py`, `settings_net.py`,
`settings_share.py`, plus `radio.py`, `coverage.py` and `overture_tiles.py` to
check a push before it goes out). Needs Python 3.9+ and `cryptography`:

    sudo apt install python3-cryptography      # or: pip install cryptography

## Run it

    python3 settings_server.py serve --name "Team server"

State goes to `~/.rtak-settings-server` (`--state DIR` to move it). Phones
connect to TCP port **47812**: open it in the firewall (`sudo ufw allow
47812/tcp`). Port-forward it on the router if phones should reach it from the
internet.

- `--lan`: also send the WiFi beacon (UDP 47811), so phones on the same
  network find the server with nothing typed, as they find a host phone.
- `--open`: push to every phone that connects, with no allow list. Only for a
  server nobody outside the team can reach.

## The admin page

    python3 settings_server.py serve --web-port 8080
    python3 settings_server.py web-url

`web-url` prints the page's address with its key
(`http://127.0.0.1:8080/#token=...`). The page shows the phones live, with
Allow and Revoke, a push form, and the server's code as a QR code (with
`qrencode` installed). It does what the commands below do, through the same
admin socket.

It answers on this machine only. From another computer, tunnel to it:

    ssh -L 8080:localhost:8080 you@server

and open the same address there. Without the key, the page loads but shows
nothing; the key is in `web_token` in the state directory, readable by the
server's user only. Other websites open in the same browser cannot use it.

## Add it on a phone

    python3 settings_server.py card --addr settings.example.org

prints the server's code (and shows it as a QR code if `qrencode` is
installed). On the phone: Share settings → Servers → *Scan a server code*.
Typing the address also works, but the code is better: it also tells the
phone which server must answer there, so a different machine at that address
is refused.

## Allow phones

A phone that connects shows up as *waiting for the admin*:

    python3 settings_server.py status
    python3 settings_server.py allow 3f9a          # enough of the id to be unique

Check the id against the phone's own screen (Share settings → Servers, "This
phone's id"). `revoke ID` takes a phone off the list and disconnects it; if
it connects again it is back to *waiting for the admin*, and is sent nothing.

## Push

    python3 settings_server.py push --lora-preset "Medium Slow" --lora-freq-mhz 906.875
    python3 settings_server.py push --lora-tx-dbm 20 --now
    python3 settings_server.py push --fm-mhz 146.52 --fm-w 5 --tx-height-m 2
    python3 settings_server.py push --passphrase       # asks for it, twice

Every phone that accepted switches together, 2 minutes after the push by
default (`--switch-in-min M`; `--now` = each phone when it accepts). A phone
that connects later still gets the push, with the original switch time. A
mistake (an unknown preset, a frequency outside the band) is refused before
any phone sees it.

`status` shows each phone: waiting for its PIN, accepted, switched, rejected,
undone. It is a report from the phones. A phone keeps a change until someone
presses Undo on it; nothing switches back by itself.

## What it keeps, and what to back up

- `host_key.json` **is the server**. Lose it and every phone has to add it (and
  trust it) again. Back it up; keep it private.
- `host_rev.json`, `server.json`: revision numbers, the allow list, the roster,
  the last push.
- **The passphrase is never written to disk.** It is kept in memory for phones
  that connect after a push. After a restart, a phone that still lacks a push
  that carried it shows *push again* on `status`, and is sent nothing until
  you push again.

## Security, briefly

Everything after the first two messages of a connection is encrypted, with
keys made fresh for that connection and thrown away after it: a key stolen
later does not unlock a recorded session. Each side proves who it is: the
server signs with its key, and the phone proves it holds the key its id comes
from. A phone that added this server from its code insists on this server's
key; one that typed the address keeps the key from the first answer. Back up
`host_key.json`: a server with a new key has to be added again on every
phone. Settings are signed, revisions only go up,
and the passphrase is also sealed to each phone separately. The admin
commands use a socket in the state directory that only the server's user can
open. There is no admin port on the network.

Not hardened against a flood of connections: 64 phones and 16 waiting at
once, and a connection that does not identify itself within 10 s is dropped.
