#!/usr/bin/env python3
"""Native Windows interface for adding exported songs to an iPhone."""
import asyncio
import argparse
import json
import queue
import sqlite3
import threading
import time
import traceback
from contextlib import closing
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import music_importer as backend
import direct_phone
import direct_preflight
from direct_inventory import parse_inventory
from automatic_playlists import match_playlist, check_folder_names
import ordered_transfer
from direct_adapter import DirectAdapter
from direct_lock import locked_phone

APP_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = APP_DIR / 'MusicExport_Faithful'
JOURNAL = APP_DIR / 'direct_import_history.sqlite'
ORDER_OPTIONS = {
    'Newest file date first': 'newest',
}


def transfer_order(songs, label):
    # Showing newest first while adding oldest first makes newest the last
    # addition for the iPhone's Date Added sorting.
    return list(reversed(songs)) if label == 'Newest file date first' else list(songs)


def batch_size_argument(value):
    try:
        size=int(value)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError('batch size must be a whole number from 1 to 100') from exc
    if not 1 <= size <= 100:
        raise argparse.ArgumentTypeError('batch size must be from 1 to 100')
    return size


def worker_argument(value, maximum=16):
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f'workers must be a whole number from 1 to {maximum}') from exc
    if not 1 <= count <= maximum:
        raise argparse.ArgumentTypeError(f'workers must be from 1 to {maximum}')
    return count


def validate_confirmed_plan(context, songs, bindings):
    """Refuse folder or file changes after the confirmation preview."""
    root=context['root']
    source=context['source']
    if context['batch']:
        groups=[c for c in backend.collections(root) if not c.is_rest and not c.all_music]
        if {g.name for g in groups} != {b.name for b in bindings}:
            raise backend.ImportErrorSafe('Playlist folders changed since confirmation; review the import again.')
        mappings=[(g,{'id':None,'new_name':g.name}) for g in groups]
        current,_,_,current_bindings=backend.prepare_import_plan(root,source,mappings,{g.name for g in groups})
    else:
        current,_,_=backend.build_plan(root,source)
        current_bindings=[]
    fingerprint=lambda values:{(s.path,s.size,s.modified_ns,s.file_date_ns) for s in values}
    if fingerprint(current)!=fingerprint(songs):
        raise backend.ImportErrorSafe('Music files or dates changed since confirmation; review the import again.')
    if context['batch']:
        original={b.name:fingerprint(b.songs) for b in bindings}
        if {b.name:fingerprint(b.songs) for b in current_bindings}!=original:
            raise backend.ImportErrorSafe('Playlist files changed since confirmation; review the import again.')


