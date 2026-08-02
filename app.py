from __future__ import annotations

import html
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

import qrcode
from PIL import ImageTk


APP_NAME = "QRLAN Drop"
CHUNK_SIZE = 64 * 1024


def readable_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def safe_filename(value: str) -> str:
    value = urllib.parse.unquote(value).replace("\\", "/").split("/")[-1]
    value = "".join(c for c in value if c >= " " and c not in '<>:"/\\|?*')
    value = value.strip(" .")
    return value[:180] or "received-file"


def available_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    stem, suffix = candidate.stem, candidate.suffix
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


def local_ipv4() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


@dataclass
class TransferSession:
    mode: str
    token: str
    file_path: Path | None = None
    receive_folder: Path | None = None


class TransferHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, session: TransferSession, notify):
        self.session = session
        self.notify = notify
        self.upload_lock = threading.Lock()
        super().__init__(address, TransferHandler)


class TransferHandler(BaseHTTPRequestHandler):
    server: TransferHTTPServer

    def log_message(self, _format, *_args):
        pass

    def _route(self) -> tuple[bool, str]:
        parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
        valid = len(parts) >= 2 and parts[0] == "t" and secrets.compare_digest(parts[1], self.server.session.token)
        tail = parts[2] if len(parts) > 2 else ""
        return valid, tail

    def _send_html(self, body: str, status=HTTPStatus.OK):
        encoded = page(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        valid, tail = self._route()
        if not valid:
            self._send_html("<h1>Link not found</h1><p>This transfer link is invalid or has expired.</p>", HTTPStatus.NOT_FOUND)
            return
        session = self.server.session
        if session.mode == "send" and tail == "download":
            self._download(session)
        elif session.mode == "send" and not tail:
            file_path = session.file_path
            assert file_path is not None
            name = html.escape(file_path.name)
            size = readable_size(file_path.stat().st_size)
            self._send_html(f'<div class="badge">Ready to download</div><h1>{name}</h1><p>{size}</p><a class="button" href="download">Download file</a><p class="note">Keep the Windows app open until the download finishes.</p>')
        elif session.mode == "receive" and not tail:
            self._send_html(upload_form(session.token))
        else:
            self._send_html("<h1>Link not found</h1>", HTTPStatus.NOT_FOUND)

    def _download(self, session: TransferSession):
        file_path = session.file_path
        if file_path is None or not file_path.is_file():
            self._send_html("<h1>File unavailable</h1><p>The sender may have stopped the transfer.</p>", HTTPStatus.GONE)
            return
        size = file_path.stat().st_size
        encoded_name = urllib.parse.quote(file_path.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent = 0
        report_every = max(CHUNK_SIZE, size // 100) if size else CHUNK_SIZE
        last_report = 0
        self.server.notify("Downloading…", 0, size)
        try:
            with file_path.open("rb") as source:
                while chunk := source.read(CHUNK_SIZE):
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    if sent - last_report >= report_every or sent == size:
                        self.server.notify("Downloading…", sent, size)
                        last_report = sent
            self.server.notify(f"Downloaded: {file_path.name}", size, size)
        except (BrokenPipeError, ConnectionResetError):
            self.server.notify("Download was cancelled", 0, 0)

    def do_POST(self):
        valid, tail = self._route()
        session = self.server.session
        if not valid or session.mode != "receive" or tail != "upload":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self.send_error(HTTPStatus.LENGTH_REQUIRED, "A valid file size is required")
            return
        filename = safe_filename(self.headers.get("X-Filename", "received-file"))
        folder = session.receive_folder
        assert folder is not None
        folder.mkdir(parents=True, exist_ok=True)
        with self.server.upload_lock:
            target = available_path(folder, filename)
            temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
            # Reserve the final name so simultaneous uploads cannot overwrite it.
            target.touch(exist_ok=False)
        remaining = length
        received = 0
        report_every = max(CHUNK_SIZE, length // 100) if length else CHUNK_SIZE
        last_report = 0
        self.server.notify("Receiving…", 0, length)
        try:
            with temporary.open("xb") as output:
                while remaining:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ConnectionError("Upload ended early")
                    output.write(chunk)
                    remaining -= len(chunk)
                    received += len(chunk)
                    if received - last_report >= report_every or received == length:
                        self.server.notify("Receiving…", received, length)
                        last_report = received
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            self.send_error(HTTPStatus.BAD_REQUEST, "Upload failed")
            return
        response = f'{{"name": {json_string(target.name)}}}'.encode("utf-8")
        self.send_response(HTTPStatus.CREATED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(response)
        self.server.notify(f"Received: {target.name}", length, length)


def json_string(value: str) -> str:
    import json
    return json.dumps(value)


def page(body: str) -> str:
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_NAME}</title><style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#eef2ff; color:#15213b; }}
main {{ width:min(86vw,420px); background:white; padding:32px; border-radius:24px; box-shadow:0 16px 50px #1e2a5a20; text-align:center; }}
h1 {{ font-size:1.45rem; overflow-wrap:anywhere; margin:14px 0 8px; }} p {{ color:#5b6478; }}
.button,button {{ display:block; width:100%; box-sizing:border-box; border:0; border-radius:12px; padding:14px; background:#3f5bea; color:white; font-weight:700; font-size:1rem; text-decoration:none; cursor:pointer; }}
.badge {{ display:inline-block; padding:6px 10px; border-radius:99px; background:#e8edff; color:#334fd2; font-size:.8rem; font-weight:700; }}
.picker {{ border:2px dashed #ccd4ef; border-radius:14px; padding:22px 12px; margin:22px 0 14px; }}
input {{ max-width:100%; }} progress {{ width:100%; height:14px; margin-top:14px; accent-color:#3f5bea; }} .note {{ font-size:.82rem; }} #status {{ min-height:1.4em; font-weight:600; color:#334fd2; }}
@media(prefers-color-scheme:dark) {{ body{{background:#111827;color:#f4f6ff}} main{{background:#1f2937}} p{{color:#bcc5d7}} }}
</style></head><body><main>{body}</main></body></html>"""


def upload_form(token: str) -> str:
    return f"""<div class="badge">Send to this PC</div><h1>Choose a file</h1><p>Files are sent directly over Wi-Fi.</p>
<div class="picker"><input id="file" type="file"></div><button id="send">Upload file</button><progress id="progress" max="100" value="0"></progress><p id="status"></p>
<script>
const input=document.querySelector('#file'), button=document.querySelector('#send'), status=document.querySelector('#status'), progress=document.querySelector('#progress');
const size=n=>{{let v=n,u='bytes';for(const x of ['KB','MB','GB','TB']){{if(v<1024)break;v/=1024;u=x;}}return (u==='bytes'?v:v.toFixed(1))+' '+u;}};
button.onclick=()=>{{
 const file=input.files[0]; if(!file){{status.textContent='Choose a file first.';return;}}
 button.disabled=true; progress.value=0; status.textContent='Starting upload…';
 const xhr=new XMLHttpRequest(); xhr.open('POST','/t/{token}/upload'); xhr.setRequestHeader('Content-Type','application/octet-stream'); xhr.setRequestHeader('X-Filename',encodeURIComponent(file.name));
 xhr.upload.onprogress=e=>{{if(e.lengthComputable){{const pct=Math.round(e.loaded/e.total*100);progress.value=pct;status.textContent='Uploading… '+pct+'% · '+size(e.loaded)+' / '+size(e.total);}}}};
 xhr.onload=()=>{{button.disabled=false;if(xhr.status===201){{progress.value=100;const data=JSON.parse(xhr.responseText);status.textContent='Sent: '+data.name;}}else status.textContent='Upload failed.';}};
 xhr.onerror=()=>{{button.disabled=false;status.textContent='Upload failed. Check that the PC app is still running.';}};
 xhr.send(file);
}};
</script>"""


class TransferService:
    def __init__(self, notify):
        self.notify = notify
        self.server: TransferHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""

    def start(self, session: TransferSession) -> str:
        self.stop()
        self.server = TransferHTTPServer(("0.0.0.0", 0), session, self.notify)
        port = self.server.server_address[1]
        self.url = f"http://{local_ipv4()}:{port}/t/{session.token}/"
        self.thread = threading.Thread(target=self.server.serve_forever, name="transfer-server", daemon=True)
        self.thread.start()
        return self.url

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.server = None
        self.thread = None
        self.url = ""


class QRLANApp(tk.Tk):
    BG = "#f4f6fb"
    CARD = "#ffffff"
    INK = "#17213a"
    MUTED = "#687086"
    ACCENT = "#4263eb"

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("720x610")
        self.minsize(680, 570)
        self.configure(bg=self.BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.service = TransferService(self.notify)
        self.file_path: Path | None = None
        self.receive_folder = Path.home() / "Downloads" / "QRLAN Drop"
        self.qr_photo = None
        self._styles()
        self._build()

    def _styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=self.BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(26, 11), font=("Segoe UI", 10, "bold"), background="#e7eaf3")
        style.map("TNotebook.Tab", background=[("selected", self.CARD)], foreground=[("selected", self.ACCENT)])
        style.configure("Transfer.Horizontal.TProgressbar", troughcolor="#e8ecf7", background=self.ACCENT, borderwidth=0)

    def _build(self):
        header = tk.Frame(self, bg=self.BG)
        header.pack(fill="x", padx=34, pady=(26, 14))
        tk.Label(header, text="QRLAN Drop", font=("Segoe UI", 23, "bold"), bg=self.BG, fg=self.INK).pack(anchor="w")
        tk.Label(header, text="Private file transfer over your Wi-Fi · No file-size limit", font=("Segoe UI", 10), bg=self.BG, fg=self.MUTED).pack(anchor="w", pady=(3, 0))

        body = tk.Frame(self, bg=self.BG)
        body.pack(fill="both", expand=True, padx=34, pady=(0, 26))
        left = tk.Frame(body, bg=self.CARD, padx=24, pady=22)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, bg=self.CARD, width=290, padx=24, pady=22)
        right.pack(side="left", fill="both", padx=(16, 0))
        right.pack_propagate(False)

        tabs = ttk.Notebook(left)
        tabs.pack(fill="both", expand=True)
        send = tk.Frame(tabs, bg=self.CARD, padx=5, pady=22)
        receive = tk.Frame(tabs, bg=self.CARD, padx=5, pady=22)
        tabs.add(send, text="Send")
        tabs.add(receive, text="Receive")
        tabs.bind("<<NotebookTabChanged>>", lambda _e: self.stop_transfer(switching=True))

        self.send_name = tk.StringVar(value="No file selected")
        self.send_size = tk.StringVar(value="Choose a file of any size")
        self._label(send, "Send a file to your phone", 16, "bold").pack(anchor="w")
        self._label(send, "Your phone downloads directly from this PC.", 9, color=self.MUTED).pack(anchor="w", pady=(4, 20))
        self._label(send, self.send_name, 11, "bold", variable=True).pack(anchor="w")
        self._label(send, self.send_size, 9, color=self.MUTED, variable=True).pack(anchor="w", pady=(3, 16))
        self._button(send, "Choose file", self.choose_file, outline=True).pack(fill="x", pady=(0, 10))
        self._button(send, "Create download QR", self.start_send).pack(fill="x")

        self.folder_name = tk.StringVar(value=str(self.receive_folder))
        self._label(receive, "Receive a file from your phone", 16, "bold").pack(anchor="w")
        self._label(receive, "The phone opens a simple upload page.", 9, color=self.MUTED).pack(anchor="w", pady=(4, 20))
        self._label(receive, "Save incoming files to", 9, "bold").pack(anchor="w")
        self._label(receive, self.folder_name, 9, color=self.MUTED, variable=True, wrap=300).pack(anchor="w", pady=(4, 16))
        self._button(receive, "Choose receive folder…", self.choose_folder, outline=True).pack(fill="x", pady=(0, 10))
        self._button(receive, "Create upload QR", self.start_receive).pack(fill="x")

        self._label(right, "SCAN WITH YOUR PHONE", 9, "bold", color=self.MUTED).pack()
        self.qr_label = tk.Label(right, text="QR code will\nappear here", font=("Segoe UI", 13), bg="#f2f4fa", fg=self.MUTED, width=22, height=10)
        self.qr_label.pack(pady=(18, 14))
        self.status = tk.StringVar(value="Choose Send or Receive to begin")
        self._label(right, self.status, 10, "bold", variable=True, wrap=240).pack()
        self.progress_value = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(right, variable=self.progress_value, maximum=100, style="Transfer.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", pady=(12, 0))
        self.progress_text = tk.StringVar(value="")
        self._label(right, self.progress_text, 8, variable=True, color=self.MUTED, wrap=240).pack(pady=(4, 0))
        self.url_label = tk.Label(right, text="", font=("Segoe UI", 8), bg=self.CARD, fg=self.MUTED, cursor="hand2", wraplength=240)
        self.url_label.pack(pady=(8, 0))
        self.url_label.bind("<Button-1>", lambda _e: webbrowser.open(self.service.url) if self.service.url else None)
        self.stop_button = self._button(right, "Stop transfer", self.stop_transfer, outline=True)
        self.stop_button.pack(side="bottom", fill="x")

    def _label(self, parent, text, size, weight="normal", color=None, variable=False, wrap=0):
        args = {"font": ("Segoe UI", size, weight), "bg": self.CARD, "fg": color or self.INK, "justify": "left"}
        if variable:
            args["textvariable"] = text
        else:
            args["text"] = text
        if wrap:
            args["wraplength"] = wrap
        return tk.Label(parent, **args)

    def _button(self, parent, text, command, outline=False):
        return tk.Button(parent, text=text, command=command, font=("Segoe UI", 10, "bold"), padx=13, pady=9,
                         relief="flat", cursor="hand2", bg="#edf0fb" if outline else self.ACCENT,
                         fg=self.ACCENT if outline else "white", activebackground="#dfe5fb" if outline else "#3451cc",
                         activeforeground=self.ACCENT if outline else "white")

    def choose_file(self):
        selected = filedialog.askopenfilename(title="Choose a file to send")
        if not selected:
            return
        path = Path(selected)
        size = path.stat().st_size
        self.file_path = path
        self.send_name.set(path.name)
        self.send_size.set(readable_size(size))

    def choose_folder(self):
        selected = filedialog.askdirectory(title="Choose where incoming files are saved", initialdir=self.receive_folder)
        if selected:
            self.receive_folder = Path(selected)
            self.folder_name.set(str(self.receive_folder))

    def start_send(self):
        if not self.file_path or not self.file_path.is_file():
            messagebox.showinfo(APP_NAME, "Choose a file first.")
            return
        session = TransferSession("send", secrets.token_urlsafe(18), file_path=self.file_path)
        self._show_qr(self.service.start(session), "Ready — scan to download")

    def start_receive(self):
        try:
            self.receive_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot use that folder:\n{exc}")
            return
        session = TransferSession("receive", secrets.token_urlsafe(18), receive_folder=self.receive_folder)
        self._show_qr(self.service.start(session), "Ready — scan to upload")

    def _show_qr(self, url: str, status: str):
        image = qrcode.make(url).resize((230, 230))
        self.qr_photo = ImageTk.PhotoImage(image)
        self.qr_label.configure(image=self.qr_photo, text="", width=230, height=230, bg="white")
        self.status.set(status)
        self.progress_value.set(0)
        self.progress_text.set("Waiting for transfer to start")
        self.url_label.configure(text=url)

    def notify(self, message: str, current: int | None = None, total: int | None = None):
        self.after(0, self._update_progress, message, current, total)

    def _update_progress(self, message: str, current: int | None, total: int | None):
        self.status.set(message)
        completed = message.startswith(("Received:", "Downloaded:"))
        if total and current is not None:
            percent = min(100, current * 100 / total)
            self.progress_value.set(percent)
            self.progress_text.set(f"{percent:.0f}% · {readable_size(current)} / {readable_size(total)}")
        elif completed:
            self.progress_value.set(100)
            self.progress_text.set("Complete")
        else:
            self.progress_value.set(0)
            self.progress_text.set("")

    def stop_transfer(self, switching=False):
        self.service.stop()
        self.qr_photo = None
        self.qr_label.configure(image="", text="QR code will\nappear here", width=22, height=10, bg="#f2f4fa")
        self.status.set("Choose Send or Receive to begin" if switching else "Transfer stopped")
        self.progress_value.set(0)
        self.progress_text.set("")
        self.url_label.configure(text="")

    def close(self):
        self.service.stop()
        self.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("QRLAN.Drop.1")
        except Exception:
            pass
    QRLANApp().mainloop()


if __name__ == "__main__":
    main()
