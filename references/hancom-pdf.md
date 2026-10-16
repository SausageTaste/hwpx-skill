# Local Hancom PDF export

Use `scripts/export_pdf.py` on Windows with a working desktop Hancom installation
that can open the input HWPX and export it to PDF. The COM identifier is
`HWPFrame.HwpObject`; `Open(path, "", "")` lets Hancom detect the document format,
and `SaveAs(path, "PDF", "")` invokes its local PDF renderer. No document upload
or server conversion is performed by this tool.

Install pywin32 in the Python environment running the script if it is absent:

```text
python -m pip install pywin32
python <skill-dir>/scripts/export_pdf.py input.hwpx output.pdf --timeout 180
```

The input must be a valid HWPX ZIP package. Output must be a new `.pdf` path.
Successful export prints JSON containing renderer version, source/output hashes,
byte count, and preservation checks. Exit 0 means export completed; exit 2 means
an error. Export success alone does not establish visual equivalence.

## File-access approvals

Hancom may display a file-access approval dialog when automation opens or saves
local files. The script leaves the Hancom window visible, does not suppress
dialogs, and does not install DLLs or change registry/security settings. If the
dialog requires consent, let the user handle it. Do not claim the default command
is guaranteed to run unattended.

If an authorized administrator has already configured a trusted Hancom security
module, the user can select its registered name explicitly:

```text
python <skill-dir>/scripts/export_pdf.py input.hwpx output.pdf --security-module REGISTERED-NAME
```

This calls `RegisterModule("FilePathCheckDLL", name)` for that COM session and
fails if Hancom rejects it. It does not install or persistently register the
module. Do not substitute a guessed name, download an approval-bypass DLL, or
perform module installation as part of an ordinary export request.

## Failure and validation

- If COM activation fails, verify desktop Hancom is installed, activated, and
  usable interactively. A viewer-only installation may not provide automation.
- On timeout, the Python worker stops. Its Hancom session may remain open; a
  locked staging directory may also remain. Do not terminate unrelated Hancom
  processes. Inspect the app's state before one informed retry.
- A failed export does not publish a final PDF. Handled copy failures remove the
  newly created partial output. A force-killed process or power loss can still
  leave temporary/partial files; inspect them before reusing an output path.
- Source hashes are checked before publishing. The PDF header and EOF marker
  are checked; detailed page validation uses `scripts/compare_pdf.py` and PyMuPDF.
- For regression checks, compare automated output with a manual export from the
  same Hancom version and settings. Record the observed result rather than
  promising every document will have identical layout under all versions.

Hancom references: [automation setup](https://developer.hancom.com/en-us/hwpautomation)
and [PDF SaveAs example](https://forum.developer.hancom.com/t/insertpicture-hwp-pdf/2247).