class ImportWindow:
    def __init__(self, root, batch_size=100, format_workers=16, hash_workers=16, metadata_workers=16, readback_workers=8):
        self.root = root
        root.title('iPhone Music Import')
        root.minsize(790, 710)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.messages = queue.Queue()
        self.devices = {}
        self.destinations = {}
        self.sources = {}
        self.songs = []
        self.unsupported = []
        self.plan_generation = 0
        self.device_generation = 0
        self.importing = False
        self.closing = False
        self.stop_event = threading.Event()
        self.last_report = None

        self.source_root = tk.StringVar(value=str(DEFAULT_SOURCE) if DEFAULT_SOURCE.exists() else '')
        self.source_name = tk.StringVar()
        self.order = tk.StringVar(value='Newest file date first')
        self.device = tk.StringVar()
        self.destination = tk.StringVar()
        self.new_name = tk.StringVar()
        self.skip_possible = tk.BooleanVar(value=True)
        self.batch_mode = tk.BooleanVar(value=True)
        self.batch_size = tk.StringVar(value=str(batch_size))
        self.format_workers = tk.StringVar(value=str(format_workers))
        self.hash_workers = tk.StringVar(value=str(hash_workers))
        self.metadata_workers = tk.StringVar(value=str(metadata_workers))
        self.readback_workers = tk.StringVar(value=str(readback_workers))
        self.fast_existing = tk.BooleanVar(value=False)
        self.phase_started = None
        self.phase_key = None
        self.map_note = tk.StringVar(value='Playlist folders will match automatically by name.')
        self.note = tk.StringVar(value='Choose the export folder and a collection.')
        self.status = tk.StringVar(value='Connect your new iPhone, then select Check iPhone.')
        self.counts = tk.StringVar(value='No import started')

        pane = ttk.Frame(root, padding=16)
        pane.grid(sticky='nsew')
        root.columnconfigure(0, weight=1); root.rowconfigure(0, weight=1)
        pane.columnconfigure(1, weight=1); pane.rowconfigure(7, weight=1)
        ttk.Label(pane, text='iPhone Music Import', font=('Segoe UI', 17, 'bold')).grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 12))

        ttk.Label(pane, text='Music export').grid(row=1, column=0, sticky='w', padx=(0, 10), pady=3)
        self.source_entry = ttk.Entry(pane, textvariable=self.source_root, state='readonly')
        self.source_entry.grid(row=1, column=1, columnspan=2, sticky='ew', pady=3)
        self.browse_button = ttk.Button(pane, text='Browse', command=self.browse_source)
        self.browse_button.grid(row=1, column=3, padx=(8,0), pady=3)

        ttk.Label(pane, text='Songs to import').grid(row=2, column=0, sticky='w', padx=(0, 10), pady=3)
        self.source_combo = ttk.Combobox(pane, textvariable=self.source_name, state='readonly')
        self.source_combo.grid(row=2, column=1, sticky='ew', pady=3)
        self.source_combo.bind('<<ComboboxSelected>>', lambda _e: self.source_changed())
        ttk.Label(pane, text='Order').grid(row=2, column=2, padx=(12, 6))
        self.order_combo = ttk.Combobox(pane, textvariable=self.order, values=list(ORDER_OPTIONS), state='readonly', width=22)
        self.order_combo.grid(row=2, column=3, sticky='ew')
        self.order_combo.bind('<<ComboboxSelected>>', lambda _e: self.load_plan())

        ttk.Label(pane, text='New iPhone').grid(row=3, column=0, sticky='w', padx=(0,10), pady=3)
        self.device_combo = ttk.Combobox(pane, textvariable=self.device, state='readonly')
        self.device_combo.grid(row=3, column=1, columnspan=2, sticky='ew', pady=3)
        self.device_combo.bind('<<ComboboxSelected>>', lambda _e: self.load_destinations())
        self.check_button = ttk.Button(pane, text='Check iPhone', command=self.check_itunes)
        self.check_button.grid(row=3, column=3, padx=(8,0), pady=3)

        ttk.Label(pane, text='Import into').grid(row=4, column=0, sticky='w', padx=(0,10), pady=3)
        self.dest_combo = ttk.Combobox(pane, textvariable=self.destination, state='readonly')
        self.dest_combo.grid(row=4, column=1, columnspan=2, sticky='ew', pady=3)
        self.dest_combo.bind('<<ComboboxSelected>>', lambda _e: self.destination_changed())
        self.new_entry = ttk.Entry(pane, textvariable=self.new_name, state='disabled')
        self.new_entry.grid(row=4, column=3, sticky='ew', padx=(8,0), pady=3)

        options=ttk.Frame(pane); options.grid(row=5,column=0,columnspan=4,sticky='ew',pady=(8,0))
        self.skip_check = ttk.Checkbutton(options, text='Stop if the phone contains unverified music', variable=self.skip_possible, state='disabled')
        self.skip_check.grid(row=0,column=0,sticky='w')
        self.batch_check = ttk.Checkbutton(options, text='Import all music, then link playlists', variable=self.batch_mode,
                                            command=self.update_batch_controls)
        self.batch_check.grid(row=1,column=0,sticky='w',pady=(3,0))
        ttk.Label(options,text='Playlist folders match by name automatically').grid(row=1,column=1,padx=(10,0),sticky='w')
        ttk.Label(options,text='Songs per batch').grid(row=0,column=2,padx=(16,4),sticky='e')
        self.batch_size_control=ttk.Spinbox(options,from_=1,to=100,textvariable=self.batch_size,width=5)
        self.batch_size_control.grid(row=0,column=3,sticky='w')
        ttk.Label(options,textvariable=self.map_note,foreground='#555555').grid(row=2,column=0,columnspan=2,sticky='w')
        performance=ttk.Frame(options); performance.grid(row=3,column=0,columnspan=4,sticky='w',pady=(6,0))
        self.worker_controls=[]
        for label, variable, maximum in (('Format checks',self.format_workers,16),
                                         ('File hashes',self.hash_workers,16),
                                         ('Metadata',self.metadata_workers,16),
                                         ('Phone reads',self.readback_workers,8)):
            ttk.Label(performance,text=label).pack(side='left',padx=(0,3))
            control=ttk.Spinbox(performance,from_=1,to=maximum,textvariable=variable,width=3)
            control.pack(side='left',padx=(0,12)); self.worker_controls.append(control)
        self.fast_check=ttk.Checkbutton(options,text='Fast check for previously verified songs (no byte readback)',variable=self.fast_existing)
        self.fast_check.grid(row=4,column=0,columnspan=4,sticky='w',pady=(4,0))
        ttk.Label(options,text='Workers per stage; uploads are sent in order.',foreground='#555555').grid(row=5,column=0,columnspan=4,sticky='w')
        ttk.Label(pane, textvariable=self.note, wraplength=750).grid(row=6, column=0, columnspan=4, sticky='w', pady=(6,5))

        table_frame = ttk.Frame(pane)
        table_frame.grid(row=7, column=0, columnspan=4, sticky='nsew')
        table_frame.columnconfigure(0, weight=1); table_frame.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(table_frame, columns=('order','title','artist','date'), show='headings', selectmode='browse', height=9)
        for key, label, width in [('order','#',45),('title','Title',370),('artist','Artist',175),('date','Saved date',140)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=40, stretch=(key in ('title','artist')))
        self.table.grid(row=0, column=0, sticky='nsew')
        scroll=ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        scroll.grid(row=0, column=1, sticky='ns'); self.table.configure(yscrollcommand=scroll.set)
        ttk.Label(pane, text='Newest is shown first; files are copied oldest first.').grid(row=8,column=0,columnspan=4,sticky='w',pady=(5,0))

        self.bar = ttk.Progressbar(pane, mode='determinate')
        self.bar.grid(row=9, column=0, columnspan=4, sticky='ew', pady=(12,2))
        ttk.Label(pane, textvariable=self.counts).grid(row=10, column=0, columnspan=4, sticky='w')
        ttk.Label(pane, textvariable=self.status, wraplength=750).grid(row=11, column=0, columnspan=4, sticky='w', pady=(6,8))
        bottom=ttk.Frame(pane); bottom.grid(row=12,column=0,columnspan=4,sticky='ew')
        self.start_button=ttk.Button(bottom,text='Import to iPhone',command=self.start,state='disabled')
        self.start_button.pack(side='left')
        self.stop_button=ttk.Button(bottom,text='Stop after current batch',command=self.stop,state='disabled')
        self.stop_button.pack(side='left',padx=8)
        self.report_button=ttk.Button(bottom,text='Open report',command=self.open_report,state='disabled')
        self.report_button.pack(side='right')
        ttk.Label(pane, text='Songs import in batches, oldest first; playlists link after upload.\n'
                  'When dates are equal, the source path breaks ties.',
                  foreground='#555555').grid(row=13,column=0,columnspan=4,sticky='w',pady=(8,0))

        self.load_sources()
        root.after(100,self.poll)

    def browse_source(self):
        name=filedialog.askdirectory(title='Choose the music export or a folder of songs',mustexist=True)
        if name:
            self.source_root.set(name); self.load_sources()

    def load_sources(self):
        self.plan_generation += 1
        try:
            root=Path(self.source_root.get()).expanduser().resolve(strict=True)
            groups=backend.collections(root)
            self.sources={c.name:c for c in groups}
            self.source_combo['values']=list(self.sources)
            if self.source_name.get() not in self.sources:
                self.source_name.set(next(iter(self.sources),''))
            self.source_changed()
        except Exception as exc:
            self.sources={}; self.source_combo['values']=(); self.source_name.set('')
            self.songs=[]; self.show_songs(); self.note.set(str(exc))

    def load_plan(self):
        selected=self.sources.get(self.source_name.get())
        if selected is None:
            return
        self.plan_generation+=1; generation=self.plan_generation
        root=Path(self.source_root.get()).expanduser().resolve()
        order=ORDER_OPTIONS[self.order.get()]
        self.songs=[]; self.show_songs()
        self.update_start_control()
        self.note.set('Reading song names and saved order…')
        def worker():
            try:
                songs,unsupported,note=backend.build_plan(root,selected,order)
                self.messages.put(('plan',(generation,songs,unsupported,note)))
            except Exception as exc:
                self.messages.put(('plan_error',(generation,str(exc))))
        threading.Thread(target=worker,daemon=True).start()

    def show_songs(self, selection=None):
        self.table.delete(*self.table.get_children())
        for index,song in enumerate(self.songs):
            saved=datetime.fromtimestamp((song.file_date_ns or song.modified_ns)/1e9).strftime('%Y-%m-%d %H:%M')
            self.table.insert('', 'end', iid=str(index), values=(index+1,song.title,song.artist,saved))
        if selection is not None and 0 <= selection < len(self.songs):
            self.table.selection_set(str(selection)); self.table.see(str(selection))

    def source_changed(self):
        selected=self.sources.get(self.source_name.get())
        if not selected or not selected.all_music:
            self.batch_mode.set(False)
        self.update_batch_controls()
        self.load_plan()

    def update_batch_controls(self):
        selected=self.sources.get(self.source_name.get())
        is_all=bool(selected and selected.all_music)
        self.batch_check.config(state='disabled' if self.importing or not is_all else 'normal')
        if not is_all:
            self.map_note.set('One collection will be imported into the selected destination.')
        elif not self.batch_mode.get():
            self.map_note.set('All files will be imported into the selected destination.')
        else:
            self.map_note.set('Existing playlists are matched by name; missing playlists are created.')
        self.update_start_control()

    def update_start_control(self):
        device=self.devices.get(self.device.get())
        dest=self.destinations.get(self.destination.get())
        source=self.sources.get(self.source_name.get())
        batch=bool(source and source.all_music and self.batch_mode.get())
        ready=(not self.importing and bool(self.songs) and device is not None
               and dest is not None
               and source is not None and (not batch or dest['kind'] in (1,4)))
        self.start_button.config(state='normal' if ready else 'disabled')

    def check_itunes(self):
        self.device_generation+=1; generation=self.device_generation
        self.devices={};self.device.set('');self.device_combo['values']=()
        self.destinations={};self.destination.set('');self.dest_combo['values']=()
        self.update_batch_controls()
        self.check_button.config(state='disabled')
        self.status.set('Checking paired iPhones over USB…')
        def worker():
            try:
                found=asyncio.run(direct_phone.devices())
                result=[{'id':d.serial,'name':d.name,'source_id':d.serial[-6:]} for d in found]
                self.messages.put(('devices',(generation,result)))
            except Exception as exc:
                self.messages.put(('devices_error',(generation,str(exc))))
        threading.Thread(target=worker,daemon=True).start()

    def load_destinations(self):
        selected=self.devices.get(self.device.get())
        self.destinations={};self.dest_combo['values']=();self.destination.set('')
        self.update_batch_controls()
        if not selected: return
        self.device_generation+=1;generation=self.device_generation
        self.status.set('Reading playlists from the selected phone…')
        def worker():
            try:
                catalog=asyncio.run(direct_phone.read_catalog(selected['id']))
                values=[{'id':str(catalog.library_id),'name':selected['name'],'kind':1,'count':catalog.song_count}]
                values.extend({'id':str(p.id),'name':p.name,'kind':2,'count':len(p.members)} for p in catalog.playlists)
                self.messages.put(('destinations',(generation,values)))
            except Exception as exc:
                self.messages.put(('destinations_error',(generation,str(exc))))
        threading.Thread(target=worker,daemon=True).start()

    def destination_changed(self):
        if self.destination.get() == 'Create a new playlist…':
            self.new_entry.config(state='normal')
        else:
            self.new_entry.config(state='disabled')
        self.update_start_control()

    def set_running(self, running):
        self.importing=running
        for control in (self.browse_button,self.check_button,self.batch_size_control,self.fast_check,*self.worker_controls):
            control.config(state='disabled' if running else 'normal')
        self.source_entry.config(state='disabled' if running else 'readonly')
        for control in (self.source_combo,self.order_combo,self.device_combo,self.dest_combo):
            control.config(state='disabled' if running else 'readonly')
        self.start_button.config(state='disabled')
        self.stop_button.config(state='normal' if running else 'disabled')
        if running: self.new_entry.config(state='disabled')
        else: self.destination_changed()
        self.update_batch_controls()

    def start(self):
        if self.importing:return
        try:
            requested_batch=batch_size_argument(self.batch_size.get())
            format_workers=worker_argument(self.format_workers.get())
            hash_workers=worker_argument(self.hash_workers.get())
            metadata_workers=worker_argument(self.metadata_workers.get())
            readback_workers=worker_argument(self.readback_workers.get(),8)
        except argparse.ArgumentTypeError as exc:
            messagebox.showerror('Invalid performance settings',str(exc));return
        device=self.devices.get(self.device.get())
        dest=self.destinations.get(self.destination.get())
        creating=False
        name=None
        source=self.sources.get(self.source_name.get())
        batch=bool(source and source.all_music and self.batch_mode.get())
        if not device or (not dest and not creating) or (creating and not name):
            messagebox.showerror('Choose a destination','Select the iPhone and a playlist, or enter a new playlist name.')
            return
        if source is None:
            messagebox.showerror('No collection','Choose the music collection to import.')
            return
        if batch and (creating or not dest or dest['kind'] not in (1,4)):
            messagebox.showerror('Choose All Music','For the whole-library workflow, choose All Music on the selected iPhone first.')
            return
        target_id=None if creating else dest['id']
        source_root=Path(self.source_root.get()).expanduser().resolve()
        context={'device':device,'dest':dest,'name':name,'target_id':target_id,'batch':batch,
                 'source':source,'root':source_root,'batch_size':requested_batch,
                 'format_workers':format_workers,'hash_workers':hash_workers,
                 'metadata_workers':metadata_workers,'readback_workers':readback_workers,
                 'fast_existing':self.fast_existing.get()}
        self.stop_event.clear();self.set_running(True)
        self.phase_key=None;self.phase_started=None
        self.bar.config(maximum=1,value=0)
        self.counts.set('Refreshing the files and dates…')
        self.status.set('Checking for songs added to the folders since the preview…')
        self.last_report=None;self.report_button.config(state='disabled')
        def preflight():
            try:
                groups=[c for c in backend.collections(source_root) if not c.is_rest and not c.all_music] if batch else []
                check_folder_names([g.name for g in groups])
                auto_mappings=[(g,{'id':None,'new_name':g.name}) for g in groups]
                songs,unsupported,note,bindings=backend.prepare_import_plan(
                    source_root,source,auto_mappings if batch else [],{g.name for g in groups} if batch else None,self.stop_event)
                if not batch and dest and dest['kind']==2:
                    bindings=[backend.PlaylistBinding(source.name,dest['id'],None,songs)]
                errors=direct_preflight.scan(songs,self.stop_event,
                    lambda done,total,issues:self.messages.put(('preflight_progress',(done,total,issues))),
                    workers=format_workers)
                if not self.stop_event.is_set() and not errors:
                    self.messages.put(('preflight_stage','Checking artist and album groups on the phone…'))
                    data,wal=asyncio.run(direct_phone.read_snapshot(device['id']))
                    if batch:
                        catalog=direct_phone.parse_catalog(data,wal)
                        context['automatic_preview']=[(b.name,
                            str(match.id) if (match:=match_playlist(catalog,b.name)) else None)
                            for b in bindings]
                    verified={}
                    if JOURNAL.is_file():
                        with closing(sqlite3.connect(JOURNAL.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                            verified={path:sha for path,sha in db.execute(
                                'SELECT path,sha FROM uploads WHERE device=? AND status=?',
                                (device['id'],'verified'))}
                    errors.extend(direct_preflight.grouping_scan(songs,parse_inventory(data,wal),verified))
                if self.stop_event.is_set():
                    raise backend.ImportErrorSafe('Stopped before the import started.')
                if batch and {c.name for c in backend.collections(source_root) if not c.is_rest and not c.all_music} != {g.name for g in groups}:
                    raise backend.ImportErrorSafe('Playlist folders changed during checking; review the import again.')
                if errors:
                    reports=APP_DIR/'import_reports';reports.mkdir(exist_ok=True)
                    report=reports/('preflight_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.json')
                    report.write_text(json.dumps({'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
                    self.last_report=report
                    raise backend.ImportErrorSafe(f'{len(errors)} files need review. Open the report before importing.')
                self.messages.put(('preflight_ready',(context,songs,unsupported,note,bindings)))
            except Exception as exc:
                self.messages.put(('preflight_error',str(exc)))
        threading.Thread(target=preflight,daemon=True).start()

    def confirm_preflight(self, value):
        context,songs,unsupported,note,bindings=value
        if self.stop_event.is_set():
            self.set_running(False);self.status.set('Stopped before starting the import.')
            if self.closing:self.root.destroy()
            return
        self.songs=songs;self.unsupported=unsupported;self.show_songs()
        self.note.set(f'{len(songs):,} songs. {note}' + (f' {len(unsupported)} unsupported files.' if unsupported else ''))
        if not songs:
            self.set_running(False);messagebox.showerror('No songs','No supported music files were found.')
            return
        target=context['name'] if context['name'] else context['dest']['name']
        details=(f'Add up to {len(songs):,} songs to {target} on {context["device"]["name"]}?\n\n'
                 f'Songs are copied in batches of up to {context["batch_size"]}, oldest first, with their saved dates. '
                 'Equal dates use source-path order. The phone controls All Music sorting. '
                 'The importer stops if it finds music it cannot verify.')
        if context.get('fast_existing',False):
            details += ('\n\nFast check: previously verified phone files are checked by metadata and size, '
                        'not by reading their bytes. Changes that keep the same size may go undetected. '
                        'New uploads receive full byte verification.')
        if bindings:
            mapping_lines=[]
            for binding in bindings:
                if context['batch']:
                    expected=dict(context['automatic_preview'])[binding.name]
                    label='existing playlist' if expected is not None else 'new playlist'
                    mapping_lines.append(f'{binding.name} ({len(binding.songs)} songs) → {label}')
                else:
                    mapping_lines.append(f'{binding.name} ({len(binding.songs)} songs) → {context["dest"]["name"]}')
            details+='\n\nPlaylist destinations:\n'+ ('\n'.join(mapping_lines) if mapping_lines else 'None selected')
        if unsupported:
            details+=f'\n\n{len(unsupported)} unsupported file entries will not be imported.'
        if not messagebox.askyesno('Confirm import',details):
            self.set_running(False);self.status.set('Import was not started.');return
        self.begin_transfer(context,songs,bindings)

    def begin_transfer(self, context, preview_songs, bindings):
        device=context['device']
        songs=list(preview_songs)
        automatic=[(b.name,b.songs,dict(context['automatic_preview'])[b.name]) for b in bindings] if context['batch'] else []
        playlists=[] if context['batch'] else [(binding.destination_id,binding.songs) for binding in bindings]
        self.bar.config(maximum=max(len(songs),1),value=0)
        self.counts.set(f'0 of {len(songs):,} processed')
        self.status.set('Connecting to the selected iPhone…')
        reports=APP_DIR/'import_reports'; reports.mkdir(exist_ok=True)
        report=reports/('import_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.jsonl')
        self.last_report=report
        def worker():
            try:
                with report.open('w',encoding='utf-8') as log:
                    log.write(json.dumps({'existing_phone_check': 'metadata_and_size' if context['fast_existing'] else 'full_readback',
                                          'new_upload_check': 'full_readback'})+'\n')
                    def progress(phase,index,total,filename):
                        record={'phase':phase,'index':index,'total':total,'file':filename}
                        log.write(json.dumps(record,ensure_ascii=False)+'\n');log.flush()
                        self.messages.put(('direct_progress',record))
                    try:
                        async def transfer():
                            await asyncio.to_thread(validate_confirmed_plan,context,songs,bindings)
                            if self.stop_event.is_set():
                                return {'stopped':True,'uploaded_or_verified':0}
                            async with locked_phone(device['id']) as (client,afc):
                                adapter=DirectAdapter(device['id'],client,afc,
                                    metadata_workers=context['metadata_workers'],readback_workers=context['readback_workers'],
                                    fast_existing=context['fast_existing'])
                                return await ordered_transfer.run(adapter,device['id'],songs,playlists,
                                                                  JOURNAL,progress,self.stop_event,
                                                                  batch_size=context['batch_size'],automatic_playlists=automatic,
                                                                  hash_workers=context['hash_workers'])
                        result=asyncio.run(transfer())
                        log.write(json.dumps({'result':result,'stopped':self.stop_event.is_set()},ensure_ascii=False)+'\n')
                        self.messages.put(('direct_done',result))
                    except Exception as exc:
                        log.write(json.dumps({'error':str(exc)},ensure_ascii=False)+'\n')
                        self.messages.put(('import_error',str(exc)))
            except Exception as exc:
                self.messages.put(('import_error',str(exc)))
        threading.Thread(target=worker,daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.stop_button.config(state='disabled')
        self.status.set('Stopping after the current batch finishes…')

    def open_report(self):
        if self.last_report and self.last_report.exists():
            import os
            os.startfile(self.last_report)

    def poll(self):
        while True:
            try: kind,value=self.messages.get_nowait()
            except queue.Empty: break
            if kind=='plan':
                generation,songs,unsupported,note=value
                if generation!=self.plan_generation:continue
                self.songs=songs;self.unsupported=unsupported;self.show_songs()
                self.note.set(f'{len(songs):,} songs. {note}' + (f' {len(unsupported)} unsupported files.' if unsupported else ''))
                self.update_start_control()
            elif kind=='plan_error':
                generation,error=value
                if generation==self.plan_generation:self.note.set(error)
            elif kind=='preflight_ready':
                self.confirm_preflight(value)
            elif kind=='preflight_progress':
                done,total,issues=value
                self.bar.config(maximum=max(total,1),value=done)
                now=time.monotonic()
                if self.phase_key != 'preflight':
                    self.phase_key='preflight';self.phase_started=now;self.phase_initial=done
                elapsed=now-self.phase_started
                speed=f' · {(done-self.phase_initial)/elapsed:.1f} files/s' if elapsed>=2 and done>self.phase_initial else ''
                self.counts.set(f'Checking formats ({self.format_workers.get()} workers): {done:,} of {total:,} · {issues:,} need review{speed}')
            elif kind=='preflight_stage':
                self.status.set(value)
            elif kind=='preflight_error':
                self.set_running(False);self.status.set(value)
                if self.last_report:self.report_button.config(state='normal')
                if not self.stop_event.is_set():messagebox.showerror('Cannot prepare import',value)
                if self.closing:self.root.destroy();return
            elif kind=='devices':
                generation,devices=value
                if generation!=self.device_generation:continue
                self.check_button.config(state='normal')
                self.devices={f'{x["name"]} (device {x["source_id"]})':x for x in devices}
                self.device_combo['values']=list(self.devices)
                self.device.set('');self.destinations={};self.destination.set('');self.dest_combo['values']=()
                if len(self.devices)==1:
                    self.device.set(next(iter(self.devices)))
                    self.load_destinations()
                else:
                    self.status.set('Choose the new iPhone.' if devices else 'No paired USB iPhone found. Connect, unlock and trust this computer.')
            elif kind=='devices_error':
                generation,error=value
                if generation==self.device_generation:
                    self.check_button.config(state='normal');self.status.set(error)
            elif kind=='destinations':
                generation,values=value
                if generation!=self.device_generation:continue
                self.destinations={f'{"All Music" if x["kind"] in (1,4) else "Playlist"}: {x["name"]} ({x["count"]:,} songs; ID {x["id"]})':x for x in values}
                has_library=any(x['kind'] in (1,4) for x in values)
                self.dest_combo['values']=list(self.destinations)
                if has_library:
                    first_library=next(label for label,x in self.destinations.items() if x['kind'] in (1,4))
                    self.destination.set(first_library)
                has_playlists=any(x['kind']==2 for x in values)
                self.status.set(('Phone and playlists detected. Select your destinations.' if has_playlists else
                    'Phone detected; no existing playlists found.') if has_library else
                    'Could not read the phone music library.')
                self.update_batch_controls()
            elif kind=='destinations_error':
                generation,error=value
                if generation==self.device_generation:self.status.set(error)
            elif kind=='direct_progress':
                phase=value['phase'];done=value['index'];total=value['total']
                self.bar.config(maximum=max(total,1),value=done if phase in ('checking_local','checking_metadata','checking_phone','preparing_batch','verified','playlist') else max(done-1,0))
                labels={'checking_local':f'Hashing local files ({self.hash_workers.get()} workers)',
                        'checking_metadata':f'Preparing metadata ({self.metadata_workers.get()} workers)',
                        'checking_phone':f'{"Fast checking" if self.fast_existing.get() else "Verifying"} phone files ({self.readback_workers.get()} workers)',
                        'preparing_batch':f'Preparing batches ({self.metadata_workers.get()} metadata workers)',
                        'uploading':'Uploading','verified':'Verified','playlist':'Linking playlist'}
                label=labels.get(phase,phase.capitalize())
                now=time.monotonic()
                if phase != self.phase_key:
                    self.phase_key=phase; self.phase_started=now; self.phase_initial=done
                elapsed=now-self.phase_started
                speed=f' · {(done-self.phase_initial)/elapsed:.1f} files/s' if elapsed>=2 and done>self.phase_initial and phase in ('checking_local','checking_metadata','checking_phone') else ''
                self.counts.set(f'{label}: {done:,} of {total:,}{speed}')
                self.status.set(f'{label}: {value["file"]}')
            elif kind=='direct_done':
                self.set_running(False);self.report_button.config(state='normal')
                self.status.set(('Stopped. ' if value['stopped'] else 'Finished. ')+
                                f'{value["uploaded_or_verified"]:,} songs verified.')
                if self.closing:self.root.destroy();return
            elif kind=='progress':
                c=value['counts'];done=c['processed']
                stage='Building playlist' if c['stage']=='playlist' else 'Checking and copying to All Music'
                if 'batch_total' in c:
                    self.bar.config(maximum=max(c['batch_total'],1),value=c['batch_done'])
                    self.counts.set(f'{c["batch_label"]}: {stage} {done:,}/{c["total"]:,} · overall {c["batch_done"]:,}/{c["batch_total"]:,}')
                else:
                    self.bar.config(maximum=max(c['total'],1),value=done)
                    self.counts.set(f'{stage}: {done:,} of {c["total"]:,} · {c["possible"]:,} possible matches skipped')
                phase=value['phase']
                self.status.set('Checking music already on the selected phone…' if phase=='checking' else f'{phase.capitalize()}: {value["file"]}')
            elif kind=='done':
                result,stopped=value;self.set_running(False);self.report_button.config(state='normal')
                self.status.set(('Stopped. ' if stopped else 'Finished. ')+
                    f'{result["added"]} copied, {result["reused"]} linked, {result["already"]} already present, '
                    f'{result["possible"]} possible matches skipped, {result["artwork_unverified"]} artwork-presence checks unavailable. {result["order"]}')
                if self.closing:self.root.destroy();return
            elif kind=='done_batch':
                self.set_running(False);self.report_button.config(state='normal')
                results=value['results'];library=results[0]['result'] if results else None
                linked=sum(x['result']['reused'] for x in results[1:])
                possible=sum(x['result']['possible'] for x in results)
                order_errors=[x['collection'] for x in results[1:]
                              if 'differs' in x['result']['order'] or 'could not' in x['result']['order']]
                prefix='Stopped. ' if value['stopped'] else 'Finished. '
                self.status.set(prefix+(f'{library["added"]} files copied; ' if library else '')+
                    f'{linked} playlist links added; {possible} possible-match skip events.'+
                    (' Some playlist order was not confirmed; see report.' if order_errors else ''))
                if self.closing:self.root.destroy();return
            elif kind=='import_error':
                self.set_running(False);self.report_button.config(state='normal')
                self.status.set(value)
                messagebox.showerror('Import stopped',value)
                if self.closing:self.root.destroy();return
        self.root.after(100,self.poll)

    def close(self):
        if self.importing:
            if not messagebox.askyesno('Import in progress','Stop after the current batch and close?'):
                return
            self.closing=True;self.stop();return
        self.root.destroy()


def main():
    parser=argparse.ArgumentParser(description='Import iPhone music in verified batches.')
    parser.add_argument('--batch-size',type=batch_size_argument,default=100,
                        help='maximum songs per sync batch (1–100; default 100)')
    parser.add_argument('--format-workers',type=worker_argument,default=16,help='local format-check workers (1–16; default 16)')
    parser.add_argument('--hash-workers',type=worker_argument,default=16,help='local file-hashing workers (1–16; default 16)')
    parser.add_argument('--metadata-workers',type=worker_argument,default=16,help='metadata preparation workers (1–16; default 16)')
    parser.add_argument('--readback-workers',type=lambda value:worker_argument(value,8),default=8,help='phone readback workers (1–8; default 8)')
    args=parser.parse_args()
    root=tk.Tk(); ImportWindow(root,args.batch_size,args.format_workers,args.hash_workers,args.metadata_workers,args.readback_workers); root.mainloop()


if __name__=='__main__':main()
