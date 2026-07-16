# Reconstruct the current deployed Atlas source

The authoritative source for this branch is the uploaded archive:

`atlas-fable-m2-current-snapshot.zip`

Archive SHA-256:

`31bab6e16af95b35c1ad73be9b5f72303a740e12740b2cc4aa2d5d4bcd67ba24`

The root source files inherited from older public branches are stale. Do not review or modify them as the current product.

## In a Claude sandbox with no network

Use the GitHub connector to fetch `atlas-fable-m2-current-snapshot.zip` from branch `atlas-fable-m2-current`. Request the file through the GitHub contents API as base64 or as a connector file reference, then save it locally.

Verify it before extraction:

```bash
python - <<'PY'
from pathlib import Path
import hashlib
p = Path('atlas-fable-m2-current-snapshot.zip')
actual = hashlib.sha256(p.read_bytes()).hexdigest()
expected = '31bab6e16af95b35c1ad73be9b5f72303a740e12740b2cc4aa2d5d4bcd67ba24'
assert actual == expected, (actual, expected)
print('verified', actual)
PY
```

Extract into a fresh workspace, not over the stale branch checkout:

```bash
mkdir -p atlas-current
python - <<'PY'
from pathlib import Path
import zipfile
archive = Path('atlas-fable-m2-current-snapshot.zip')
out = Path('atlas-current').resolve()
out.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(archive) as zf:
    for item in zf.infolist():
        target = (out / item.filename).resolve()
        if target != out and out not in target.parents:
            raise SystemExit(f'unsafe path: {item.filename}')
    zf.extractall(out)
print(out)
PY
```

Then work only inside `atlas-current/`.

The archive already contains the current `CLAUDE.md`, `FABLE5_PROMPT.md`, `docs/LIVE_PRODUCT_GAPS.md`, `docs/NEXT_ACCEPTANCE_MATRIX.md`, production source, packages, scripts, and tests. Read those extracted files before editing.

Do not claim the public branch itself is the production deployment. The private repository and Railway deployment remain managed by ChatGPT after artifact review.
