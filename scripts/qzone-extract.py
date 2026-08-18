#!/usr/bin/env python3
"""Extract text-only posts from saved QZone mood-list HTML into _posts/*.md.

Usage: python3 scripts/qzone-extract.py qzone/*.html
Each <li class="feed"> becomes _posts/YYYY-MM-DD-<moodid>.md with a `date:`
front matter field holding the post's exact publish time (CST, +08:00).
"""
import html
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "_posts"


def iter_posts(page: str):
    chunks = re.split(r'<li class="feed\s*"', page)[1:]
    for chunk in chunks:
        tid = re.search(r'data-tid="([^"]+)"', chunk)
        pre = re.search(r'<pre[^>]*class="content"[^>]*>(.*?)</pre>', chunk, re.S)
        time_link = re.search(
            r'class="c_tx c_tx3 goDetail"[^>]*title="([^"]+)"', chunk
        )
        if not tid or not pre or not time_link:
            continue
        text = html.unescape(re.sub(r'<br\s*/?>', '\n', pre.group(1))).strip()
        if not text:
            continue  # image-only posts have an empty <pre>
        yield tid.group(1), time_link.group(1), text


def parse_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y年%m月%d日 %H:%M")


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for path in argv:
        page = Path(path).read_text(encoding="utf-8", errors="replace")
        for tid, raw_time, text in iter_posts(page):
            dt = parse_time(raw_time)
            name = f"{dt:%Y-%m-%d}-{tid}.md"
            out = OUT_DIR / name
            frontmatter = (
                "---\n"
                f"date: {dt:%Y-%m-%d %H:%M:%S} +0800\n"
                "---\n\n"
            )
            # The source is a <pre>: preserve its line breaks in markdown by
            # using GFM hard breaks (two trailing spaces) on every line but
            # the last, so kramdown renders them as <br> instead of one line.
            lines = text.split("\n")
            body = "\n".join(
                line + "  " if i < len(lines) - 1 and line else line
                for i, line in enumerate(lines)
            )
            out.write_text(frontmatter + body + "\n", encoding="utf-8")
            written.append(name)
    for name in written:
        print(f"wrote {name}")
    print(f"{len(written)} posts written to {OUT_DIR}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))