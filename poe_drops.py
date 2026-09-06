"""PoE forum -> Discord. Python 3.10+, no external dependencies."""
import argparse
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORUMS = {"PoE 1": "news", "PoE 2": "2211"}
BASE = "https://www.pathofexile.com"
MATCH = re.compile(r"\btwitch\s+drops?\b", re.I)
UA = "PoEDropsNotifier/1.0"


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(html):
    parser = Text()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


def parse_threads(html):
    found = {}
    for ident, title in re.findall(
        r'<div\s+class="title"\s*>\s*<a\s+href="/forum/view-thread/(\d+)"[^>]*>(.*?)</a>',
        html, re.S,
    ):
        found[ident] = plain(title)
    if not found:
        raise RuntimeError("Geen threads gevonden; mogelijk websiteblokkade of gewijzigde HTML.")
    return found


def first_post(html):
    match = re.search(r'<tr\s+class="newsPost"\s*>(.*?)</tr>', html, re.S)
    if not match:
        raise RuntimeError("Eerste aankondigingsbericht niet gevonden; thread wordt later opnieuw geprobeerd.")
    return plain(match.group(1))


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def validate_webhook(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "discord.com"
            or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+", parsed.path)
            or parsed.query or parsed.fragment or parsed.port is not None):
        raise ValueError("Vul een geldige Discord-webhook-URL in (https://discord.com/api/webhooks/...).")
    return url


def send(url, payload):
    data = json.dumps({**payload, "allowed_mentions": {"parse": []}}).encode("utf-8")
    for attempt in range(4):
        request = urllib.request.Request(url + "?wait=true", data=data, headers={
            "User-Agent": UA, "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            if not result.get("id"):
                raise RuntimeError("Discord heeft verzending niet bevestigd.")
            return
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 3:
                wait = float(json.loads(error.read()).get("retry_after", 5))
                time.sleep(min(max(wait, 1), 60))
                continue
            # Never expose the secret URL in logs.
            raise RuntimeError(f"Discord HTTP {error.code}; niet als verzonden opgeslagen.") from None
        except urllib.error.URLError:
            raise RuntimeError("Discord netwerkfout; niet als verzonden opgeslagen.") from None
    raise RuntimeError("Discord blijft bezet; probeer later opnieuw.")


def payload(game, ident, title, body):
    snippet = body[:900] + ("..." if len(body) > 900 else "")
    return {"username": "PoE Twitch Drops", "embeds": [{
        "title": f"{game} | {title}"[:256],
        "url": f"{BASE}/forum/view-thread/{ident}",
        "description": snippet,
        "color": 9520895,
        "footer": {"text": "Twitch Drops mentioned — check the announcement for requirements and start/end times."},
    }]}


def scan(state, webhook=None, preview=False):
    errors = 0
    for game, forum in FORUMS.items():
        try:
            threads = parse_threads(fetch(f"{BASE}/forum/view-forum/{forum}"))
        except Exception as error:
            print(f"{game}: ophalen mislukt ({type(error).__name__}).", flush=True)
            errors += 1
            continue
        if not preview and forum not in state["forums"]:
            state["forums"][forum] = {ident: "baseline" for ident in threads}
            save_json(ROOT / "state.json", state)
            print(f"{game}: {len(threads)} bestaande threads geregistreerd; geen oude meldingen verstuurd.", flush=True)
            continue
        seen = state["forums"].get(forum, {})
        candidates = list(reversed(list(threads.items())))
        for ident, title in candidates:
            if not preview and ident in seen:
                continue
            try:
                body = first_post(fetch(f"{BASE}/forum/view-thread/{ident}"))
                matches = bool(MATCH.search(title + " " + body))
                if preview:
                    if matches:
                        print(json.dumps(payload(game, ident, title, body), ensure_ascii=False), flush=True)
                else:
                    if matches:
                        send(webhook, payload(game, ident, title, body))
                        print(f"Verstuurd: {game} — {title}", flush=True)
                    seen[ident] = "sent" if matches else "checked"
                    save_json(ROOT / "state.json", state)
            except Exception as error:
                print(f"{game}, thread {ident}: mislukt ({type(error).__name__}); opnieuw bij volgende controle.", flush=True)
                errors += 1
            time.sleep(1)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="Webhook lokaal instellen")
    parser.add_argument("--test", action="store_true", help="Een testbericht naar Discord sturen")
    parser.add_argument("--once", action="store_true", help="Eenmalig controleren")
    parser.add_argument("--preview", action="store_true", help="Huidige matches tonen zonder verzenden of opslaan")
    args = parser.parse_args()
    if args.setup:
        url = validate_webhook(getpass.getpass("Plak de Discord-webhook-URL (invoer blijft verborgen): ").strip())
        save_json(ROOT / "config.json", {"webhook_url": url, "interval_seconds": 900})
        print("Instellingen opgeslagen. Start START.bat om te controleren.")
        return 0
    if args.preview:
        return int(bool(scan({"forums": {}}, preview=True)))
    config = read_json(ROOT / "config.json", {})
    webhook = validate_webhook(os.environ.get("DISCORD_WEBHOOK_URL") or config.get("webhook_url", ""))
    interval = max(60, int(config.get("interval_seconds", 900)))
    if args.test:
        send(webhook, {"content": "Test successful! The PoE 1 + PoE 2 Twitch Drops webhook is working."})
        print("Testbericht verstuurd.")
        return 0
    # One process at a time, released automatically even after a crash.
    lock = open(ROOT / ".lock", "a+b")
    lock.seek(0)
    if os.name == "nt":
        import msvcrt
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = read_json(ROOT / "state.json", {"forums": {}})
    if not isinstance(state.get("forums"), dict):
        raise ValueError("state.json is ongeldig; herstel een reservekopie.")
    while True:
        print(time.strftime("%Y-%m-%d %H:%M:%S") + " — forums controleren...", flush=True)
        errors = scan(state, webhook)
        if args.once:
            return int(bool(errors))
        print(f"Controle klaar ({errors} fouten). Volgende controle over {interval // 60} minuten. Stoppen: Ctrl+C.", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Gestopt.")
    except Exception as error:
        # Avoid printing exception URLs, which may contain credentials.
        print(f"Starten mislukt ({type(error).__name__}). Controleer configuratie, state.json en of het script al draait.", file=sys.stderr)
        sys.exit(1)
