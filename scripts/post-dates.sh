#!/usr/bin/env bash
# Assign each undated post a date from the first git commit that added it.
# Renames  _posts/foo.md  ->  _posts/<date>-foo.md  so Jekyll sorts/orders it.
# An explicit `date:` in the front matter wins (for bulk back-dating).
set -euo pipefail

shopt -s nullglob
cd "$(dirname "$0")/.."

for f in _posts/*.md; do
  name=$(basename "$f")

  # Already dated (YYYY-MM-DD-...) — leave alone.
  if [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}- ]]; then
    continue
  fi

  date=""
  if head -1 "$f" | grep -q '^---'; then
    date=$(awk '/^---/{c++;next} c==1 && /^date:/{print $2; exit}' "$f")
  fi

  if [[ -z "$date" ]]; then
    # First commit that ever added this file; `tail -1` = oldest.
    date=$(git log --diff-filter=A --follow --format=%cs -- "$f" | tail -1)
  fi

  if [[ -z "$date" ]]; then
    echo "post-dates: no git date for $name, skipping" >&2
    continue
  fi

  mv "$f" "$(dirname "$f")/${date}-${name}"
  echo "post-dates: ${name} -> ${date}-${name}"
done