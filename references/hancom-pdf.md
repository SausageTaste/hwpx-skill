# Local Hancom PDF export

Use `scripts/export_pdf.py` on Windows with a working desktop Hancom installation
that can open the input HWPX and export it to PDF. The COM identifier is
`HWPFrame.HwpObject`; `Open(path, "", "")` lets Hancom detect the document format,
and `SaveAs(path, "PDF", "")` invokes its local PDF renderer. No document upload
or server conversion is performed by this tool.

Install pywin32 in the Python environment running the script if it is absent:

```text
python -m pip install pywin32
python <skill-dir>/scripts/export_pdf.py input.hwpx output.pdf --timeout 300
```

The input must be a valid HWPX ZIP package. Output must be a new `.pdf` path.
Successful export prints JSON containing renderer version, source/output hashes,
byte count, and preservation checks. Exit 0 means export completed; exit 2 means
an error. Export success alone does not establish visual equivalence.

The default limit is 300 seconds for the entire worker, including opening,
rendering, and closing. Waiting for file-access consent also uses this time.
Large documents can take more than 45 seconds even when
export is working. Progress goes to stderr: the current stage and, every 15
seconds while waiting, elapsed time and the staged PDF size. A growing file is
evidence of progress; no growth alone does not prove the app is blocked. JSON
on stdout remains machine-readable and includes total elapsed time. Increase
`--timeout` for slower machines or larger documents.

## File-access approvals

Hancom may display a file-access approval dialog when automation opens or saves
local files. The script leaves the Hancom window visible, does not suppress
dialogs, and does not install DLLs or change registry/security settings. If the
dialog requires consent, let the user handle it. Do not claim the default command
is guaranteed to run unattended.

### Return later and approve

Use a detached job when the user wants to return later, click approval, and
notify the agent in chat:

```text
python <skill-dir>/scripts/export_pdf_job.py start input.hwpx output.pdf export-job --timeout 86400 --wait 30
python <skill-dir>/scripts/export_pdf_job.py status export-job
```

`export-job` must be a new directory, separate from the output PDF. The defaults
are 24 hours for the export and 30 seconds for the launcher's initial check.
`--wait 0` returns immediately; the maximum initial check is 60 seconds. The
detached Python process continues when the launcher exits or the agent ends its
turn. Hancom's approval dialog stays available while its session is running.
Approval lets conversion continue without another chat message; the message
tells the agent to inspect the saved result.

The job directory contains `job.json` (paths and limits), `launcher.json` and
`runner.pid` (process identifiers), `progress.log`, and atomically updated
`status.json`. States are `queued`, `running`, `succeeded`, `failed`, or
`interrupted`. Only `succeeded` confirms publication of the final PDF and includes
the export's hashes and preservation checks. `start` returns exit 0 for either
a launched pending job or success; inspect its JSON state. Failed/interrupted
jobs return exit 2. `status` reads the same files without launching anything.

After the initial check, report the pending job path and end the turn rather
than repeatedly checking it. On the user's return, inspect that job's status
and recent log. Do not start a duplicate job or assume clicking approval means
conversion has already finished. Apply PDF verification when required.

Detachment does not survive shutdown, logoff, or force termination. A `running`
record may be stale after such an interruption; inspect the recorded process
and app before restarting. The 24-hour limit includes waiting for consent and
can be increased at launch. An expired job cannot be resumed by clicking an
old dialog. This helper does not install a scheduled task or a Windows service.
If Windows refuses process detachment, launch fails explicitly without falling
back to a process that may be killed with the launcher.

The helper uses Windows' documented [detached process and job breakaway flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags).

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
- On timeout, the Python worker stops. Hancom may still be rendering; timeout
  alone does not diagnose a dialog or failed conversion. The error includes
  the last stage and staging path. A locked staging directory may remain.
  Do not terminate unrelated Hancom processes. Inspect the app's state and
  allow an ongoing export to finish before retrying with a longer limit.
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
