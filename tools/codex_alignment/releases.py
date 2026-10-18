"""Install official binaries without executing release scripts or tar paths."""
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
from urllib.request import Request, urlopen

from .evidence import atomic_json


def file_sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1 << 20), b''):
            value.update(chunk)
    return value.hexdigest()


def prune_binaries(directory, keep):
    """Retain manifests in reports; only the approved/current executables stay cached."""
    import shutil
    for child in Path(directory).iterdir():
        if child.name not in keep and re.fullmatch(r'\d+\.\d+\.\d+', child.name) and child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)


def github(path):
    req = Request('https://api.github.com/' + path,
                  headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'sub2api-codex-alignment'})
    with urlopen(req, timeout=30) as response:
        raw = response.read(8 << 20)
    return json.loads(raw)


def release(version=None):
    endpoint = 'latest' if version is None else 'tags/rust-v' + version
    info = github('repos/openai/codex/releases/' + endpoint)
    tag = info.get('tag_name', '')
    if not re.fullmatch(r'rust-v\d+\.\d+\.\d+', tag) or info.get('prerelease') or info.get('draft'):
        raise ValueError('stable_release_required')
    return info


def install(directory, info, target='x86_64-unknown-linux-musl', component='codex'):
    if component not in {'codex', 'codex-code-mode-host'}:
        raise ValueError('unexpected_official_component')
    version = info['tag_name'].removeprefix('rust-v')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('invalid_release_version')
    asset_name = component + '-' + target + '.tar.gz'
    assets = [a for a in info['assets'] if a['name'] == asset_name]
    if len(assets) != 1:
        raise ValueError('official_binary_missing')
    asset = assets[0]
    expected = asset.get('digest', '')
    if not re.fullmatch('sha256:[0-9a-f]{64}', expected):
        raise ValueError('official_asset_digest_missing')
    url = asset['browser_download_url']
    if url != 'https://github.com/openai/codex/releases/download/rust-v' + version + '/' + asset_name:
        raise ValueError('unexpected_release_url')
    root = Path(directory) / version
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = root / component
    manifest = root / ('provenance.json' if component == 'codex' else component + '.provenance.json')
    if binary.exists() and manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved.get('asset_sha256') == expected[7:] and saved.get('binary_sha256') == file_sha(binary):
            return binary, saved
        raise ValueError('installed_binary_digest_changed')
    fd, archive = tempfile.mkstemp(prefix='.download-', dir=root)
    tmp_binary = root / ('.' + component + '-pending')
    try:
        sha, total = hashlib.sha256(), 0
        with os.fdopen(fd, 'wb') as f, urlopen(Request(url, headers={'User-Agent': 'sub2api-alignment'}), timeout=60) as r:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > 250 << 20:
                    raise ValueError('release_asset_too_large')
                sha.update(chunk)
                f.write(chunk)
        if total != asset['size'] or sha.hexdigest() != expected[7:]:
            raise ValueError('release_asset_digest_mismatch')
        with tarfile.open(archive, 'r:gz') as tar:
            members = tar.getmembers()
            candidates = [m for m in members if m.isfile() and Path(m.name).name == component + '-' + target]
            if len(candidates) != 1 or candidates[0].size > 400 << 20:
                raise ValueError('unexpected_binary_archive')
            with tar.extractfile(candidates[0]) as source, tmp_binary.open('wb') as out:
                import shutil
                shutil.copyfileobj(source, out)
        tmp_binary.chmod(0o700)
        saved = {'version': version, 'target': target, 'asset_sha256': expected[7:],
                 'binary_sha256': file_sha(tmp_binary),
                 'release_id': info['id']}
        os.replace(tmp_binary, binary)
        atomic_json(manifest, saved)
        return binary, saved
    finally:
        Path(archive).unlink(missing_ok=True)
        tmp_binary.unlink(missing_ok=True)
