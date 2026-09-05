"""Fluent Commander desktop: local file selection, pairing, and private history."""
from __future__ import annotations

import datetime
import ipaddress
import json
import os
import queue
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk
import qrcode
from tkinterdnd2 import DND_FILES, TkinterDnD
from branding import asset_path, BLUE as BRAND_BLUE

from app import APP_NAME, APP_VERSION, TransferService, TransferSession, readable_size, _enable_dark_title_bar


def valid_transfer_url(value):
    value = value.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname
        if host == 'localhost' or host.endswith('.local'):
            return True
        address = ipaddress.ip_address(host)
        return address.is_private and not address.is_unspecified and not address.is_multicast
    except ValueError:
        return False


def decode_qr_image(path):
    import cv2
    import numpy as np
    with Image.open(path) as source:
        source.thumbnail((2048, 2048))
        pixels = cv2.cvtColor(np.array(source.convert('RGB')), cv2.COLOR_RGB2BGR)
    value, _points, _straight = cv2.QRCodeDetector().detectAndDecode(pixels)
    if not value:
        raise ValueError('No readable QR code was found. Choose a clearer image.')
    if not valid_transfer_url(value):
        raise ValueError('This QR code does not contain a local HTTP or HTTPS link.')
    return value


class LocalStore:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get('QRLAN_DATA_DIR') or Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'QRLAN Drop')
        self.warning = ''

    def load(self, name, default):
        path = self.root / (name + '.json')
        if not path.exists():
            return default
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, type(default)):
                raise ValueError('Unexpected saved data')
            return value
        except (OSError, ValueError):
            self.warning = 'Saved data could not be read. A backup will be kept.'
            backup = path.with_name(path.stem + '.unreadable-' + secrets.token_hex(4) + '.json')
            try:
                path.rename(backup)
            except OSError:
                self.warning = 'Saved data cannot be read or backed up. Check folder permissions.'
            return default

    def save(self, name, value):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (name + '.json')
        # Do not overwrite an unreadable document when the backup failed.
        if path.exists():
            json.loads(path.read_text(encoding='utf-8'))
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)


