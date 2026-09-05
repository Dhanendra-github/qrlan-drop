# QRLAN Drop patch notes

## Version 2.0.0 — September 6, 2026

- Adopted the mint logo with the white QRLAN / blue DROP wordmark across the desktop and browser pages.
- Added a matching Windows application icon, browser favicon, and executable version metadata.

- Blue and charcoal dark interface with Send, Receive, and a separate History screen.
- Select multiple files, add a folder recursively, or drag files and folders into the selection.
- QR and link views, Copy Link, and Share Link with copy, email draft, and QR image export.
- Scan local transfer QR codes from an image or a camera; review the destination before opening it.
- Persistent local transfer history with Sent/Received filters and Open File/Open Folder actions.
- Main screens show only pending selections and general progress; transfer records appear only in History.
- Updated phone download page supports individual downloads from the selected files.
- Reliable source launcher and executable build include drag-and-drop and QR decoding dependencies.

### Reliability fixes

- Stop transfer now cancels active uploads and downloads and prevents cancelled uploads from being committed.
- Failed uploads reset the desktop status and show actionable errors for interrupted connections, unwritable folders, and full drives.
- Missing or unreadable source files show a useful error page instead of dropping the browser connection.
- Completed uploads retain their actual destination when the receive-folder preference changes.
- Transfer notifications are delivered on the desktop UI thread; updates from stopped sessions are discarded.
- Added regression coverage for cancellation, cleanup, concurrent uploads, error handling, and session tracking.

## Version 1.5.0 — August 3, 2026

### A beautiful new look

- Dark mode is now the default.
- The whole app has a new modern purple-and-cyan design.
- Buttons glow smoothly when you move the mouse over them.
- The transfer status light pulses while QRLAN Drop is working.
- The progress bar has a smooth animated highlight.
- The app gently fades in when it opens.
- The phone upload and download pages now match the dark design.

### New shortcuts

- **Open Folder** opens the folder containing your selected or newest received file.
- **Open File** opens your selected or newest received file.

### Fixed

- The extra command window no longer stays open.
- The white Windows title bar is now dark and matches the app.
- The QR panel no longer disappears when the window is narrow.
- Send and Receive screens fit correctly at common Windows 11 display sizes.

### Transfer features included

- Choose where received files are saved.
- See progress for large uploads and downloads.
- No artificial file-size limit. Your available disk space and Wi-Fi connection are the practical limits.
- Files travel directly over the local Wi-Fi network and are not uploaded to the cloud.
