"""File downloads with resume. The Wayback Machine cuts large files at 1 MiB steps, so a download
is only complete once a ranged request returns nothing more."""

import time
from pathlib import Path

import httpx

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ganga-dss/0.1"}


def wayback(original: str, timestamp: str = "2026") -> str:
    """Raw (unrewritten) archived copy of a URL."""
    return f"http://web.archive.org/web/{timestamp}id_/{original}"


def download(url: str, dest: Path, tries: int = 8, timeout: float = 120.0) -> bool:
    """Fetch url into dest, resuming partial files. Returns True when the file is complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = dest.with_suffix(dest.suffix + ".ok")
    if done.exists() and dest.exists():
        return True
    with httpx.Client(timeout=timeout, headers=UA, follow_redirects=True) as client:
        for attempt in range(tries):
            have = dest.stat().st_size if dest.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with client.stream("GET", url, headers=headers) as r:
                    if r.status_code == 416:  # nothing beyond what we have
                        done.touch()
                        return True
                    if r.status_code == 404 and have and have % (1 << 20) == 0:
                        # The archive only ever stored a cut-off copy. Keep what exists and
                        # flag it; the parser salvages whatever pages are intact.
                        dest.with_suffix(dest.suffix + ".truncated").touch()
                        done.touch()
                        return True
                    if r.status_code == 200 and have:
                        have = 0  # server ignored the range; start over
                        dest.unlink()
                    elif r.status_code not in (200, 206):
                        if r.status_code in (429, 500, 502, 503, 504):
                            time.sleep(5 * (attempt + 1))
                            continue
                        return False
                    got = 0
                    with dest.open("ab" if have else "wb") as fh:
                        for chunk in r.iter_bytes():
                            fh.write(chunk)
                            got += len(chunk)
                    total = r.headers.get("content-range", "").rpartition("/")[2]
                    size = dest.stat().st_size
                    if total.isdigit() and size >= int(total):
                        done.touch()
                        return True
                    if got == 0:
                        done.touch()
                        return True
                    # A full 200 reply whose length is not a whole MiB is complete.
                    if r.status_code == 200 and size % (1 << 20) != 0:
                        done.touch()
                        return True
            except (httpx.TransportError, httpx.TimeoutException):
                time.sleep(3 * (attempt + 1))
    return False