class FluentApp(TkinterDnD.Tk):
    BG = '#0c1118'
    CARD = '#111923'
    ALT = '#18222f'
    LINE = '#2b3849'
    INK = '#eef3fb'
    MUTED = '#a1afc2'
    BLUE = '#1766d8'

    def __init__(self):
        super().__init__()
        self.title(APP_NAME + ' — Fluent Commander')
        self.app_icon = ImageTk.PhotoImage(file=str(asset_path('app-icon.png')))
        self.iconphoto(True, self.app_icon)
        if os.name == 'nt':self.iconbitmap(str(asset_path('app.ico')))
        self.geometry('1180x780')
        self.minsize(1040, 720)
        self.configure(bg=self.BG)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.store = LocalStore()
        self.history = [r for r in self.store.load('history', []) if isinstance(r, dict) and all(k in r for k in ('id','name','direction','size','status','date'))]
        settings = self.store.load('settings', {})
        self.receive_folder = Path(settings.get('receive_folder') or Path.home() / 'Downloads' / 'QRLAN Drop')
        self.paths = {}
        self.checked = set()
        self.mode = 'send'
        self.view = 'send'
        self.closed = False
        self.link_view = 'qr'
        self.folder_loading = False
        self.jobs = queue.SimpleQueue()
        self.service = TransferService(self.on_progress, self.record_transfer)
        self.status = tk.StringVar(value='Ready to share')
        self.link = tk.StringVar()
        self.folder_label = tk.StringVar(value=str(self.receive_folder))
        self.selection_label = tk.StringVar(value='No files selected')
        self.connection_label = tk.StringVar(value='Create a link to connect')
        self.history_filter = tk.StringVar(value='All')
        self._style()
        self._build()
        self.navigate('send')
        self.after(50, self._poll)
        self.after(80, _enable_dark_title_bar, self)
        self.bind('<Control-o>', lambda e: self.choose_files())
        self.bind('<Control-h>', lambda e: self.navigate('history'))
        self.bind('<Control-Return>', lambda e: self.create_link())
        if self.store.warning:
            self.status.set(self.store.warning)

    def _style(self):
        self.option_add('*Font', ('Segoe UI', 11))
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('Files.Treeview', background=self.CARD, fieldbackground=self.CARD, foreground=self.INK,
                        rowheight=48, borderwidth=0, font=('Segoe UI', 11))
        style.configure('Files.Treeview.Heading', background=self.ALT, foreground=self.MUTED,
                        relief='flat', font=('Segoe UI', 10), padding=(10, 14))
        style.map('Files.Treeview', background=[('selected','#153561')], foreground=[('selected',self.INK)])
        style.map('Files.Treeview.Heading', background=[('active',self.LINE)])
        style.configure('Vertical.TScrollbar', background=self.ALT, troughcolor=self.CARD, borderwidth=0, arrowsize=13)

    def label(self, parent, text='', size=11, color=None, bold=False, variable=None, **kw):
        options = dict(bg=parent.cget('bg'), fg=color or self.INK, font=('Segoe UI', size, 'bold' if bold else 'normal'), anchor='w', justify='left')
        options.update(kw)
        if variable is not None: options['textvariable'] = variable
        else: options['text'] = text
        return tk.Label(parent, **options)

    def button(self, parent, text, command, primary=False, **kw):
        pady = kw.pop('pady', 8)
        button = tk.Button(parent, text=text, command=command, bg=self.BLUE if primary else self.CARD,
                           fg=self.INK, activebackground='#237cec' if primary else self.ALT,
                           activeforeground='white', relief='flat', bd=0, highlightthickness=1,
                           highlightbackground='#2679e7' if primary else self.LINE, highlightcolor='#7baeff',
                           padx=16, pady=pady, cursor='hand2', takefocus=True, **kw)
        button.bind('<Return>', lambda e: button.invoke())
        return button

    def panel(self, parent, **kw):
        return tk.Frame(parent, bg=self.CARD, highlightbackground=self.LINE, highlightthickness=1, **kw)

    def _build(self):
        header = tk.Frame(self, bg=self.BG, height=64)
        header.pack(fill='x'); header.pack_propagate(False)
        with Image.open(asset_path('brand-mark.png')) as source:
            self.brand_photo = ImageTk.PhotoImage(source.resize((34,34),Image.Resampling.LANCZOS))
        tk.Label(header,image=self.brand_photo,bg=self.BG).pack(side='left',padx=(22,10))
        self.label(header, 'QRLAN', 16, bold=True).pack(side='left')
        self.label(header, 'DROP', 16, color=BRAND_BLUE).pack(side='left',padx=(4,0))
        self.label(header, 'LOCAL FILE SHARING', 9, self.MUTED).pack(side='right', padx=26)
        footer = tk.Frame(self, bg=self.BG, height=42, highlightbackground=self.LINE, highlightthickness=1)
        footer.pack(side='bottom', fill='x'); footer.pack_propagate(False)
        self.label(footer, variable=self.status, color=self.MUTED, size=10).pack(side='left', padx=20, pady=9)
        self.label(footer, APP_VERSION, size=9, color=self.MUTED).pack(side='right', padx=18)
        body = tk.Frame(self, bg=self.BG); body.pack(fill='both', expand=True)
        sidebar = tk.Frame(body, bg=self.BG, width=168)
        sidebar.pack(side='left', fill='y', padx=(12,0), pady=8); sidebar.pack_propagate(False)
        self.nav = {}
        for key, text in [('send','↗   Send'),('receive','↙   Receive'),('history','◷   History')]:
            self.nav[key] = self.button(sidebar, text, lambda key=key:self.navigate(key), anchor='w')
            self.nav[key].pack(fill='x', padx=3, pady=(5,12))
        self.button(sidebar,'ⓘ   About',self.about,anchor='w').pack(side='bottom',fill='x',padx=3,pady=(5,18))
        self.button(sidebar,'⚙   Settings',self.settings,anchor='w').pack(side='bottom',fill='x',padx=3,pady=5)
        self.content = tk.Frame(body,bg=self.BG)
        self.content.pack(side='left',fill='both',expand=True,padx=(20,20),pady=(8,14))
        self.transfer_screen = tk.Frame(self.content,bg=self.BG)
        self.transfer_screen.columnconfigure(0,weight=3,uniform='panels')
        self.transfer_screen.columnconfigure(1,weight=2,uniform='panels')
        self.transfer_screen.rowconfigure(0,weight=1)
        self.left = self.panel(self.transfer_screen,padx=20,pady=22)
        self.left.grid(row=0,column=0,sticky='nsew',padx=(0,16))
        self.send_panel = tk.Frame(self.left,bg=self.CARD)
        self.receive_panel = tk.Frame(self.left,bg=self.CARD)
        self._build_send(); self._build_receive()
        self.connect = self.panel(self.transfer_screen,padx=22,pady=22)
        self.connect.grid(row=0,column=1,sticky='nsew')
        self._build_connection()
        self.history_screen = tk.Frame(self.content,bg=self.BG)
        self._build_history()

    def _build_send(self):
        p=self.send_panel
        self.label(p,'Files to send',22,bold=True).pack(anchor='w')
        self.label(p,'Choose files for a new transfer.',color=self.MUTED).pack(anchor='w',pady=(7,22))
        actions=tk.Frame(p,bg=self.CARD);actions.pack(fill='x',pady=(0,18))
        self.button(actions,'＋  Choose files',self.choose_files).pack(side='left',fill='x',expand=True,padx=(0,8))
        self.button(actions,'▱  Choose folder',self.choose_send_folder).pack(side='left',fill='x',expand=True)
        table=tk.Frame(p,bg=self.CARD);table.pack(fill='both',expand=True)
        self.file_table=ttk.Treeview(table,height=3,columns=('check','name','size','type'),show='headings',style='Files.Treeview',selectmode='extended')
        for key,title,width in [('check','✓',34),('name','Name',220),('size','Size',83),('type','Type',90)]:
            self.file_table.heading(key,text=title)
            self.file_table.column(key,width=width,minwidth=30,stretch=key=='name',anchor='w')
        scrollbar=ttk.Scrollbar(table,orient='vertical',command=self.file_table.yview)
        self.file_table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y');self.file_table.pack(side='left',fill='both',expand=True)
        self.file_table.bind('<Button-1>',self.toggle_checked)
        self.file_table.bind('<space>',self.toggle_selected)
        self.file_table.bind('<Delete>',lambda e:self.remove_files())
        bottom=tk.Frame(p,bg=self.CARD);bottom.pack(fill='x',pady=(8,12))
        self.label(bottom,variable=self.selection_label,size=10,color=self.MUTED).pack(side='left')
        self.button(bottom,'Remove',self.remove_files,pady=4).pack(side='right')
        self.drop=self.button(p,'⇧   Drop files or folders here',self.choose_files)
        self.drop.pack(fill='x',pady=(0,14))
        for widget in (self.drop,self.file_table):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind('<<Drop>>',self.drop_files)
        self.create_button=self.button(p,'⌁   Create transfer link',self.create_link,primary=True)
        self.create_button.pack(fill='x')

    def _build_receive(self):
        p=self.receive_panel
        self.label(p,'Receive files',22,bold=True).pack(anchor='w')
        self.label(p,'Let another device send files to this PC.',color=self.MUTED,wraplength=390).pack(anchor='w',pady=(8,30))
        self.label(p,'SAVE RECEIVED FILES TO',10,self.MUTED,True).pack(anchor='w')
        self.label(p,variable=self.folder_label,wraplength=400).pack(anchor='w',pady=(12,20))
        self.button(p,'▱   Choose receive folder',self.choose_receive_folder).pack(fill='x')
        self.storage=self.label(p,'',10,self.MUTED);self.storage.pack(anchor='w',pady=18)
        self.label(p,'1   Create an upload link.\n\n2   Scan the QR or open the link on your phone.\n\n3   Choose a file and upload it.',color=self.MUTED,wraplength=400).pack(anchor='w',pady=24)
        self.button(p,'⌁   Create upload link',self.create_link,primary=True).pack(side='bottom',fill='x')

    def _build_connection(self):
        p=self.connect
        self.label(p,'Connect a device',21,bold=True).pack(anchor='w')
        self.label(p,variable=self.connection_label,color=self.MUTED,size=10,wraplength=310).pack(anchor='w',pady=(8,18))
        self.button(p,'▣   Scan QR',self.scan_qr).pack(fill='x',pady=(0,16))
        tabs=tk.Frame(p,bg=self.CARD);tabs.pack(fill='x')
        self.qr_tab=self.button(tabs,'Show QR',lambda:self.show_link_mode('qr'),primary=True)
        self.qr_tab.pack(side='left',fill='x',expand=True,padx=(0,4))
        self.link_tab=self.button(tabs,'Show Link',lambda:self.show_link_mode('link'))
        self.link_tab.pack(side='left',fill='x',expand=True)
        connection_bottom=tk.Frame(p,bg=self.CARD)
        connection_bottom.pack(side='bottom',fill='x')
        self.pair_area=tk.Frame(p,bg=self.CARD,height=80)
        self.pair_area.pack(fill='both',expand=True,pady=(16,8));self.pair_area.pack_propagate(False)
        self.qr_area=tk.Frame(self.pair_area,bg=self.CARD)
        self.qr_area.pack(fill='both',expand=True)
        self.qr_label=self.label(self.qr_area,'Your QR code will appear here',color=self.MUTED,anchor='center',wraplength=230)
        self.qr_label.pack(expand=True)
        self.pair_area.bind('<Configure>',self.resize_qr)
        self.link_area=tk.Frame(self.pair_area,bg=self.CARD)
        self.label(self.link_area,'TRANSFER LINK',10,self.MUTED,True).pack(anchor='w',pady=(8,10))
        self.link_text=tk.Text(self.link_area,height=3,wrap='char',bg=self.ALT,fg=self.INK,insertbackground=self.INK,relief='flat',padx=14,pady=10,font=('Consolas',11))
        self.link_text.pack(fill='x');self.link_text.configure(state='disabled')
        self.button(self.link_area,'Open in browser',self.open_link).pack(fill='x',pady=12)
        self.label(connection_bottom,'Scan with your phone camera',10,bold=True,anchor='center').pack(fill='x',pady=(2,10))
        actions=tk.Frame(connection_bottom,bg=self.CARD);actions.pack(fill='x')
        self.copy_button=self.button(actions,'⧉  Copy Link',self.copy_link)
        self.copy_button.pack(side='left',fill='x',expand=True,padx=(0,6))
        self.share_button=self.button(actions,'↗  Share Link',self.share_link)
        self.share_button.pack(side='left',fill='x',expand=True)
        self.stop_button=self.button(connection_bottom,'■  Stop transfer',self.stop_transfer,pady=4)
        self.stop_button.pack(fill='x',pady=(8,0))
        self.label(connection_bottom,'Links work on the same local network.',9,self.MUTED,anchor='center',wraplength=310).pack(fill='x',pady=(8,0))
        self.update_link_controls()

    def _build_history(self):
        p=self.history_screen
        header=tk.Frame(p,bg=self.BG);header.pack(fill='x',pady=(12,8))
        self.label(header,'Transfer history',25,bold=True).pack(side='left')
        self.button(header,'←  Back to Send',lambda:self.navigate('send')).pack(side='right')
        self.label(p,'Your sent and received files. Stored on this PC.',color=self.MUTED).pack(anchor='w',pady=(0,24))
        filters=tk.Frame(p,bg=self.BG);filters.pack(fill='x',pady=(0,16))
        self.filter_buttons={}
        for name in ('All','Sent','Received'):
            b=self.button(filters,name,lambda name=name:self.filter_history(name));b.pack(side='left',padx=(0,8));self.filter_buttons[name]=b
        panel=self.panel(p);panel.pack(fill='both',expand=True)
        self.history_table=ttk.Treeview(panel,height=3,columns=('name','direction','size','status','date'),show='headings',style='Files.Treeview')
        for name,title,width in [('name','File name',260),('direction','Direction',110),('size','Size',100),('status','Status',125),('date','Date',180)]:
            self.history_table.heading(name,text=title);self.history_table.column(name,width=width,minwidth=70,stretch=name in ('name','date'))
        scroll=ttk.Scrollbar(panel,orient='vertical',command=self.history_table.yview)
        self.history_table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.history_table.pack(side='left',fill='both',expand=True)
        self.history_table.tag_configure('Completed',foreground='#8bdbb0')
        self.history_table.tag_configure('Failed',foreground='#ffa6ad')
        self.history_table.bind('<<TreeviewSelect>>',lambda e:self.history_selection())
        self.history_empty=self.label(p,'',color=self.MUTED,size=10)
        self.history_empty.pack(anchor='w',pady=10)
        details=self.panel(p,padx=18,pady=18);details.pack(fill='x',pady=(8,0))
        self.history_detail=tk.StringVar(value='Select a completed transfer to open its file.')
        self.label(details,variable=self.history_detail,size=11,wraplength=390).pack(side='left',fill='x',expand=True)
        self.open_folder_button=self.button(details,'▱  Open Folder',lambda:self.open_history(True));self.open_folder_button.pack(side='right',padx=(8,0))
        self.open_file_button=self.button(details,'↗  Open File',self.open_history);self.open_file_button.pack(side='right')

    def navigate(self, view):
        # Navigation never interrupts an active transfer or reveals history on Send.
        self.view=view
        for key,button in self.nav.items():button.configure(bg=self.BLUE if key==view else self.BG)
        self.transfer_screen.pack_forget();self.history_screen.pack_forget()
        if view=='history':
            self.history_screen.pack(fill='both',expand=True);self.render_history()
            return
        self.mode=view
        self.transfer_screen.pack(fill='both',expand=True)
        self.send_panel.pack_forget();self.receive_panel.pack_forget()
        (self.send_panel if view=='send' else self.receive_panel).pack(fill='both',expand=True)
        if view=='receive':
            import shutil
            try:
                path=self.receive_folder
                while not path.exists() and path!=path.parent:path=path.parent
                self.storage.configure(text=readable_size(shutil.disk_usage(path).free)+' available on receive drive')
            except OSError:self.storage.configure(text='Storage information is unavailable')

    def choose_files(self):
        selected=filedialog.askopenfilenames(parent=self,title='Choose files to send')
        if selected:self.add_paths(selected)

    def choose_send_folder(self):
        selected=filedialog.askdirectory(parent=self,title='Choose a folder to send')
        if selected:self.add_paths([selected])

    def drop_files(self,event):
        self.add_paths(self.tk.splitlist(event.data));return 'copy'

    def add_paths(self, values):
        if self.folder_loading:
            self.status.set('Please wait for the current folder selection to finish.');return
        self.folder_loading=True;self.status.set('Reading your selection…')
        def collect():
            files=[];errors=[]
            for value in values:
                path=Path(value)
                try:
                    if path.is_dir():
                        for directory,dirs,names in os.walk(path,followlinks=False):
                            dirs[:]=[name for name in dirs if not Path(directory,name).is_symlink() and not getattr(Path(directory,name), 'is_junction', lambda: False)()]
                            for name in names:
                                file=Path(directory,name)
                                if not file.is_symlink():files.append((file.resolve(),file.stat().st_size))
                    elif path.is_file() and not path.is_symlink():files.append((path.resolve(),path.stat().st_size))
                except OSError as error:errors.append(str(error))
            self.jobs.put(('files',(files,errors)))
        threading.Thread(target=collect,daemon=True).start()

    def finish_paths(self, result):
        files,errors=result;self.folder_loading=False
        for path,size in files:
            key=str(path)
            if key not in self.paths:
                self.paths[key]=(path,size);self.checked.add(key)
                self.file_table.insert('', 'end', iid=key, values=('☑',path.name,readable_size(size),path.suffix.lstrip('.').upper() or 'File'))
        self.update_selection()
        self.status.set('Selection ready' if files else 'No readable files found')
        if errors:messagebox.showwarning(APP_NAME,'Some files could not be read.\n'+errors[0],parent=self)

    def update_selection(self):
        total=sum(self.paths[k][1] for k in self.checked)
        self.selection_label.set(f'{len(self.checked)} selected · {readable_size(total)}')

    def toggle_checked(self,event):
        if self.file_table.identify_column(event.x)=='#1':
            key=self.file_table.identify_row(event.y)
            if key:self.toggle_key(key)

    def toggle_key(self,key):
        if key in self.checked:self.checked.remove(key)
        else:self.checked.add(key)
        self.file_table.set(key,'check','☑' if key in self.checked else '☐');self.update_selection()

    def toggle_selected(self,event=None):
        for key in self.file_table.selection():self.toggle_key(key)
        return 'break'

    def remove_files(self):
        for key in self.file_table.selection():
            self.paths.pop(key,None);self.checked.discard(key);self.file_table.delete(key)
        self.update_selection()

    def choose_receive_folder(self):
        folder=filedialog.askdirectory(parent=self,title='Save received files to',initialdir=self.receive_folder if self.receive_folder.exists() else Path.home())
        if folder:
            self.receive_folder=Path(folder);self.folder_label.set(folder);self.save_settings()
            self.status.set('Folder saved. Existing links keep their original destination.')
            if self.view=='receive':self.navigate('receive')

    def create_link(self):
        if self.view=='history':return
        if self.mode=='send':
            paths=tuple(self.paths[k][0] for k in self.paths if k in self.checked)
            if not paths:
                messagebox.showinfo(APP_NAME,'Choose at least one file first.',parent=self);return
            if any(not path.is_file() for path in paths):
                messagebox.showerror(APP_NAME,'A selected file was moved or deleted. Update the selection.',parent=self);return
            session=TransferSession('send',secrets.token_urlsafe(18),file_path=paths[0],file_paths=paths)
        else:
            try:self.receive_folder.mkdir(parents=True,exist_ok=True)
            except OSError as e:messagebox.showerror(APP_NAME,str(e),parent=self);return
            session=TransferSession('receive',secrets.token_urlsafe(18),receive_folder=self.receive_folder)
        if self.service.server and not messagebox.askyesno(APP_NAME,'Create a new link? The current link and any active transfers will stop.',parent=self):return
        try:
            url=self.service.start(session)
            self.link.set(url)
            self.qr_source=qrcode.make(url).convert('RGB')
            self.resize_qr()
            self.connection_label.set('Send link active · Same local network' if session.mode=='send' else 'Receive link active · Same local network')
            self.status.set('Link ready. Keep this PC awake while sharing.')
            if urllib.parse.urlsplit(url).hostname=='127.0.0.1':self.status.set('No LAN address found. Check your network connection before sharing.')
            self.refresh_link_text();self.update_link_controls()
        except OSError as e:
            self.link.set('');self.update_link_controls();messagebox.showerror(APP_NAME,'Cannot start sharing:\n'+str(e),parent=self)

    def refresh_link_text(self):
        self.link_text.configure(state='normal');self.link_text.delete('1.0','end')
        self.link_text.insert('1.0',self.link.get() or 'Create a transfer link first.');self.link_text.configure(state='disabled')

    def resize_qr(self,event=None):
        if not self.service.url or not hasattr(self,'qr_source'):return
        size=max(64,min(230,self.pair_area.winfo_width()-8,self.pair_area.winfo_height()-8))
        self.qr_photo=ImageTk.PhotoImage(self.qr_source.resize((size,size),Image.Resampling.NEAREST))
        self.qr_label.configure(image=self.qr_photo,text='')

    def show_link_mode(self,mode):
        self.link_view=mode;self.qr_area.pack_forget();self.link_area.pack_forget()
        (self.qr_area if mode=='qr' else self.link_area).pack(fill='both',expand=True)
        self.qr_tab.configure(bg=self.BLUE if mode=='qr' else self.CARD)
        self.link_tab.configure(bg=self.BLUE if mode=='link' else self.CARD)
        self.refresh_link_text()

    def update_link_controls(self):
        for b in (self.copy_button,self.share_button,self.stop_button):b.configure(state='normal' if self.service.url else 'disabled')

    def copy_link(self):
        if self.service.url:
            self.clipboard_clear();self.clipboard_append(self.service.url);self.update_idletasks()
            self.status.set('Link copied. Share it with a device on the same network.')

    def open_link(self):
        if self.service.url:webbrowser.open(self.service.url)

    def dialog(self,title,width=530,height=340):
        win=tk.Toplevel(self);win.title(title);win.configure(bg=self.CARD);win.geometry(f'{width}x{height}')
        win.transient(self);win.after(50,_enable_dark_title_bar,win)
        return win

    def share_link(self):
        if not self.service.url:return
        win=self.dialog('Share transfer link')
        p=tk.Frame(win,bg=self.CARD,padx=24,pady=24);p.pack(fill='both',expand=True)
        self.label(p,'Share with another device',19,bold=True).pack(anchor='w')
        self.label(p,self.service.url,wraplength=470,color='#a4caff').pack(anchor='w',pady=20)
        self.button(p,'Copy Link',self.copy_link,primary=True).pack(fill='x',pady=5)
        self.button(p,'Open email draft',lambda:webbrowser.open('mailto:?subject='+urllib.parse.quote('QRLAN Drop transfer')+'&body='+urllib.parse.quote('Open this link on the same local network:\n'+self.service.url))).pack(fill='x',pady=5)
        self.button(p,'Save QR image',self.save_qr).pack(fill='x',pady=5)

    def save_qr(self):
        if not self.service.url:return
        target=filedialog.asksaveasfilename(parent=self,title='Save transfer QR',defaultextension='.png',filetypes=[('PNG image','*.png')],initialfile='QRLAN-transfer.png')
        if target:
            try:qrcode.make(self.service.url).save(target);self.status.set('QR image saved.')
            except OSError as e:messagebox.showerror(APP_NAME,str(e),parent=self)

    def scan_qr(self):
        win=self.dialog('Scan QR',560,420)
        p=tk.Frame(win,bg=self.CARD,padx=24,pady=24);p.pack(fill='both',expand=True)
        self.label(p,'Connect using a QR code',20,bold=True).pack(anchor='w')
        self.label(p,'Scan another device’s QR, or choose a QR image.',color=self.MUTED,wraplength=490).pack(anchor='w',pady=(10,20))
        self.button(p,'Use camera',lambda:self.camera_scan(win),primary=True).pack(fill='x',pady=6)
        def from_image():
            path=filedialog.askopenfilename(parent=win,title='Open QR image',filetypes=[('Images','*.png *.jpg *.jpeg *.bmp *.webp')])
            if path:
                def decode():
                    try:self.jobs.put(('scanned',decode_qr_image(path)))
                    except Exception as e:self.jobs.put(('scan_error',str(e)))
                threading.Thread(target=decode,daemon=True).start()
        self.button(p,'Open QR image',from_image).pack(fill='x',pady=6)
        self.label(p,'Or paste a local transfer link',color=self.MUTED,size=10).pack(anchor='w',pady=(18,6))
        entry=tk.Entry(p,bg=self.ALT,fg=self.INK,insertbackground=self.INK,relief='flat');entry.pack(fill='x',ipady=10)
        self.button(p,'Review link',lambda:self.review_scanned(entry.get())).pack(fill='x',pady=10)

    def review_scanned(self,url):
        if not valid_transfer_url(url):
            messagebox.showerror(APP_NAME,'Use an HTTP or HTTPS link to a local device.',parent=self);return
        win=self.dialog('Open scanned link',560,260)
        p=tk.Frame(win,bg=self.CARD,padx=24,pady=24);p.pack(fill='both',expand=True)
        self.label(p,'QR link found',20,bold=True).pack(anchor='w')
        self.label(p,url.strip(),wraplength=490,color='#a4caff').pack(anchor='w',pady=24)
        self.button(p,'Open in browser',lambda:(webbrowser.open(url.strip()),win.destroy()),primary=True).pack(fill='x')

    def camera_scan(self,parent):
        win=self.dialog('Camera · Scan QR',640,540)
        preview=self.label(win,'Starting camera…',anchor='center');preview.pack(fill='both',expand=True,padx=15,pady=15)
        stop=threading.Event();frames=queue.Queue(maxsize=1)
        self.camera_stop=stop
        def close():stop.set();win.destroy()
        win.protocol('WM_DELETE_WINDOW',close)
        self.button(win,'Cancel',close).pack(pady=12)
        def capture():
            import cv2
            camera=cv2.VideoCapture(0)
            try:
                if not camera.isOpened():frames.put(('error','No camera is available. Use Open QR image instead.'));return
                detector=cv2.QRCodeDetector()
                while not stop.is_set():
                    ok,frame=camera.read()
                    if not ok:break
                    value,_points,_straight=detector.detectAndDecode(frame)
                    if value:
                        self.jobs.put(('scanned',value));stop.set();break
                    rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
                    image=Image.fromarray(rgb);image.thumbnail((590,380))
                    try:frames.put_nowait(('frame',image))
                    except queue.Full:pass
            finally:camera.release()
        threading.Thread(target=capture,daemon=True).start()
        def show():
            if not win.winfo_exists():return
            if stop.is_set():win.destroy();return
            try:
                kind,value=frames.get_nowait()
                if kind=='frame':preview.photo=ImageTk.PhotoImage(value);preview.configure(image=preview.photo,text='')
                else:preview.configure(text=value,wraplength=520)
            except queue.Empty:pass
            win.after(70,show)
        show()

    def on_progress(self,message,current,total,path=None):
        # No filenames or completed-file records leak into the main screen.
        if message.startswith(('Received:','Downloaded:')):self.status.set('Transfer complete. Open History for details.')
        elif message.startswith(('Receiving','Downloading')):
            pct=f' · {current*100/total:.0f}%' if total else ''
            self.status.set('Transfer in progress'+pct)
        elif 'failed' in message.lower() or 'unavailable' in message.lower():self.status.set(message)
        elif 'cancelled' in message.lower():self.status.set('Transfer cancelled. See History for details.')

    def record_transfer(self,record):
        self.history.insert(0,record)
        try:self.store.save('history',self.history)
        except (OSError,ValueError):self.status.set('Transfer recorded for this session; history could not be saved to disk.')
        if self.view=='history':self.render_history()

    def filter_history(self,value):self.history_filter.set(value);self.render_history()

    def render_history(self):
        if self.view!='history':return
        selected=self.history_table.selection()
        self.history_table.delete(*self.history_table.get_children())
        for row in self.history:
            if self.history_filter.get()!='All' and row['direction']!=self.history_filter.get():continue
            date=row['date'].replace('T',' ')[:16]
            self.history_table.insert('','end',iid=row['id'],values=(row['name'],row['direction'],readable_size(row['size']),row['status'],date),tags=(row['status'],))
        for name,b in self.filter_buttons.items():b.configure(bg=self.BLUE if self.history_filter.get()==name else self.CARD)
        if selected and self.history_table.exists(selected[0]):self.history_table.selection_set(selected[0])
        self.history_empty.configure(text='' if self.history_table.get_children() else 'No transfers here yet. Send or receive a file to get started.')
        self.history_selection()

    def selected_record(self):
        selected=self.history_table.selection()
        return next((r for r in self.history if selected and r['id']==selected[0]),None)

    def history_selection(self):
        record=self.selected_record()
        available=bool(record and record.get('path') and Path(record['path']).is_file())
        self.history_detail.set(record['name']+' · '+record['status'] if record else 'Select a completed transfer to open its file.')
        for b in (self.open_file_button,self.open_folder_button):b.configure(state='normal' if available else 'disabled')

    def open_history(self,folder=False):
        record=self.selected_record()
        if not record or not record.get('path'):return
        path=Path(record['path'])
        if not path.is_file():messagebox.showinfo(APP_NAME,'This file has been moved or deleted.',parent=self);return
        try:os.startfile(str(path.parent if folder else path))
        except OSError as e:messagebox.showerror(APP_NAME,str(e),parent=self)

    def save_settings(self):
        try:self.store.save('settings',{'receive_folder':str(self.receive_folder)})
        except (OSError,ValueError):self.status.set('Settings could not be saved. Check local storage permissions.')

    def settings(self):
        win=self.dialog('Settings',560,330)
        p=tk.Frame(win,bg=self.CARD,padx=24,pady=24);p.pack(fill='both',expand=True)
        self.label(p,'Preferences',21,bold=True).pack(anchor='w')
        self.label(p,'Receive folder',color=self.MUTED).pack(anchor='w',pady=(22,8))
        self.label(p,variable=self.folder_label,wraplength=480).pack(anchor='w')
        self.button(p,'Change receive folder',self.choose_receive_folder).pack(fill='x',pady=18)
        self.label(p,'Appearance: Dark\nHistory is stored only on this PC.',color=self.MUTED,size=10).pack(anchor='w')

    def about(self):
        messagebox.showinfo(APP_NAME,f'QRLAN Drop {APP_VERSION}\nFluent Commander\n\nDirect file sharing on your local network.\nKeep this PC awake while transferring.\nNo artificial file-size limit.\n\nMIT License',parent=self)

    def stop_transfer(self):
        self.service.stop();self.service.poll();self.link.set('')
        self.qr_photo=None;self.qr_label.configure(image='',text='Your QR code will appear here')
        self.refresh_link_text();self.update_link_controls()
        self.connection_label.set('Create a link to connect');self.status.set('Transfer stopped. The old link is inactive.')

    def _poll(self):
        if self.closed:return
        self.service.poll()
        for _ in range(20):
            try:kind,value=self.jobs.get_nowait()
            except queue.Empty:break
            if kind=='files':self.finish_paths(value)
            elif kind=='scanned':self.review_scanned(value)
            elif kind=='scan_error':messagebox.showerror(APP_NAME,value,parent=self)
        self.after(70,self._poll)

    def close(self):
        self.closed=True
        if hasattr(self,'camera_stop'):self.camera_stop.set()
        self.service.stop();self.service.poll();self.destroy()
