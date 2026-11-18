# /// script
# requires-python = ">=3.11"
# dependencies = ["telethon", "qrcode[pil]"]
# ///
"""One-time Telegram login: writes ~/.job_search/telegram.env (mode 600).

Takes the API id and hash from my.telegram.org (from telegram.env if already there, else
asks), then logs in. The session string is written straight to the file and never printed:
it is equivalent to full access to the account.

    uv run scripts/tg_login.py          # phone number + code (+ 2FA password), interactive
    uv run scripts/tg_login.py --qr     # QR code: needs no terminal input, can run unattended

--qr opens each QR as a picture on screen (and prints it as text); scan it in the Telegram app:
Settings -> Devices -> Link Desktop Device. A QR expires in about 30 seconds and is replaced
until --timeout runs out. If the account has a 2FA password, the script waits for it in
~/.job_search/.tg_2fa (one line, deleted as soon as it is read), written from a terminal with:
    read -rs p && printf %s "$p" > ~/.job_search/.tg_2fa && chmod 600 ~/.job_search/.tg_2fa
"""
import argparse
import asyncio
import getpass
import os
import subprocess
import tempfile
import time
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

SECRETS = Path.home() / ".job_search"
ENV_FILE = SECRETS / "telegram.env"
PASSWORD_FILE = SECRETS / ".tg_2fa"


def show_qr(url: str, n: int) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)
    png = Path(tempfile.gettempdir()) / f"telegram_login_qr_{n}.png"
    qr.make_image().save(png)
    subprocess.run(["open", str(png)], check=False)
    print(f"QR {n} opened on screen ({png}); scan it: Settings -> Devices -> Link Desktop Device",
          flush=True)


async def wait_password(timeout: int) -> str:
    print(f"2FA password needed: waiting up to {timeout}s for {PASSWORD_FILE}", flush=True)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if PASSWORD_FILE.exists():
            pw = PASSWORD_FILE.read_text().strip()
            PASSWORD_FILE.unlink()
            return pw
        await asyncio.sleep(2)
    raise SystemExit("no 2FA password arrived; nothing written")


async def login_qr(client: TelegramClient, timeout: int) -> None:
    end = time.monotonic() + timeout
    qr, n = await client.qr_login(), 1
    while True:
        show_qr(qr.url, n)
        try:
            await qr.wait(timeout=max(5, min(25, end - time.monotonic())))
            return
        except SessionPasswordNeededError:
            await client.sign_in(password=await wait_password(timeout))
            return
        except asyncio.TimeoutError:
            if time.monotonic() >= end:
                raise SystemExit("QR was not scanned in time; nothing written")
            await qr.recreate()
            n += 1


async def run(args) -> None:
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    os.chmod(SECRETS, 0o700)
    # An env file with only the id and hash (no session yet) is reused, not asked again.
    known = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            key, _, value = line.partition("=")
            known[key.strip()] = value.strip()
    api_id = int(known.get("TELEGRAM_API_ID") or input("API id: ").strip())
    api_hash = known.get("TELEGRAM_API_HASH") or getpass.getpass("API hash (hidden): ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        if args.qr:
            await login_qr(client, args.timeout)
        else:
            await client.start()
        me = await client.get_me()
        session = client.session.save()
    finally:
        await client.disconnect()
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"TELEGRAM_API_ID={api_id}\n")
        f.write(f"TELEGRAM_API_HASH={api_hash}\n")
        f.write(f"TELEGRAM_SESSION_STRING={session}\n")
    print(f"Logged in as {me.first_name}; wrote {ENV_FILE}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qr", action="store_true", help="log in by scanning a QR code")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for the scan (--qr)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
