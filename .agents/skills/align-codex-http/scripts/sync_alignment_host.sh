#!/usr/bin/env bash
set -euo pipefail

# Sync tools/codex_alignment from an exact git commit to /opt/sub2api-alignment
# using `git archive` (avoiding macOS AppleDouble ._* files), refresh build.json,
# and run the host monitor unit suite. Dry-run by default; pass --execute to apply.

HOST="sub2api-tencent"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
COMMIT=""
TAG=""
EXECUTE="false"

usage() {
  cat <<'EOF'
Usage: sync_alignment_host.sh --commit <full-sha> --tag <vX.Y.Z> [--host <ssh-alias>] [--repo <path>] [--execute]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) COMMIT="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --repo) REPO_ROOT="${2:-}"; shift 2 ;;
    --execute) EXECUTE="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$COMMIT" || -z "$TAG" ]]; then
  echo "Both --commit and --tag are required." >&2
  usage >&2
  exit 2
fi

RESOLVED_SHA="$(git -C "$REPO_ROOT" rev-parse --verify "${COMMIT}^{commit}")"
FILE_LIST="$(git -C "$REPO_ROOT" ls-tree -r --name-only "$RESOLVED_SHA" -- tools/codex_alignment)"
FILE_COUNT="$(printf '%s\n' "$FILE_LIST" | grep -c .)"

echo "mode=$([[ "$EXECUTE" == "true" ]] && echo execute || echo dry-run)"
echo "host=$HOST"
echo "repo=$REPO_ROOT"
echo "tag=$TAG"
echo "commit=$RESOLVED_SHA"
echo "tracked_files=$FILE_COUNT"

if [[ "$EXECUTE" != "true" ]]; then
  echo "Dry-run complete. Re-run with --execute to sync /opt/sub2api-alignment and run host unit tests."
  exit 0
fi

LOCAL_TAR="$(mktemp "${TMPDIR:-/tmp}/alignment-sync-XXXXXX.tar")"
REMOTE_TAR="/tmp/alignment-sync-${RESOLVED_SHA:0:12}-$$.tar"
cleanup_local() { rm -f "$LOCAL_TAR"; }
trap cleanup_local EXIT

git -C "$REPO_ROOT" archive --format=tar "$RESOLVED_SHA" tools/codex_alignment > "$LOCAL_TAR"
scp -q "$LOCAL_TAR" "${HOST}:${REMOTE_TAR}"

ssh "$HOST" "sudo bash -se -- $(printf '%q %q %q' "$REMOTE_TAR" "$RESOLVED_SHA" "$TAG")" <<'REMOTE'
set -euo pipefail
REMOTE_TAR="$1"
COMMIT="$2"
TAG="$3"
trap 'rm -f "$REMOTE_TAR"' EXIT

tar -xf "$REMOTE_TAR" -C /opt/sub2api-alignment
rm -f /opt/sub2api-alignment/tools/codex_alignment/._*

BIN_SHA="$(docker exec sub2api-app sha256sum /app/sub2api | awk '{print $1}')"
python3 - "$COMMIT" "$TAG" "$BIN_SHA" <<'PY'
import hashlib, json, pathlib, sys, time
commit, tag, bin_sha = sys.argv[1], sys.argv[2], sys.argv[3]
root = pathlib.Path("/opt/sub2api-alignment")
build_path = root / "build.json"
data = json.loads(build_path.read_text()) if build_path.exists() else {"schema": 1, "repository": "BAIMOoo/sub2api"}
files = {}
for path in sorted((root / "tools" / "codex_alignment").iterdir()):
    if path.is_file() and not path.name.startswith("._") and path.suffix in (".py", ".txt"):
        files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
if data.get("source_commit") and data.get("source_commit") != commit:
    data["previous_source_commit"] = data["source_commit"]
data["source_commit"] = commit
data["production_source_commit"] = commit
data["production_binary_sha256"] = bin_sha
data["commit"] = commit
data["tag"] = tag
data["files"] = files
data["synced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
build_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
print(f"build_json_refreshed=ok files={len(files)} bin_sha={bin_sha}")
PY

(cd /opt/sub2api-alignment && /opt/sub2api-alignment/venv/bin/python -m unittest discover -s tools/codex_alignment -p 'test_*.py')
REMOTE
