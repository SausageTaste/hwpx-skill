---
name: hwpx
description: Pack and unpack HWPX documents, indent their XML without changing document text or layout settings, export PDFs locally with desktop Hancom on Windows, and compare before/after PDF exports. Use for HWPX package maintenance and source formatting, not legacy binary HWP conversion or document content authoring.
---

# HWPX

Use the bundled Python tools for package operations and conservative XML source
formatting. Resolve script paths relative to this skill; input and output paths
belong to the user's workspace. No repository-specific files are required.
Python 3.10+ is sufficient for packing and XML formatting. Local PDF export
requires Windows, desktop Hancom, and pywin32; PDF comparison requires PyMuPDF.

## Pack and unpack

```text
python <skill-dir>/scripts/hwpx_archive.py unpack input.hwpx package-directory
python <skill-dir>/scripts/hwpx_archive.py pack package-directory output.hwpx
```

- Preserve the original archive. Both commands require a new output path and
  refuse overwrites. Pack the directory containing `mimetype` directly, without
  introducing an extra parent directory inside the archive.
- Keep reports, scripts, and backups outside the package: packing includes all
  files beneath the supplied directory. Use an existing unpacked working copy
  when appropriate; do not replace it wholesale with a fresh extraction.
- The tools validate the HWPX mimetype, ZIP integrity, and safe package paths.
  They reject duplicate/case-colliding archive paths, links, and encrypted ZIP
  entries. `mimetype` is written first and uncompressed; resource bytes and empty
  directories are preserved. ZIP metadata is normalized, so archive hashes may
  change even when contained file bytes do not.
- Archive operations do not edit XML or certify document layout. Do not convert
  a legacy `.hwp` file by renaming its extension.

## Format XML without changing content

```text
python <skill-dir>/scripts/format_hwpx_xml.py package-directory --check --report check.json
python <skill-dir>/scripts/format_hwpx_xml.py package-directory --report result.json
```

Run the preview first and inspect its changed-file list and validation results.
Apply the same operation within the user's authorized scope. Reports must be
outside the package; existing report files at the explicitly supplied path may
be replaced. The package directory is always explicit.

The formatter changes only literal whitespace between child tags in known HWPX
structural containers, using four-space indentation. It preserves all other
bytes, including text, repeated spaces, text-only elements, attributes,
namespace prefixes, entity spelling, and inline children of `hp:t`. It leaves
unknown or mixed-content subtrees, `xml:space="preserve"` subtrees, and containers
with intervening comments, processing instructions, CDATA, or character
references intact. Some XML therefore intentionally remains on long lines.

Do not substitute generic pretty printers, collapse whitespace in text nodes,
or reserialize whole XML trees just to indent them. The bundled formatter
supports UTF-8 XML 1.0 and rejects DTDs; unsupported input is left unchanged.
It reparses the output, verifies exact protected-byte preservation, checks for
concurrent package changes, and writes only changed files. Handled write failures
trigger restoration of files already written; this is not a transaction against
process termination or power loss.

Rerunning the formatter should report no changed files. Non-XML resources must
remain byte-identical. Formatting never repairs previously altered document
text: recover from a verified original only when that recovery is requested and
newer edits have been accounted for. Do not guess missing spaces or line breaks.

## Export PDF locally on Windows

Use desktop Hancom through its local COM API; document conversion does not use
Hancom web/server APIs. Read [the export guide](references/hancom-pdf.md) for
prerequisites, file-access approvals, and recovery from blocked exports.

```text
python <skill-dir>/scripts/export_pdf.py input.hwpx output.pdf
```

The exporter creates its own COM object, opens the document, calls PDF SaveAs,
and closes that session. It requires a new output path, stages the PDF before
publishing, checks the input hash, and enforces a 300-second timeout (adjust with
`--timeout SECONDS` for large documents). It verifies the PDF signature/end
marker; use the comparison helper for page rendering verification. It does not
change document content or install/configure a security module. Stage messages,
elapsed time, and staged PDF size appear on stderr; the result JSON uses stdout.

An approval dialog can require the user's action. Do not click through security
prompts or retry indefinitely. A preconfigured, trusted Hancom module can be
selected explicitly with `--security-module NAME`; never register arbitrary
modules or edit security settings as part of export. On timeout, report that
Hancom may still be rendering and inspect its state before retrying with a longer
limit. A timeout does not by itself prove export failure or a dialog. The exporter
stops its Python worker without killing other Hancom processes.

For a user who will return later to approve Hancom, use the detached job helper:

```text
python <skill-dir>/scripts/export_pdf_job.py start input.hwpx output.pdf export-job
python <skill-dir>/scripts/export_pdf_job.py status export-job
```

The job runs independently with a 24-hour timeout. The launcher checks for up
to 30 seconds, then returns even if export is still pending (`--timeout` and
`--wait` adjust these limits). Keep the job directory path in your response and
end the turn if waiting for the user; do not continue polling indefinitely.
When the user reports approval, check that same job and verify its PDF instead
of launching another export. A pending status is not successful conversion.
Read the export guide for persisted status, logs, and interruption limitations.

## Verify appearance when required

Content preservation and demonstrated visual equivalence are different checks.
To verify appearance, export before/after PDFs locally when the export
prerequisites are available, or obtain them from the user. Use the same renderer,
fonts, and export settings for both, then run:

```text
python <skill-dir>/scripts/compare_pdf.py before.pdf after.pdf --report comparison.json
```

Only this helper requires PyMuPDF (`python -m pip install pymupdf` if needed).
It compares all corresponding pages at 300 DPI in opaque RGB with zero pixel
tolerance, records dimensions and render hashes, and compares extracted text.
Missing pages are differences. It refuses to overwrite an existing report or
write a report over either input. Exit codes: 0 for visual and text equality,
1 for a completed comparison with differences, 2 for an execution error.

Inspect reported differing pages visually to explain spacing, wrapping, tables,
images, or pagination changes. Matching embedded HWPX preview images, matching
page counts, or matching extracted text alone do not prove identical appearance.
When PDFs or a renderer are unavailable, report content verification separately
and leave visual equivalence unverified. Do not claim universal rendering
compatibility from one document or renderer. The comparison helper does not
export documents; use the local export operation above when available.

## Validate the portable tools

```text
python -m unittest discover -s <skill-dir>/tests -t <skill-dir> -v
```

Tests use synthetic fixtures and temporary directories. Export tests mock
Hancom and exercise worker supervision with Python subprocesses; they do not
launch desktop apps. PDF comparison tests require PyMuPDF and
are skipped when it is unavailable; core archive and XML tests have no
third-party dependencies. No installation or document-specific authoring
workflow is implied by this skill.
