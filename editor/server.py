#!/usr/bin/env python3
"""Logli microblog editor: stdlib-only HTTP server + git client.

Runs inside Docker against a mounted checkout of the Jekyll repo at REPO_DIR.
Posts/edits/deletes commit+push immediately. Image uploads are staged as
untracked files in assets/images/ and are committed+pushed together with the
next post save. GitHub Actions then rebuilds the site.
"""
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None

HERE = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("REPO_DIR", "/repo"))
PORT = int(os.environ.get("PORT", "8000"))
GIT_NAME = os.environ.get("GIT_NAME", "logli-editor")
GIT_EMAIL = os.environ.get("GIT_EMAIL", "logli-editor@localhost")
EDITOR_UID = int(os.environ.get("EDITOR_UID", "501"))
EDITOR_GID = int(os.environ.get("EDITOR_GID", "20"))
TZ = "Europe/Zurich"
IMAGE_EXTS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
DISPLAY_MAX = 1600

LOCK = threading.Lock()


class GitError(Exception):
    pass


def run_git(repo, *args, timeout=120):
    r = subprocess.run(
        ["git", "-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}",
         "-c", "safe.directory=*", *args],
        cwd=repo, capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


def sync(repo):
    # Non-destructive: ff-only fast-forwards and never wipes local work
    # (uncommitted edits or staged-untracked images) the way reset --hard does.
    run_git(repo, "fetch", "origin", "main")
    run_git(repo, "merge", "--ff-only", "origin/main")


def git_flow(repo, mutate, msg, gc=False):
    # ponytail: global lock; the editor is the only writer, so serialize all ops.
    with LOCK:
        sync(repo)
        mutate()
        if gc:
            gc_orphan_images(repo)
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-m", msg)
        run_git(repo, "push", "origin", "HEAD")
        fix_owner(repo)


def fix_owner(repo):
    # Container runs as root (needed for ssh); hand the repo back to the host
    # user so posts/.git aren't root-owned on the host.
    if os.geteuid() != 0:
        return
    subprocess.run(["chown", "-R", f"{EDITOR_UID}:{EDITOR_GID}", repo],
                   capture_output=True, text=True)


def post_path(repo, name):
    name = Path(name).name
    if not re.fullmatch(r"[\w.-]+\.md", name):
        raise GitError("bad post name")
    return repo / "_posts" / name


def new_post_filename():
    now = datetime.now(ZoneInfo(TZ))
    return f"{now:%Y-%m-%d}-{int(time.time() * 1000)}-{secrets.token_hex(3)}.md"


def write_post(repo, name, content):
    p = post_path(repo, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n---\n\n" + content, encoding="utf-8")


def delete_post(repo, name):
    # git add -A in git_flow stages the deletion (tracked) or ignores it (untracked).
    post_path(repo, name).unlink(missing_ok=True)


def image_path(repo, name):
    name = Path(name).name
    if not re.fullmatch(r"[\w.-]+\.(?:png|jpe?g|gif|webp)", name):
        raise GitError("bad image name")
    return repo / "assets" / "images" / name


def write_image(repo, name, data):
    p = image_path(repo, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def process_image(data, base=None):
    """Strip metadata and return {filename: bytes} to write.

    JPEG/PNG/WebP -> <base>_d.webp (display, <=1600px) + <base>_f.webp (full).
    GIF -> single <base>.gif (metadata stripped, animation preserved).
    Re-encoding drops EXIF/metadata; orientation is applied first so photos
    aren't left rotated once the EXIF tag is gone.
    """
    if Image is None:
        raise GitError("Pillow not installed")
    base = base or f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    img = Image.open(BytesIO(data))
    img.load()
    img = ImageOps.exif_transpose(img)
    fmt = (img.format or "").upper()
    if fmt == "GIF":
        buf = BytesIO()
        img.save(buf, "GIF", save_all=True, optimize=True)
        return {f"{base}.gif": buf.getvalue()}
    full = BytesIO()
    img.save(full, "WEBP", quality=90)
    disp = img
    if img.width > DISPLAY_MAX:
        h = int(img.height * DISPLAY_MAX / img.width)
        disp = img.resize((DISPLAY_MAX, h), Image.LANCZOS)
    buf = BytesIO()
    disp.save(buf, "WEBP", quality=80)
    return {
        f"{base}_d.webp": buf.getvalue(),
        f"{base}_f.webp": full.getvalue(),
    }


def orphan_images(repo):
    img_dir = repo / "assets" / "images"
    if not img_dir.is_dir():
        return []
    used = set()
    posts_dir = repo / "_posts"
    if posts_dir.is_dir():
        for f in posts_dir.glob("*.md"):
            used.update(re.findall(
                r"assets/images/([\w.-]+\.(?:png|jpe?g|gif|webp))",
                f.read_text(encoding="utf-8", errors="replace"),
            ))
    orphans = []
    for f in img_dir.iterdir():
        name = f.name
        if not re.fullmatch(r"[\w.-]+\.(?:png|jpe?g|gif|webp)", name):
            continue
        if name in used:
            continue
        # sibling rule: _f.webp (lightbox full) is owned by its _d.webp display
        if name.endswith("_d.webp") and name[:-7] + "_f.webp" in used:
            continue
        if name.endswith("_f.webp") and name[:-7] + "_d.webp" in used:
            continue
        orphans.append(name)
    return orphans


def migrate_images(repo):
    """One-shot: convert old single-file uploads to _d/_f pairs and rewrite
    every post reference to point at the _d.webp display version."""
    img_dir = repo / "assets" / "images"
    if not img_dir.is_dir():
        return
    old = [
        f for f in img_dir.iterdir()
        if re.fullmatch(r"[\w.-]+\.(?:png|jpe?g|gif|webp)", f.name)
        and not (f.name.endswith("_d.webp") or f.name.endswith("_f.webp"))
    ]
    for f in old:
        outputs = process_image(f.read_bytes(), base=f.stem)
        f.unlink()
        for out_name, blob in outputs.items():
            write_image(repo, out_name, blob)
        new_name = next((n for n in outputs if n.endswith("_d.webp")), list(outputs)[0])
        old_ref = f"assets/images/{f.name}"
        new_ref = f"assets/images/{new_name}"
        if old_ref == new_ref:
            continue
        for p in (repo / "_posts").glob("*.md"):
            text = p.read_text(encoding="utf-8")
            if old_ref in text:
                p.write_text(text.replace(old_ref, new_ref), encoding="utf-8")


def gc_orphan_images(repo):
    # unlink, not `git rm`: git add -A stages tracked deletions and ignores
    # untracked strays, so an untracked file can't 500 every subsequent write.
    for f in orphan_images(repo):
        (repo / "assets" / "images" / f).unlink(missing_ok=True)


def list_posts(repo):
    return sorted(
        (f.name for f in (repo / "_posts").glob("*.md")),
        reverse=True,
    )


def read_post(repo, name):
    p = post_path(repo, name)
    if not p.exists():
        raise GitError("not found")
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        body = text.split("\n---\n", 1)
        if len(body) == 2:
            return body[1].lstrip("\n")
    return text


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_index(self):
        body = (HERE / "index.html").read_text(encoding="utf-8").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name):
        p = HERE / name
        if not re.fullmatch(r"[\w.-]+", name) or not p.is_file():
            return self._send_json({"error": "not found"}, 404)
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_repo_image(self, name):
        try:
            p = image_path(REPO_DIR, name)
        except GitError:
            return self._send_json({"error": "not found"}, 404)
        if not p.is_file():
            return self._send_json({"error": "not found"}, 404)
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        return json.loads(raw) if raw else {}

    def _name(self, path):
        return path[len("/api/posts/"):]

    def _write(self, mutate, msg, gc=False):
        try:
            git_flow(REPO_DIR, mutate, msg, gc=gc)
        except (GitError, subprocess.TimeoutExpired) as e:
            self._send_json({"error": str(e)}, 500)
            return False
        return True

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/":
            self._send_index()
        elif u.path == "/api/posts":
            self._send_json({"posts": list_posts(REPO_DIR)})
        elif u.path.startswith("/api/posts/"):
            try:
                name = self._name(u.path)
                self._send_json({"name": name, "content": read_post(REPO_DIR, name)})
            except GitError as e:
                self._send_json({"error": str(e)}, 404)
        elif u.path.startswith("/assets/images/"):
            self._send_repo_image(u.path[len("/assets/images/"):])
        else:
            self._send_static(u.path.lstrip("/"))

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == "/api/posts":
            content = (self._read_body().get("content") or "").strip()
            if not content:
                return self._send_json({"error": "empty post"}, 400)
            name = new_post_filename()
            if self._write(lambda: write_post(REPO_DIR, name, content), f"post: {name}", gc=True):
                self._send_json({"name": name})
        elif u.path == "/api/images":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = IMAGE_EXTS.get(ctype)
            if not ext:
                return self._send_json({"error": "unsupported image type"}, 400)
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n == 0:
                return self._send_json({"error": "empty image"}, 400)
            if n > MAX_IMAGE_BYTES:
                return self._send_json({"error": "image too large (max 15MB)"}, 413)
            data = self.rfile.read(n)
            try:
                outputs = process_image(data)
            except GitError as e:
                return self._send_json({"error": str(e)}, 500)
            except Exception as e:
                return self._send_json({"error": f"could not process image: {e}"}, 400)
            # Stage as untracked files: no git_flow, no push. The next post
            # save picks them up via `git add -A` and ships them in that commit.
            with LOCK:
                for fname, blob in outputs.items():
                    write_image(REPO_DIR, fname, blob)
                fix_owner(REPO_DIR)
            name = next((n for n in outputs if n.endswith("_d.webp")), list(outputs)[0])
            self._send_json({"name": name,
                             "markdown": f"![image]({{{{ site.baseurl }}}}/assets/images/{name})"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        u = urlsplit(self.path)
        if u.path.startswith("/api/posts/"):
            name = self._name(u.path)
            content = (self._read_body().get("content") or "").strip()
            if not content:
                return self._send_json({"error": "empty post"}, 400)
            if self._write(lambda: write_post(REPO_DIR, name, content), f"edit: {name}", gc=True):
                self._send_json({"name": name})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urlsplit(self.path)
        if u.path.startswith("/api/posts/"):
            name = self._name(u.path)
            if self._write(lambda: delete_post(REPO_DIR, name), f"delete: {name}", gc=True):
                self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def selftest():
    base = Path(tempfile.mkdtemp(prefix="logli-selftest-"))
    remote = base / "remote.git"
    work = base / "work"
    verify = base / "verify"
    img_dir = work / "assets" / "images"

    (work / "_posts").mkdir(parents=True)
    (work / "_posts" / ".gitkeep").touch()
    run_git(work, "init", "-b", "main")
    run_git(work, "init", "--bare", remote)
    run_git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    run_git(work, "add", "-A")
    run_git(work, "commit", "-m", "init")
    run_git(work, "remote", "add", "origin", remote)
    run_git(work, "push", "-u", "origin", "main")

    name = new_post_filename()
    git_flow(work, lambda: write_post(work, name, "hello **world**"), f"post: {name}")
    assert read_post(work, name) == "hello **world**"
    git_flow(work, lambda: write_post(work, name, "edited line"), f"edit: {name}")
    git_flow(work, lambda: delete_post(work, name), f"delete: {name}")
    assert not post_path(work, name).exists()

    img_dir.mkdir(parents=True, exist_ok=True)
    base1 = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    d1, f1 = f"{base1}_d.webp", f"{base1}_f.webp"
    write_image(work, d1, b"dd")  # staged display
    write_image(work, f1, b"ff")  # staged full (lightbox original)
    status_out = run_git(work, "status", "--porcelain", "--untracked-files=all")
    assert f"?? assets/images/{d1}" in status_out and f"?? assets/images/{f1}" in status_out

    p2 = new_post_filename()
    git_flow(work, lambda: write_post(
        work, p2, f"see ![image]({{{{ site.baseurl }}}}/assets/images/{d1})"),
        f"post: {p2}", gc=True)
    assert (img_dir / d1).exists() and (img_dir / f1).exists()  # _f kept while _d referenced

    git_flow(work, lambda: delete_post(work, p2), f"delete: {p2}", gc=True)
    assert not (img_dir / d1).exists() and not (img_dir / f1).exists()  # both GC'd

    d2 = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}_d.webp"
    write_image(work, d2, b"xx")  # staged, never referenced
    p3 = new_post_filename()
    git_flow(work, lambda: write_post(work, p3, "no images here"), f"post: {p3}", gc=True)
    assert not (img_dir / d2).exists()

    stray = img_dir / "stray.png"
    stray.write_bytes(b"zzz")  # untracked file dropped in, survives sync
    p4 = new_post_filename()
    git_flow(work, lambda: write_post(work, p4, "cleanup"), f"post: {p4}", gc=True)
    assert not stray.exists()  # GC removed it without a git rm 128 failure

    if Image is not None:
        src = Image.new("RGB", (2000, 1000), "red")
        exif = Image.Exif()
        exif[274] = 1  # Orientation (normal) - just to exercise EXIF stripping
        buf = BytesIO()
        src.save(buf, "JPEG", exif=exif.tobytes())
        outs = process_image(buf.getvalue())
        dname = next(n for n in outs if n.endswith("_d.webp"))
        fname = next(n for n in outs if n.endswith("_f.webp"))
        with Image.open(BytesIO(outs[fname])) as full_img:
            assert full_img.width == 2000 and full_img.height == 1000
            assert not full_img.getexif()  # EXIF stripped
        with Image.open(BytesIO(outs[dname])) as disp_img:
            assert disp_img.width == DISPLAY_MAX and disp_img.height == 800
        assert len(outs[dname]) < len(outs[fname])

        old = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}.jpg"
        write_image(work, old, buf.getvalue())  # old single-file upload
        p5 = new_post_filename()
        git_flow(work, lambda: write_post(
            work, p5, f"![o]({{{{ site.baseurl }}}}/assets/images/{old})"),
            f"post: {p5}", gc=True)
        assert (img_dir / old).exists()
        git_flow(work, lambda: migrate_images(work), "migrate images", gc=True)
        assert not (img_dir / old).exists()
        new_ref = f"assets/images/{old[:-4]}_d.webp"
        assert read_post(work, p5) == f"![o]({{{{ site.baseurl }}}}/{new_ref})"
        assert (img_dir / f"{old[:-4]}_d.webp").exists()
        assert (img_dir / f"{old[:-4]}_f.webp").exists()

    log = run_git(work, "log", "--format=%s", "origin/main")
    assert log.count("image:") == 0
    assert log.count("edit:") == 1 and log.count("delete:") == 2
    if Image is not None:
        assert log.count("post:") == 5 and log.count("migrate images") == 1
    else:
        assert log.count("post:") == 4

    run_git(base, "clone", remote, verify)
    assert run_git(verify, "rev-parse", "HEAD") == run_git(work, "rev-parse", "origin/main")
    assert not post_path(verify, name).exists()
    assert read_post(verify, p3) == "no images here"
    v_img = verify / "assets" / "images"
    if Image is not None:
        assert read_post(verify, p5) == f"![o]({{{{ site.baseurl }}}}/{new_ref})"
        assert (v_img / f"{old[:-4]}_d.webp").exists()
        assert (v_img / f"{old[:-4]}_f.webp").exists()
        assert orphan_images(verify) == []
    else:
        assert not v_img.is_dir() or not any(v_img.glob("*"))
    added = run_git(verify, "log", "--all", "--diff-filter=A", "--format=%s",
                    "--", f"assets/images/{d1}")
    assert added == f"post: {p2}"  # image shipped inside the post commit


def main():
    if "--selftest" in sys.argv:
        selftest()
        print("selftest: OK")
        return
    if "--migrate-images" in sys.argv:
        git_flow(REPO_DIR, lambda: migrate_images(REPO_DIR),
                 "migrate images: display+full split", gc=True)
        print("migrated")
        return
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Logli editor on http://0.0.0.0:{PORT} (repo: {REPO_DIR})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
