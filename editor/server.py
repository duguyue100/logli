#!/usr/bin/env python3
"""Logli microblog editor: stdlib-only HTTP server + git client.

Runs inside Docker against a mounted checkout of the Jekyll repo at REPO_DIR.
Every write (create/edit/delete) does: fetch+reset origin/main, mutate a
post in _posts/, commit, push. GitHub Actions then rebuilds the site.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.environ.get("REPO_DIR", "/repo")
PORT = int(os.environ.get("PORT", "8000"))
GIT_NAME = os.environ.get("GIT_NAME", "logli-editor")
GIT_EMAIL = os.environ.get("GIT_EMAIL", "logli-editor@localhost")
EDITOR_UID = int(os.environ.get("EDITOR_UID", "501"))
EDITOR_GID = int(os.environ.get("EDITOR_GID", "20"))
TZ = "Europe/Zurich"

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
    run_git(repo, "fetch", "origin", "main")
    run_git(repo, "reset", "--hard", "origin/main")


def git_flow(repo, mutate, msg):
    # ponytail: global lock; the editor is the only writer, so serialize all ops.
    with LOCK:
        sync(repo)
        mutate()
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
    name = os.path.basename(name)
    if not re.fullmatch(r"[\w.-]+\.md", name):
        raise GitError("bad post name")
    return os.path.join(repo, "_posts", name)


def new_post_filename():
    now = datetime.now(ZoneInfo(TZ))
    return f"{now:%Y-%m-%d}-{int(time.time() * 1000)}.md"


def write_post(repo, name, content):
    p = post_path(repo, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("---\n---\n\n")
        f.write(content)


def delete_post(repo, name):
    run_git(repo, "rm", "-f", os.path.relpath(post_path(repo, name), repo))


def list_posts(repo):
    return sorted(
        (f for f in os.listdir(os.path.join(repo, "_posts")) if f.endswith(".md")),
        reverse=True,
    )


def read_post(repo, name):
    p = post_path(repo, name)
    if not os.path.exists(p):
        raise GitError("not found")
    text = open(p, encoding="utf-8").read()
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
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
            body = f.read().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        return json.loads(raw) if raw else {}

    def _name(self, path):
        return path[len("/api/posts/"):]

    def _write(self, mutate, msg):
        try:
            git_flow(REPO_DIR, mutate, msg)
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
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == "/api/posts":
            content = (self._read_body().get("content") or "").strip()
            if not content:
                return self._send_json({"error": "empty post"}, 400)
            name = new_post_filename()
            if self._write(lambda: write_post(REPO_DIR, name, content), f"post: {name}"):
                self._send_json({"name": name})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        u = urlsplit(self.path)
        if u.path.startswith("/api/posts/"):
            name = self._name(u.path)
            content = (self._read_body().get("content") or "").strip()
            if not content:
                return self._send_json({"error": "empty post"}, 400)
            if self._write(lambda: write_post(REPO_DIR, name, content), f"edit: {name}"):
                self._send_json({"name": name})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urlsplit(self.path)
        if u.path.startswith("/api/posts/"):
            name = self._name(u.path)
            if self._write(lambda: delete_post(REPO_DIR, name), f"delete: {name}"):
                self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def selftest():
    base = tempfile.mkdtemp(prefix="logli-selftest-")
    remote = os.path.join(base, "remote.git")
    work = os.path.join(base, "work")
    verify = os.path.join(base, "verify")

    os.makedirs(os.path.join(work, "_posts"))
    open(os.path.join(work, "_posts", ".gitkeep"), "w").close()
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
    assert read_post(work, name) == "edited line"

    git_flow(work, lambda: delete_post(work, name), f"delete: {name}")
    assert not os.path.exists(post_path(work, name))

    log = run_git(work, "log", "--format=%s", "origin/main")
    assert log.count("post:") == 1 and log.count("edit:") == 1 and log.count("delete:") == 1

    run_git(base, "clone", remote, verify)
    assert run_git(verify, "rev-parse", "HEAD") == run_git(work, "rev-parse", "origin/main")
    assert run_git(verify, "show", f"HEAD~1:_posts/{name}") == "---\n---\n\nedited line"
    assert not os.path.exists(os.path.join(verify, "_posts", name))


def main():
    if "--selftest" in sys.argv:
        selftest()
        print("selftest: OK")
        return
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Logli editor on http://0.0.0.0:{PORT} (repo: {REPO_DIR})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
