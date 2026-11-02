#!/usr/bin/env python3
"""Small native Windows interface for the iPhone music exporter."""
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
PYTHON = APP_DIR / '.venv' / 'Scripts' / 'python.exe'
EXPORTER = APP_DIR / 'rebuild_music.py'
MARKER = '.iphone_music_export.json'


def parse_progress(line):
    if not line.startswith('PROGRESS '):
        return None
    try:
        data = json.loads(line[len('PROGRESS '):])
        done, failed, total = (int(data[key]) for key in ('completed', 'failed', 'total'))
        if min(done, failed, total) < 0 or done + failed > total:
            return None
        return done, failed, total
    except (ValueError, TypeError, KeyError):
        return None


def cover_status(output):
    reports = sorted(output.glob('_rebuild_report*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return None
    report = json.loads(reports[0].read_text(encoding='utf-8'))
    return sum(x['artwork'] not in ('phone original', 'original embedded cover retained')
               and (not x.get('files') or any((output / name).is_file() for name in x['files']))
               for x in report['tracks'])


class MusicExportWindow:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.log_path = None
        self.open_path = None
        self.current_watch = False
        self.summary_shown = False
        self.messages = queue.Queue()
        self.root.title('iPhone Music Export')
        self.root.minsize(610, 410)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

        default_existing = APP_DIR / 'MusicExport_Faithful'
        if not default_existing.exists():
            default_existing = APP_DIR / 'MusicExport'
        self.device = tk.StringVar()
        self.output = tk.StringVar(value=str(APP_DIR / ('MusicExport_' + datetime.now().strftime('%Y%m%d_%H%M%S'))))
        self.existing = tk.StringVar(value=str(default_existing) if default_existing.exists() else '')
        self.protected_name = tk.StringVar()
        self.jobs = tk.StringVar(value='4')
        self.status = tk.StringVar(value='Select your iPhone and choose where to save the music.')
        self.counts = tk.StringVar(value='0 completed · 0 failed')

        body = ttk.Frame(root, padding=18)
        body.grid(sticky='nsew')
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(7, weight=1)

        ttk.Label(body, text='iPhone Music Export', font=('Segoe UI', 17, 'bold')).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 14))
        ttk.Label(body, text='iPhone').grid(row=1, column=0, sticky='w', padx=(0, 12), pady=4)
        self.devices = ttk.Combobox(body, textvariable=self.device, state='readonly')
        self.devices.grid(row=1, column=1, sticky='ew', pady=4)
        self.refresh_button = ttk.Button(body, text='Refresh', command=self.refresh)
        self.refresh_button.grid(row=1, column=2, padx=(8, 0), pady=4)

        ttk.Label(body, text='Save to').grid(row=2, column=0, sticky='w', padx=(0, 12), pady=4)
        self.output_entry = ttk.Entry(body, textvariable=self.output)
        self.output_entry.grid(row=2, column=1, sticky='ew', pady=4)
        ttk.Button(body, text='Browse', command=self.browse_output).grid(row=2, column=2, padx=(8, 0), pady=4)

        ttk.Label(body, text='Keep additions').grid(row=3, column=0, sticky='w', padx=(0, 12), pady=4)
        self.existing_entry = ttk.Entry(body, textvariable=self.existing)
        self.existing_entry.grid(row=3, column=1, sticky='ew', pady=4)
        ttk.Button(body, text='Browse', command=self.browse_existing).grid(row=3, column=2, padx=(8, 0), pady=4)
        ttk.Label(body, text='Playlist to keep').grid(row=4, column=0, sticky='w')
        ttk.Entry(body, textvariable=self.protected_name).grid(row=4, column=1, sticky='ew')

        ttk.Label(body, text='Transfers').grid(row=5, column=0, sticky='w', pady=6)
        ttk.Spinbox(body, from_=1, to=8, width=4, textvariable=self.jobs).grid(row=5, column=1, sticky='w')
        self.progress = ttk.Progressbar(body, mode='determinate', maximum=100)
        self.progress.grid(row=6, column=0, columnspan=3, sticky='ew', pady=(14, 5))
        ttk.Label(body, textvariable=self.counts).grid(row=7, column=0, columnspan=3, sticky='nw')
        ttk.Label(body, textvariable=self.status, wraplength=550).grid(row=8, column=0, columnspan=3, sticky='w', pady=(8, 10))
        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=3, sticky='ew')
        self.start_button = ttk.Button(buttons, text='Start export', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(buttons, text='Stop', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=8)
        self.open_button = ttk.Button(buttons, text='Open folder', command=self.open_folder, state='disabled')
        self.open_button.pack(side='right')
        root.after(100, self.poll)
        self.refresh()

    def browse_output(self):
        parent = filedialog.askdirectory(title='Choose a folder for the new export', mustexist=True)
        if parent:
            self.output.set(str(Path(parent) / ('MusicExport_' + datetime.now().strftime('%Y%m%d_%H%M%S'))))

    def browse_existing(self):
        folder = filedialog.askdirectory(title='Choose an existing export to preserve its added playlist files', mustexist=True)
        if folder:
            self.existing.set(folder)

    def refresh(self):
        self.refresh_button.config(state='disabled')
        self.status.set('Looking for USB-connected iPhones…')
        def worker():
            try:
                from pymobiledevice3.usbmux import list_devices
                devices = asyncio.run(list_devices())
                self.messages.put(('devices', sorted({d.serial for d in devices if d.is_usb})))
            except Exception as exc:
                self.messages.put(('device_error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def start(self):
        serial = self.device.get().strip()
        if not serial:
            messagebox.showerror('Choose a phone', 'Select the iPhone you want to export.')
            return
        try:
            jobs = int(self.jobs.get())
            if not 1 <= jobs <= 8:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Transfers', 'Choose a number from 1 to 8.')
            return
        output = Path(self.output.get().strip()).expanduser().resolve()
        original = Path(self.existing.get().strip()).expanduser().resolve() if self.existing.get().strip() else APP_DIR / '__no_existing_export__'
        protected = self.protected_name.get().strip()
        if protected and (not self.existing.get().strip() or Path(protected).name != protected or protected in ('.', '..')):
            messagebox.showerror('Choose existing export', 'Choose an existing export and enter a valid playlist folder name.')
            return
        if original == output or original in output.parents or output in original.parents:
            messagebox.showerror('Choose a separate folder', 'The new export must be outside the existing export.')
            return
        if not PYTHON.is_file() or not EXPORTER.is_file():
            messagebox.showerror('Setup incomplete', 'The exporter or its Python environment is missing.')
            return
        try:
            if output.exists():
                marker = output / MARKER
                if not marker.is_file():
                    raise ValueError('Choose a new folder, or a previous export created by this window.')
                info = json.loads(marker.read_text(encoding='utf-8'))
                if (info.get('serial') != serial or info.get('original') != str(original)
                        or info.get('protected', '') != protected):
                    raise ValueError('This existing export belongs to a different phone or source folder.')
            else:
                output.mkdir(parents=True)
                (output / MARKER).write_text(json.dumps({'serial': serial, 'original': str(original), 'protected': protected}), encoding='utf-8')
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror('Cannot use this folder', str(exc))
            return
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        cmd = [str(PYTHON), '-u', str(EXPORTER), '--serial', serial, '--original', str(original),
               '--out', str(output), '--jobs', str(jobs), '--progress-json']
        if protected:
            cmd += ['--protected-playlist', protected]
        try:
            self.log_path = output / '_ui_export.log'
            self.open_path = output
            self.summary_shown = False
            self.process = subprocess.Popen(cmd, cwd=str(APP_DIR), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                bufsize=1, env=env, creationflags=flags)
        except OSError as exc:
            messagebox.showerror('Could not start', str(exc))
            return
        self.start_button.config(state='disabled')
        self.refresh_button.config(state='disabled')
        self.stop_button.config(state='normal')
        self.open_button.config(state='disabled')
        self.progress.config(mode='indeterminate')
        self.progress.start(12)
        self.counts.set('Connecting…')
        self.status.set('Reading the phone’s music library. Keep the phone connected.')
        threading.Thread(target=self.read_output, args=(self.process,), daemon=True).start()

    def read_output(self, process):
        with self.log_path.open('a', encoding='utf-8') as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                self.messages.put(('line', line.rstrip()))
        self.messages.put(('exit', process.wait()))

    def stop(self):
        if self.process and self.process.poll() is None:
            self.status.set('Stopping… Files already copied will remain in the export folder.')
            self.stop_button.config(state='disabled')
            pid = self.process.pid
            def terminate():
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            threading.Thread(target=terminate).start()

    def open_folder(self):
        path = self.open_path or Path(self.output.get())
        if path.exists():
            os.startfile(path)

    def watch_current_metadata_job(self):
        """Show the separately started date-preservation job if it is still active."""
        output = APP_DIR / 'MusicExport_Faithful'
        report = output / '_rebuild_report.json'
        finished = output / '_metadata_report.json'
        snapshot = APP_DIR.parent.parent / 'work' / 'phone_medialibrary_snapshot.sqlite'
        if not report.is_file() or not snapshot.is_file():
            return
        if finished.is_file():
            try:
                result = json.loads(finished.read_text(encoding='utf-8'))
                total = len(result['updated']) + len(result['failures'])
                self.messages.put(('current_begin', (output, total)))
                self.messages.put(('current_end', (len(result['updated']), len(result['failures']))))
            except (OSError, ValueError, KeyError):
                pass
            return
        def active():
            import psutil
            for process in psutil.process_iter(['cmdline']):
                try:
                    command = ' '.join(process.info.get('cmdline') or [])
                    if 'complete_metadata.py' in command and 'MusicExport_Faithful' in command:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return False
        def worker():
            try:
                if not active():
                    return
                from complete_metadata import read_metadata
                metadata = read_metadata(snapshot)
                records = json.loads(report.read_text(encoding='utf-8'))['tracks']
                pending = {}
                for record in records:
                    value = metadata.get(record['id'], {}).get('date_added')
                    if value and record['files']:
                        pending[str(output / record['files'][0])] = float(value) + 978307200
                total = len(records)
                self.messages.put(('current_begin', (output, total)))
                while True:
                    for name, expected in list(pending.items()):
                        try:
                            if abs(Path(name).stat().st_mtime - expected) < 1:
                                pending.pop(name)
                        except OSError:
                            pass
                    self.messages.put(('current_progress', (total - len(pending), total)))
                    if finished.is_file():
                        try:
                            result = json.loads(finished.read_text(encoding='utf-8'))
                        except (OSError, json.JSONDecodeError):
                            time.sleep(1)
                            continue
                        self.messages.put(('current_end', (len(result['updated']), len(result['failures']))))
                        return
                    if not active():
                        self.messages.put(('current_error', 'The date update stopped before its report was written.'))
                        return
                    time.sleep(8)
            except Exception as exc:
                self.messages.put(('current_error', 'Could not monitor the current update: ' + str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'devices':
                previous = self.device.get()
                self.devices['values'] = value
                self.device.set(previous if previous in value else (value[0] if len(value) == 1 else ''))
                self.refresh_button.config(state='normal')
                if not self.current_watch and not self.summary_shown:
                    self.status.set('Select the iPhone to export.' if value else 'No iPhone detected over USB. Connect and unlock the phone, then select Refresh.')
            elif kind == 'device_error':
                self.refresh_button.config(state='normal')
                if not self.current_watch and not self.summary_shown:
                    self.status.set('Could not list devices: ' + value)
            elif kind == 'current_begin':
                self.current_watch = True
                self.open_path = value[0]
                self.start_button.config(state='disabled')
                self.open_button.config(state='normal')
                self.progress.config(mode='determinate', maximum=max(value[1], 1), value=0)
                self.status.set('Current rebuild: preserving the phone’s original dates and metadata…')
            elif kind == 'current_progress':
                done, total = value
                self.progress.config(value=done, maximum=max(total, 1))
                self.counts.set(f'{done:,} completed of {total:,}')
            elif kind == 'current_end':
                done, failed = value
                self.current_watch = False
                self.summary_shown = True
                self.start_button.config(state='normal')
                self.progress.config(value=done + failed)
                self.counts.set(f'{done:,} completed · {failed:,} failed')
                try:
                    missing = cover_status(self.open_path)
                except (OSError, ValueError, KeyError):
                    missing = None
                if failed:
                    self.status.set('Current rebuild finished with errors; see the metadata report.')
                elif missing:
                    self.status.set(f'Rebuild complete; original artwork was unavailable for {missing:,} tracks.')
                else:
                    self.status.set('Current rebuild complete.')
            elif kind == 'current_error':
                self.current_watch = False
                self.start_button.config(state='normal')
                self.status.set(value)
            elif kind == 'line':
                progress = parse_progress(value)
                if progress:
                    done, failed, total = progress
                    self.progress.stop()
                    self.progress.config(mode='determinate', maximum=max(total, 1), value=done + failed)
                    self.counts.set(f'{done:,} completed of {total:,} · {failed:,} failed')
                    self.status.set('Copying music and the phone’s original artwork and dates…')
                elif value.startswith('Finished:'):
                    self.status.set(value)
                elif value.startswith('Traceback') or value.startswith('Error:'):
                    self.status.set(value)
            elif kind == 'exit':
                self.progress.stop()
                self.start_button.config(state='normal')
                self.refresh_button.config(state='normal')
                self.stop_button.config(state='disabled')
                self.open_button.config(state='normal')
                if value == 0:
                    try:
                        missing = cover_status(Path(self.output.get()))
                        if missing is None:
                            raise ValueError('no report')
                        self.status.set(f'Export complete. {missing:,} tracks had no verified local cover; see the report.')
                    except (OSError, ValueError, KeyError):
                        self.status.set('Export complete. Open the folder to see your music.')
                else:
                    self.status.set('Export stopped or had errors. Files already copied are in the folder; see _ui_export.log for details.')
                self.process = None
        self.root.after(100, self.poll)

    def close(self):
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno('Export running', 'Stop the export and close this window?'):
                return
            self.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    MusicExportWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
