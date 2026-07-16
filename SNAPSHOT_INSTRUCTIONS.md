# Use the published current Atlas source

The authoritative production snapshot has already been verified and extracted server-side.

Use:

- source tree: `snapshot/tree/`
- per-file manifest: `snapshot/MANIFEST.sha256`
- original archive: `atlas-fable-m2-current-snapshot.zip`
- archive SHA-256: `31bab6e16af95b35c1ad73be9b5f72303a740e12740b2cc4aa2d5d4bcd67ba24`

The publishing workflow completed successfully after verifying the archive checksum and generated the manifest from the extracted files. The inherited root source remains stale and must not be used as the product baseline.

## Claude sandbox with no network

Do **not** fetch or decode the ZIP now. Read files directly from `snapshot/tree/` through the GitHub connector.

If the previous Claude conversation still has the verified M1.5 workspace at local head `9917aaf`, only six files differ from that local tree:

```text
CLAUDE.md
FABLE5_PROMPT.md
atlas/providers.py
docs/LIVE_PRODUCT_GAPS.md
docs/NEXT_ACCEPTANCE_MATRIX.md
tests/test_providers.py
```

Their current SHA-256 values are:

```text
901c9bb9af4ee5914bf1235f418e6df339d3c8fd734723c22f48e6b115562a60  CLAUDE.md
f9475d1fba20931df171d0742b8c036b9362ce486e21aabc0239d6d88ea5d2c0  FABLE5_PROMPT.md
aee9ab2c606896358b1df8e84e7b8ceb077edeb2b35c5522905814112796bc3d  atlas/providers.py
e6146ee35788fd27bbb6c4c93d84a63076841e42eef546db9f255276206f394  docs/LIVE_PRODUCT_GAPS.md
9934bc85a4311b7ef9a9008ddd062da9e9ba5d63a6d820773c213d4bc726a9dd  docs/NEXT_ACCEPTANCE_MATRIX.md
65509c1f0413fea38d28971d14c537ce2be52372295b6c6177f95b17bdadac3b  tests/test_providers.py
```

Efficient reconstruction:

1. Copy the intact local M1.5 workspace to a fresh `atlas-current/` directory.
2. Fetch those six files from `snapshot/tree/` and overwrite/add them in `atlas-current/`.
3. Read `snapshot/MANIFEST.sha256`.
4. Verify the full reconstructed local tree against that manifest. Existing banked hashes may be used to confirm unchanged files without retransmitting them.
5. Work only inside `atlas-current/`.

If the local M1.5 workspace is unavailable, reconstruct `atlas-current/` from the plain files under `snapshot/tree/`; no binary transfer is required.

Then read `snapshot/tree/CLAUDE.md`, `snapshot/tree/FABLE5_PROMPT.md`, every relevant source/test file, `docs/LIVE_PRODUCT_GAPS.md`, and `docs/NEXT_ACCEPTANCE_MATRIX.md` before editing.

Do not claim the public branch is production. ChatGPT manages private-repository review, merging, Railway deployment, and live verification after artifact audit.
