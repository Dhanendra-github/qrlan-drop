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
from PIL import Image, ImageTk


APP_NAME = "QRLAN Drop"
APP_VERSION = "1.5.0"
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
:root {{ color-scheme:dark; font-family:Inter,"Segoe UI",system-ui,sans-serif; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:24px; overflow-x:hidden; background:#080a12; color:#f7f5ff; }}
body::before {{ content:""; position:fixed; inset:-30%; z-index:-2; background:radial-gradient(circle at 28% 34%,#6d4aff42 0,transparent 28%),radial-gradient(circle at 78% 70%,#24c9e82b 0,transparent 25%); animation:drift 10s ease-in-out infinite alternate; }}
body::after {{ content:""; position:fixed; inset:0; z-index:-1; opacity:.22; background-image:linear-gradient(#ffffff08 1px,transparent 1px),linear-gradient(90deg,#ffffff08 1px,transparent 1px); background-size:32px 32px; mask-image:linear-gradient(to bottom,black,transparent); }}
main {{ position:relative; width:min(90vw,440px); overflow:hidden; padding:34px; border:1px solid #ffffff14; border-radius:28px; background:linear-gradient(145deg,#171a29ee,#10121dee); box-shadow:0 28px 90px #0009,inset 0 1px #ffffff0d; text-align:center; backdrop-filter:blur(24px); }}
main::before {{ content:""; position:absolute; inset:0 0 auto; height:2px; background:linear-gradient(90deg,transparent,#8468ff,#39d9ed,transparent); }}
h1 {{ font-size:1.5rem; line-height:1.25; overflow-wrap:anywhere; margin:16px 0 8px; }} p {{ color:#a5abc2; line-height:1.55; }}
.button,button {{ display:block; width:100%; border:0; border-radius:14px; padding:15px; background:linear-gradient(110deg,#7558ff,#9978ff); color:white; font-weight:750; font-size:1rem; text-decoration:none; cursor:pointer; box-shadow:0 10px 30px #694cff36; transition:transform .2s,filter .2s; }}
.button:hover,button:hover {{ transform:translateY(-2px); filter:brightness(1.12); }} button:disabled {{ opacity:.55; cursor:default; transform:none; }}
.badge {{ display:inline-flex; padding:7px 12px; border:1px solid #8a72ff44; border-radius:99px; background:#7960ff18; color:#b9aaff; font-size:.78rem; font-weight:750; letter-spacing:.04em; }}
.picker {{ border:1px dashed #71669a; border-radius:17px; padding:25px 14px; margin:24px 0 15px; background:#ffffff05; transition:border-color .2s,background .2s; }} .picker:hover {{ border-color:#947cff; background:#8a6eff0c; }}
input {{ max-width:100%; color:#cdd1e1; }} input::file-selector-button {{ margin-right:10px; border:0; border-radius:9px; padding:9px 12px; background:#292d42; color:#f5f3ff; cursor:pointer; }}
progress {{ width:100%; height:10px; margin-top:18px; border:0; border-radius:99px; overflow:hidden; accent-color:#8064ff; }} progress::-webkit-progress-bar {{ background:#252839; }} progress::-webkit-progress-value {{ background:linear-gradient(90deg,#7659ff,#3dd9eb); }}
.note {{ font-size:.82rem; }} #status {{ min-height:1.4em; font-weight:650; color:#baaaff; }}
@keyframes drift {{ to {{ transform:translate3d(5%,-3%,0) rotate(5deg); }} }}
@media(prefers-reduced-motion:reduce) {{ * {{ animation:none!important; transition:none!important; }} }}
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


class LegacyQRLANApp(tk.Tk):
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
        self.last_received_path: Path | None = None
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


def _mix_color(first: str, second: str, amount: float) -> str:
    """Blend two #RRGGBB colors for subtle UI motion."""
    a = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
    b = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
    values = tuple(round(x + (y - x) * amount) for x, y in zip(a, b))
    return "#" + "".join(f"{value:02x}" for value in values)


def _rounded_rectangle(canvas: tk.Canvas, x1, y1, x2, y2, radius, **kwargs):
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class ModernButton(tk.Canvas):
    def __init__(self, parent, text, command, *, accent="#7c5cff", outline=False, height=46):
        self.parent_bg = parent.cget("bg")
        super().__init__(parent, width=120, height=height, bg=self.parent_bg, bd=0, highlightthickness=0, cursor="hand2")
        self.text = text
        self.command = command
        self.accent = accent
        self.outline = outline
        self.selected = False
        self.hover = 0.0
        self.target_hover = 0.0
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda _event: self._animate_to(1.0))
        self.bind("<Leave>", lambda _event: self._animate_to(0.0))
        self.bind("<ButtonRelease-1>", self._click)

    def _click(self, _event):
        if self.command:
            self.command()

    def _animate_to(self, value: float):
        self.target_hover = value
        self._animate_step()

    def _animate_step(self):
        difference = self.target_hover - self.hover
        self.hover = self.target_hover if abs(difference) < 0.04 else self.hover + difference * 0.28
        self._draw()
        if self.hover != self.target_hover:
            self.after(16, self._animate_step)

    def set_selected(self, selected: bool):
        self.selected = selected
        self._draw()

    def _draw(self, _event=None):
        width = max(self.winfo_width(), 20)
        height = max(self.winfo_height(), 20)
        self.delete("all")
        if self.outline and not self.selected:
            base, hover, ink = "#1a1e2e", "#292e45", "#d8d9e8"
        else:
            base, hover, ink = self.accent, "#967fff", "#ffffff"
        fill = _mix_color(base, hover, self.hover)
        _rounded_rectangle(self, 1, 1, width - 1, height - 1, 13, fill=fill, outline="")
        if self.outline and not self.selected:
            _rounded_rectangle(self, 1, 1, width - 1, height - 1, 13, fill="", outline="#30364f", width=1)
        self.create_text(width / 2, height / 2, text=self.text, fill=ink, font=("Segoe UI", 10, "bold"))


class AnimatedProgress(tk.Canvas):
    def __init__(self, parent, variable: tk.DoubleVar):
        super().__init__(parent, height=9, bg=parent.cget("bg"), bd=0, highlightthickness=0)
        self.variable = variable
        self.phase = 0
        self.bind("<Configure>", self._draw)
        self.variable.trace_add("write", lambda *_args: self._draw())
        self.after(40, self._tick)

    def _tick(self):
        self.phase = (self.phase + 4) % 80
        if 0 < self.variable.get() < 100:
            self._draw()
        try:
            self.after(40, self._tick)
        except tk.TclError:
            pass

    def _draw(self, _event=None):
        if not self.winfo_exists():
            return
        width = max(self.winfo_width(), 4)
        self.delete("all")
        _rounded_rectangle(self, 0, 1, width, 8, 4, fill="#24283a", outline="")
        fill_width = width * max(0, min(100, self.variable.get())) / 100
        if fill_width > 3:
            _rounded_rectangle(self, 0, 1, fill_width, 8, 4, fill="#7c5cff", outline="")
            shine_x = min(fill_width - 2, self.phase / 80 * fill_width)
            self.create_line(max(2, shine_x - 18), 4, shine_x, 4, fill="#52d9eb", width=3)


class StatusOrb(tk.Canvas):
    def __init__(self, parent):
        super().__init__(parent, width=16, height=16, bg=parent.cget("bg"), bd=0, highlightthickness=0)
        self.active = False
        self.phase = 0
        self.after(45, self._tick)

    def set_active(self, active: bool):
        self.active = active

    def _tick(self):
        self.phase = (self.phase + 1) % 40
        self.delete("all")
        if self.active:
            wave = abs(20 - self.phase) / 20
            radius = 5 + (1 - wave) * 2
            self.create_oval(8 - radius, 8 - radius, 8 + radius, 8 + radius, fill="#7c5cff", outline="")
            self.create_oval(5, 5, 11, 11, fill="#51d7e9", outline="")
        else:
            self.create_oval(5, 5, 11, 11, fill="#626b85", outline="")
        try:
            self.after(45, self._tick)
        except tk.TclError:
            pass


class QRLANApp(LegacyQRLANApp):
    BG = "#090b13"
    CARD = "#121622"
    CARD_ALT = "#171b2a"
    INK = "#f5f3ff"
    MUTED = "#929ab2"
    ACCENT = "#7c5cff"
    CYAN = "#51d7e9"
    BORDER = "#252a3d"

    def __init__(self):
        tk.Tk.__init__(self)
        self.title(APP_NAME)
        self.geometry("920x720")
        self.minsize(820, 660)
        self.configure(bg=self.BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.service = TransferService(self.notify)
        self.file_path: Path | None = None
        self.last_received_path: Path | None = None
        self.receive_folder = Path.home() / "Downloads" / "QRLAN Drop"
        self.qr_photo = None
        self.active_mode = "send"
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self._build()
        self.after(30, self._fade_in, 0.0)

    def _fade_in(self, opacity=0.0):
        try:
            opacity = min(1.0, opacity + 0.09)
            self.attributes("-alpha", opacity)
            if opacity < 1.0:
                self.after(18, self._fade_in, opacity)
        except tk.TclError:
            pass

    def _card(self, parent, **kwargs):
        border = tk.Frame(parent, bg=self.BORDER, bd=0)
        inner = tk.Frame(border, bg=self.CARD, bd=0, **kwargs)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return border, inner

    def _build(self):
        shell = tk.Frame(self, bg=self.BG)
        shell.pack(fill="both", expand=True, padx=38, pady=28)

        header = tk.Frame(shell, bg=self.BG, height=64)
        header.pack(fill="x", pady=(0, 22))
        header.pack_propagate(False)
        mark = tk.Canvas(header, width=48, height=48, bg=self.BG, bd=0, highlightthickness=0)
        mark.pack(side="left", pady=5)
        _rounded_rectangle(mark, 2, 2, 46, 46, 14, fill=self.ACCENT, outline="")
        mark.create_arc(12, 12, 36, 36, start=35, extent=270, style="arc", outline="white", width=3)
        mark.create_oval(28, 12, 35, 19, fill=self.CYAN, outline="")

        brand = tk.Frame(header, bg=self.BG)
        brand.pack(side="left", padx=(13, 0), pady=4)
        tk.Label(brand, text="QRLAN DROP", font=("Segoe UI", 18, "bold"), bg=self.BG, fg=self.INK).pack(anchor="w")
        tk.Label(brand, text=f"Direct. Private. Effortless.  ·  v{APP_VERSION}", font=("Segoe UI", 9), bg=self.BG, fg=self.MUTED).pack(anchor="w", pady=(2, 0))
        privacy = tk.Frame(header, bg="#151a27", padx=13, pady=8, highlightthickness=1, highlightbackground=self.BORDER)
        privacy.pack(side="right", pady=11)
        tk.Label(privacy, text="●", font=("Segoe UI", 8), bg="#151a27", fg=self.CYAN).pack(side="left")
        tk.Label(privacy, text=" LOCAL NETWORK ONLY", font=("Segoe UI", 8, "bold"), bg="#151a27", fg="#c8ccda").pack(side="left")

        body = tk.Frame(shell, bg=self.BG)
        body.pack(fill="both", expand=True)
        left_border, left = self._card(body, padx=28, pady=25)
        left_border.pack(side="left", fill="both", expand=True)
        right_border, right = self._card(body, width=320, padx=26, pady=24)
        right_border.configure(width=322)
        right_border.pack(side="left", fill="both", padx=(18, 0))
        right_border.pack_propagate(False)
        right.pack_propagate(False)

        switch = tk.Frame(left, bg="#0d101a", padx=5, pady=5)
        switch.pack(fill="x", pady=(0, 26))
        self.send_tab = ModernButton(switch, "SEND", lambda: self._switch_mode("send"), outline=True, height=40)
        self.send_tab.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.receive_tab = ModernButton(switch, "RECEIVE", lambda: self._switch_mode("receive"), outline=True, height=40)
        self.receive_tab.pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.content = tk.Frame(left, bg=self.CARD)
        self.content.pack(fill="both", expand=True)
        self.send_panel = tk.Frame(self.content, bg=self.CARD)
        self.receive_panel = tk.Frame(self.content, bg=self.CARD)
        self._build_send_panel()
        self._build_receive_panel()
        self._switch_mode("send", stop=False)
        self._build_scanner(right)

        footer = tk.Frame(shell, bg=self.BG)
        footer.pack(fill="x", pady=(17, 0))
        tk.Label(footer, text="FILES NEVER TOUCH THE CLOUD", font=("Segoe UI", 8, "bold"), bg=self.BG, fg="#687089").pack(side="left")
        tk.Label(footer, text="∞  NO ARTIFICIAL FILE LIMIT", font=("Segoe UI", 8, "bold"), bg=self.BG, fg="#687089").pack(side="right")

    def _build_send_panel(self):
        panel = self.send_panel
        self._eyebrow(panel, "01  FROM THIS COMPUTER")
        self._label(panel, "Send something beautiful.", 22, "bold").pack(anchor="w", pady=(8, 5))
        self._label(panel, "Choose any file. Your phone downloads it directly over Wi-Fi.", 10, color=self.MUTED, wrap=430).pack(anchor="w")
        zone = tk.Frame(panel, bg=self.CARD_ALT, padx=18, pady=17, highlightthickness=1, highlightbackground="#30364c")
        zone.pack(fill="x", pady=(24, 19))
        icon = tk.Canvas(zone, width=44, height=44, bg=self.CARD_ALT, bd=0, highlightthickness=0)
        icon.pack(side="left", padx=(0, 14))
        icon.create_rectangle(10, 6, 34, 38, outline=self.CYAN, width=2)
        icon.create_line(25, 6, 34, 15, fill=self.CYAN, width=2)
        icon.create_line(25, 6, 25, 15, 34, 15, fill=self.CYAN, width=2)
        details = tk.Frame(zone, bg=self.CARD_ALT)
        details.pack(side="left", fill="x", expand=True)
        self.send_name = tk.StringVar(value="No file selected")
        self.send_size = tk.StringVar(value="Any file type · Any size")
        self._label(details, self.send_name, 10, "bold", variable=True, bg=self.CARD_ALT, wrap=320).pack(anchor="w")
        self._label(details, self.send_size, 9, variable=True, color=self.MUTED, bg=self.CARD_ALT).pack(anchor="w", pady=(4, 0))
        self._button(panel, "Choose a file", self.choose_file, outline=True).pack(fill="x", pady=(0, 10))
        self._button(panel, "Create download QR", self.start_send).pack(fill="x")

    def _build_receive_panel(self):
        panel = self.receive_panel
        self._eyebrow(panel, "02  TO THIS COMPUTER")
        self._label(panel, "Bring files home.", 22, "bold").pack(anchor="w", pady=(8, 5))
        self._label(panel, "Scan once, choose a file on your phone, and watch it arrive.", 10, color=self.MUTED, wrap=430).pack(anchor="w")
        zone = tk.Frame(panel, bg=self.CARD_ALT, padx=18, pady=17, highlightthickness=1, highlightbackground="#30364c")
        zone.pack(fill="x", pady=(24, 19))
        icon = tk.Canvas(zone, width=44, height=44, bg=self.CARD_ALT, bd=0, highlightthickness=0)
        icon.pack(side="left", padx=(0, 14))
        icon.create_rectangle(6, 12, 38, 36, outline=self.CYAN, width=2)
        icon.create_line(8, 12, 20, 12, 24, 17, 38, 17, fill=self.CYAN, width=2)
        details = tk.Frame(zone, bg=self.CARD_ALT)
        details.pack(side="left", fill="x", expand=True)
        self.folder_name = tk.StringVar(value=str(self.receive_folder))
        self._label(details, "SAVE IN", 8, "bold", color=self.MUTED, bg=self.CARD_ALT).pack(anchor="w")
        self._label(details, self.folder_name, 9, "bold", variable=True, bg=self.CARD_ALT, wrap=320).pack(anchor="w", pady=(4, 0))
        self._button(panel, "Choose receive folder", self.choose_folder, outline=True).pack(fill="x", pady=(0, 10))
        self._button(panel, "Create upload QR", self.start_receive).pack(fill="x")

    def _build_scanner(self, right):
        top = tk.Frame(right, bg=self.CARD)
        top.pack(fill="x")
        self._label(top, "SCAN PORTAL", 9, "bold", color=self.MUTED).pack(side="left")
        tk.Label(top, text="READY", font=("Segoe UI", 8, "bold"), bg="#18272a", fg=self.CYAN, padx=8, pady=4).pack(side="right")
        qr_border = tk.Frame(right, bg="#2d3348", width=250, height=250)
        qr_border.pack(pady=(18, 17))
        qr_border.pack_propagate(False)
        qr_inner = tk.Frame(qr_border, bg="#0d1019")
        qr_inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.qr_label = tk.Label(qr_inner, text="⌁\n\nYOUR QR CODE\nWILL APPEAR HERE", font=("Segoe UI", 9, "bold"), bg="#0d1019", fg="#646d87", justify="center")
        self.qr_label.pack(fill="both", expand=True, padx=13, pady=13)
        status_row = tk.Frame(right, bg=self.CARD)
        status_row.pack(fill="x")
        self.status_orb = StatusOrb(status_row)
        self.status_orb.pack(side="left", padx=(0, 7))
        self.status = tk.StringVar(value="Waiting for your move")
        self._label(status_row, self.status, 9, "bold", variable=True, wrap=235).pack(side="left", fill="x", expand=True)
        self.progress_value = tk.DoubleVar(value=0)
        self.progress_bar = AnimatedProgress(right, self.progress_value)
        self.progress_bar.pack(fill="x", pady=(13, 0))
        self.progress_text = tk.StringVar(value="Select Send or Receive to begin")
        self._label(right, self.progress_text, 8, variable=True, color=self.MUTED, wrap=260).pack(pady=(6, 0))
        self.url_label = tk.Label(right, text="", font=("Segoe UI", 8), bg=self.CARD, fg=self.CYAN, cursor="hand2", wraplength=260)
        self.url_label.pack(pady=(8, 0))
        self.url_label.bind("<Button-1>", lambda _event: webbrowser.open(self.service.url) if self.service.url else None)
        self.stop_button = self._button(right, "Stop transfer", self.stop_transfer, outline=True)
        self.stop_button.pack(side="bottom", fill="x")
        open_actions = tk.Frame(right, bg=self.CARD)
        open_actions.pack(side="bottom", fill="x", pady=(0, 9))
        self.open_folder_button = ModernButton(open_actions, "OPEN FOLDER", self.open_folder, accent=self.ACCENT, outline=True, height=40)
        self.open_folder_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.open_file_button = ModernButton(open_actions, "OPEN FILE", self.open_file, accent=self.ACCENT, outline=True, height=40)
        self.open_file_button.pack(side="left", fill="x", expand=True, padx=(4, 0))

    def _switch_mode(self, mode, stop=True):
        if stop and mode != self.active_mode:
            self.stop_transfer(switching=True)
        self.active_mode = mode
        self.send_tab.set_selected(mode == "send")
        self.receive_tab.set_selected(mode == "receive")
        self.send_panel.pack_forget()
        self.receive_panel.pack_forget()
        (self.send_panel if mode == "send" else self.receive_panel).pack(fill="both", expand=True)

    def _eyebrow(self, parent, text):
        tk.Label(parent, text=text, font=("Segoe UI", 8, "bold"), bg=self.CARD, fg=self.CYAN).pack(anchor="w")

    def _label(self, parent, text, size, weight="normal", color=None, variable=False, wrap=0, bg=None):
        args = {"font": ("Segoe UI", size, weight), "bg": bg or parent.cget("bg"), "fg": color or self.INK, "justify": "left"}
        args["textvariable" if variable else "text"] = text
        if wrap:
            args["wraplength"] = wrap
        return tk.Label(parent, **args)

    def _button(self, parent, text, command, outline=False):
        return ModernButton(parent, text, command, accent=self.ACCENT, outline=outline)

    def _show_qr(self, url: str, status: str):
        image = qrcode.make(url).convert("RGB")
        image = image.resize((216, 216), Image.Resampling.NEAREST)
        self.qr_photo = ImageTk.PhotoImage(image)
        self.qr_label.configure(image=self.qr_photo, text="", bg="white")
        self.status.set(status)
        self.status_orb.set_active(True)
        self.progress_value.set(0)
        self.progress_text.set("Waiting for transfer to start")
        self.url_label.configure(text=url)

    def _update_progress(self, message: str, current: int | None, total: int | None):
        self.status.set(message)
        completed = message.startswith(("Received:", "Downloaded:"))
        if message.startswith("Received: "):
            candidate = self.receive_folder / message.removeprefix("Received: ")
            if candidate.is_file():
                self.last_received_path = candidate
        self.status_orb.set_active(not completed and bool(self.service.url))
        if total and current is not None:
            percent = min(100, current * 100 / total)
            self.progress_value.set(percent)
            self.progress_text.set(f"{percent:.0f}% · {readable_size(current)} / {readable_size(total)}")
        elif completed:
            self.progress_value.set(100)
            self.progress_text.set("Transfer complete")
        else:
            self.progress_value.set(0)
            self.progress_text.set("")

    def _current_file(self) -> Path | None:
        if self.active_mode == "send" and self.file_path and self.file_path.is_file():
            return self.file_path
        if self.last_received_path and self.last_received_path.is_file():
            return self.last_received_path
        return None

    def open_folder(self):
        current = self._current_file()
        folder = current.parent if current else self.receive_folder
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(folder))
            else:
                webbrowser.open(folder.as_uri())
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot open that folder:\n{exc}")

    def open_file(self):
        current = self._current_file()
        if current is None:
            messagebox.showinfo(APP_NAME, "Choose a file or receive one first.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(current))
            else:
                webbrowser.open(current.as_uri())
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot open that file:\n{exc}")

    def stop_transfer(self, switching=False):
        self.service.stop()
        self.qr_photo = None
        self.qr_label.configure(image="", text="⌁\n\nYOUR QR CODE\nWILL APPEAR HERE", bg="#0d1019")
        self.status.set("Waiting for your move" if switching else "Transfer stopped")
        self.status_orb.set_active(False)
        self.progress_value.set(0)
        self.progress_text.set("Select Send or Receive to begin" if switching else "")
        self.url_label.configure(text="")


def _hide_windows_console():
    """Keep source and packaged launches free of a companion console window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        console = ctypes.windll.kernel32.GetConsoleWindow()
        if console:
            ctypes.windll.user32.ShowWindow(console, 0)
    except Exception:
        pass


def _enable_dark_title_bar(window: tk.Tk):
    """Ask Windows 11 to render Tk's native frame in dark mode."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        child = window.winfo_id()
        hwnd = ctypes.windll.user32.GetParent(child) or child
        enabled = ctypes.c_int(1)
        # 20 is the current immersive-dark-mode attribute; 19 supports older builds.
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
            if result == 0:
                break
        flags = 0x0001 | 0x0002 | 0x0004 | 0x0020
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags)
    except Exception:
        pass


def main():
    _hide_windows_console()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("QRLAN.Drop.1")
        except Exception:
            pass
    app = QRLANApp()
    app.after(30, _enable_dark_title_bar, app)
    app.after(350, _enable_dark_title_bar, app)
    app.mainloop()


if __name__ == "__main__":
    main()
