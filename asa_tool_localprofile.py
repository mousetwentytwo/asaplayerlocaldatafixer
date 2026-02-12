#!/usr/bin/env python
"""Unified CLI for ASA PlayerLocalData.arkprofile files.

Subcommands: extract, build, verify, gui (default).
"""
#
# Table of contents
# -----------------
# cmd_extract(args)                  .arkprofile → JSON
# cmd_build(args)                    JSON → .arkprofile
#
# Verify internals:
#   _read_ntstring(data, pos)        read UE4 length-prefixed string
#   _read_pair(data, pos, end)       read (name, type) property pair
#   _Verifier                        recursive property-size checker
#     .verify_properties()           walk properties between offsets
#     ._struct / ._array / ._map     per-type handlers
#     ._set / ._bool / ._simple
#   _find_property_start(data)       locate first property after header
#   _verify_file(path, verbose)      verify a single file
# cmd_verify(args)                   verify one or more files
#
# GUI:
# cmd_gui(args)                      launch Tk profile editor
#   _shorten(path)                   truncate long paths for display
#   _array_summary(entry)            one-line array description
#   App (tk.Tk)                      main application window
#     ._build_ui()                   construct menus, tabs, bindings
#     ._log / ._status               logging and status bar
#     ._load_pld / ._refresh_*       load profile, refresh tree/json/summary
#     ._open_profile / ._open_json   file open dialogs
#     ._save_json / ._save_json_as   save JSON to disk
#     ._build_profile                build .arkprofile from JSON
#     ._verify_profile / ._run_verify  run verification in background
#     ._apply_json / ._reformat_json JSON editor actions
#     ._clear_array                  clear an array property
#     ._clear_ark_items              clear ArkItems
#     ._clear_tamed_dinos            clear ArkTamedDinosData
#     ._on_close                     exit with unsaved-changes prompt
#
# main()                             argparse entry point
#

import argparse
import io
import json
import os
import struct
import subprocess
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'asaplayerlocaldatafixer'))
from asa import PlayerLocalData
from binary import BinaryStream


def cmd_extract(args):
    """Extract a .arkprofile to JSON."""
    if not os.path.isfile(args.input):
        print(f'Error: file not found: {args.input}', file=sys.stderr)
        return 1

    out_path = args.output or args.input + '.json'
    pld = PlayerLocalData(args.input)
    json_str = pld.to_json(indent=args.indent)

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(json_str)

    print(f'Extracted {os.path.basename(args.input)} -> {out_path}')
    return 0


def cmd_build(args):
    """Rebuild a .arkprofile from JSON."""
    if not os.path.isfile(args.input):
        print(f'Error: file not found: {args.input}', file=sys.stderr)
        return 1

    if args.output:
        out_path = args.output
    elif args.input.endswith('.arkprofile.json'):
        out_path = args.input[:-5]  # strip .json
    else:
        out_path = os.path.splitext(args.input)[0] + '.arkprofile'

    with open(args.input, 'r', encoding='utf-8') as f:
        json_str = f.read()

    pld = PlayerLocalData.from_json(json_str)
    pld.save(out_path)

    print(f'Built {out_path} from {os.path.basename(args.input)}')
    return 0


def _read_ntstring(data, pos):
    """Read a UE4 length-prefixed null-terminated string."""
    if pos + 4 > len(data):
        return (None, pos)
    slen = struct.unpack_from('<I', data, pos)[0]
    pos += 4
    if slen == 0:
        return ('', pos)
    s = data[pos:pos + slen - 1].decode('ascii', errors='replace')
    pos += slen
    return (s, pos)


def _read_pair(data, pos, end):
    """Read a (name, type) pair."""
    if pos + 4 > end:
        return (None, None, pos)
    name, npos = _read_ntstring(data, pos)
    if name is None:
        return (None, None, pos)
    if name == 'None':
        return (name, None, npos)
    if len(name) > 200 or not all(32 <= ord(c) < 127 for c in name):
        return (None, None, pos)
    if npos + 4 > end:
        return (None, None, pos)
    ptype, tpos = _read_ntstring(data, npos)
    if ptype is None:
        return (None, None, pos)
    valid_types = {
        'StructProperty', 'ArrayProperty', 'MapProperty', 'SetProperty',
        'BoolProperty', 'IntProperty', 'UInt32Property', 'FloatProperty',
        'DoubleProperty', 'Int64Property', 'UInt64Property', 'Int16Property',
        'UInt16Property', 'ByteProperty', 'StrProperty', 'NameProperty',
        'ObjectProperty', 'SoftObjectProperty',
    }
    if ptype not in valid_types:
        return (None, None, pos)
    return (name, ptype, tpos)


class _Verifier:
    def __init__(self, data, verbose=False):
        self.data = data
        self.verbose = verbose
        self.errors = []
        self.props_checked = 0

    def log(self, depth, msg):
        if self.verbose:
            print(f'{"  " * depth}{msg}')

    def error(self, depth, msg):
        line = f'{"  " * depth}ERROR: {msg}'
        self.errors.append(line)
        print(line, file=sys.stderr)

    def verify_properties(self, start, end, depth=0):
        pos = start
        while pos < end:
            name, ptype, new_pos = _read_pair(self.data, pos, end)
            if name is None:
                return pos
            pos = new_pos
            if name == 'None':
                break
            self.props_checked += 1
            handler = {
                'StructProperty': self._struct,
                'ArrayProperty': self._array,
                'MapProperty': self._map,
                'SetProperty': self._set,
                'BoolProperty': self._bool,
            }.get(ptype, self._simple)
            pos = handler(name, ptype, pos, depth)
        return pos

    # -- property handlers --------------------------------------------------

    def _struct(self, name, ptype, pos, depth):
        _f1 = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        struct_name, pos = _read_ntstring(self.data, pos)
        _f2 = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        package, pos = _read_ntstring(self.data, pos)
        idx = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        dsz = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        tag = self.data[pos]; pos += 1
        expected_end = pos + dsz
        self.log(depth, f'Struct {name} ({struct_name}) declared_size={dsz} [{pos}..{expected_end})')
        if expected_end > len(self.data):
            self.error(depth, f'{name} ({struct_name}): size {dsz} overflows file')
            return min(expected_end, len(self.data))
        self.verify_properties(pos, expected_end, depth + 1)
        return expected_end

    def _array(self, name, ptype, pos, depth):
        _f = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        child_type, pos = _read_ntstring(self.data, pos)
        if child_type == 'StructProperty':
            _f2 = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
            _sn, pos = _read_ntstring(self.data, pos)
            _f3 = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
            _pkg, pos = _read_ntstring(self.data, pos)
        idx = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        dsz = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        tag = self.data[pos]; pos += 1
        length = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        elem_data_size = dsz - 4
        expected_end = pos + elem_data_size
        self.log(depth, f'Array {name} [{child_type}] declared_size={dsz} length={length}')
        if expected_end > len(self.data):
            self.error(depth, f'{name}: size {dsz} overflows file')
            return min(expected_end, len(self.data))
        if dsz < 4:
            self.error(depth, f'{name}: size {dsz} < 4')
            return pos
        if child_type == 'StructProperty' and length > 0:
            ep = pos
            for i in range(length):
                if i > 0 and ep + 4 <= expected_end:
                    if struct.unpack_from('<I', self.data, ep)[0] == 0:
                        ep += 4
                ep = self.verify_properties(ep, expected_end, depth + 1)
                if ep > expected_end:
                    self.error(depth, f'{name}[{i}]: overran array boundary')
                    break
        else:
            type_sizes = {
                'IntProperty': 4, 'UInt32Property': 4, 'FloatProperty': 4,
                'DoubleProperty': 8, 'Int64Property': 8, 'UInt64Property': 8,
                'Int16Property': 2, 'UInt16Property': 2, 'ByteProperty': 1,
                'BoolProperty': 1,
            }
            if child_type in type_sizes and length > 0:
                expected_bytes = length * type_sizes[child_type]
                if expected_bytes != elem_data_size:
                    self.error(depth, f'{name}: {length}×{child_type} = {expected_bytes} bytes, declared {elem_data_size}')
        return expected_end

    def _map(self, name, ptype, pos, depth):
        _fk = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        key_type, pos = _read_ntstring(self.data, pos)
        val_type, pos = _read_ntstring(self.data, pos)
        idx = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        dsz = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        tag = self.data[pos]; pos += 1
        expected_end = pos + dsz
        self.log(depth, f'Map {name} [{key_type}->{val_type}] size={dsz}')
        if expected_end > len(self.data):
            self.error(depth, f'{name}: size {dsz} overflows file')
            return min(expected_end, len(self.data))
        return expected_end

    def _set(self, name, ptype, pos, depth):
        _f = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        elem_type, pos = _read_ntstring(self.data, pos)
        idx = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        dsz = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        tag = self.data[pos]; pos += 1
        expected_end = pos + dsz
        self.log(depth, f'Set {name} [{elem_type}] size={dsz}')
        if expected_end > len(self.data):
            self.error(depth, f'{name}: size {dsz} overflows file')
            return min(expected_end, len(self.data))
        return expected_end

    def _bool(self, name, ptype, pos, depth):
        idx = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        _sz = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        val = self.data[pos]; pos += 1
        if _sz != 0:
            self.error(depth, f'BoolProperty {name}: size should be 0, got {_sz}')
        self.log(depth, f'Bool {name} = {val}')
        return pos

    def _simple(self, name, ptype, pos, depth):
        idx = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        dsz = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        tag = self.data[pos]; pos += 1
        if tag:
            _extra = struct.unpack_from('<i', self.data, pos)[0]; pos += 4
        expected_end = pos + dsz
        self.log(depth, f'{ptype} {name} size={dsz}')
        if dsz < 0:
            self.error(depth, f'{name} ({ptype}): negative size {dsz}')
            return pos
        if expected_end > len(self.data):
            self.error(depth, f'{name} ({ptype}): size {dsz} overflows file')
            return min(expected_end, len(self.data))
        expected_sizes = {
            'IntProperty': 4, 'UInt32Property': 4, 'FloatProperty': 4,
            'DoubleProperty': 8, 'Int64Property': 8, 'UInt64Property': 8,
            'Int16Property': 2, 'UInt16Property': 2,
        }
        if ptype in expected_sizes and dsz != expected_sizes[ptype]:
            self.error(depth, f'{name} ({ptype}): expected size {expected_sizes[ptype]}, got {dsz}')
        return expected_end


def _find_property_start(data):
    """Replay header reading to find where properties begin."""
    pos = 12  # header_v1, v2, v3
    version = struct.unpack_from('<i', data, pos)[0]; pos += 4
    pos += 16  # guid
    _, pos = _read_ntstring(data, pos)  # file_type
    pos += 8   # two int32s
    name, pos = _read_ntstring(data, pos)
    _, pos = _read_ntstring(data, pos)  # controller
    _, pos = _read_ntstring(data, pos)  # game_mode
    _, pos = _read_ntstring(data, pos)  # map_name
    _, pos = _read_ntstring(data, pos)  # map_path
    pos += 12  # zeros
    pos += 4   # header_size
    pos += 4   # always 0
    pos += 1   # ASA extra separator
    return pos, name


def _verify_file(path, verbose=False):
    with open(path, 'rb') as f:
        data = f.read()

    print(f'File: {path} ({len(data):,} bytes)')

    if len(data) < 50:
        print('ERROR: file too short for header', file=sys.stderr)
        return False

    save_version = struct.unpack_from('<i', data, 0)[0]
    header_size = struct.unpack_from('<i', data, 4)[0]
    print(f'  Header: save_version={save_version}, header_size={header_size}')

    prop_start, name = _find_property_start(data)
    print(f'  Name: {name}')
    print(f'  Properties start at byte {prop_start}')
    v = _Verifier(data, verbose=verbose)
    end_pos = v.verify_properties(prop_start, len(data))

    remaining = len(data) - end_pos
    if remaining == 20:
        trailer_int = struct.unpack_from('<i', data, end_pos)[0]
        guid = data[end_pos + 4:end_pos + 20].hex()
        print(f'  Trailer: int={trailer_int}, GUID={guid}')
    elif remaining > 0:
        v.error(0, f'Unexpected trailing data: {remaining} bytes at offset {end_pos}')

    print(f'\n  Properties checked: {v.props_checked}')
    if v.errors:
        print(f'  ERRORS: {len(v.errors)}')
        for e in v.errors:
            print(f'    {e}')
        return False
    else:
        print('  All sizes OK')
        return True


def cmd_verify(args):
    """Verify property sizes in .arkprofile file(s)."""
    all_ok = True
    for path in args.input:
        if not os.path.isfile(path):
            print(f'Error: file not found: {path}', file=sys.stderr)
            all_ok = False
            continue
        ok = _verify_file(path, verbose=args.verbose)
        if not ok:
            all_ok = False
        print()
    return 0 if all_ok else 1


def cmd_gui(args):
    """Launch the graphical profile editor."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    def _shorten(path, max_len=60):
        return path if len(path) <= max_len else '...' + path[-(max_len - 3):]

    def _array_summary(entry):
        if not isinstance(entry, dict):
            return str(entry)[:80]
        length = entry.get('length', len(entry.get('value', [])))
        child = entry.get('_child_type', '?')
        return f'{length} × {child}'

    # -- application --------------------------------------------------------

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title('ARK Profile Editor')
            self.geometry('960x640')
            self.minsize(720, 480)
            self.pld = None
            self.json_path = None
            self.dirty = False
            self._build_ui()

        def _build_ui(self):
            menubar = tk.Menu(self)
            file_menu = tk.Menu(menubar, tearoff=0)
            file_menu.add_command(label='Open .arkprofile…', command=self._open_profile,
                                  accelerator='Ctrl+O')
            file_menu.add_command(label='Open JSON…', command=self._open_json)
            file_menu.add_separator()
            file_menu.add_command(label='Save JSON', command=self._save_json,
                                  accelerator='Ctrl+S')
            file_menu.add_command(label='Save JSON As…', command=self._save_json_as)
            file_menu.add_separator()
            file_menu.add_command(label='Build .arkprofile…', command=self._build_profile,
                                  accelerator='Ctrl+B')
            file_menu.add_command(label='Verify .arkprofile…', command=self._verify_profile)
            file_menu.add_separator()
            file_menu.add_command(label='Exit', command=self._on_close)
            menubar.add_cascade(label='File', menu=file_menu)

            tools_menu = tk.Menu(menubar, tearoff=0)
            tools_menu.add_command(label='Clear ArkItems', command=self._clear_ark_items)
            tools_menu.add_command(label='Clear Tamed Dinos', command=self._clear_tamed_dinos)
            menubar.add_cascade(label='Tools', menu=tools_menu)
            self.config(menu=menubar)

            self.bind_all('<Control-o>', lambda e: self._open_profile())
            self.bind_all('<Control-s>', lambda e: self._save_json())
            self.bind_all('<Control-b>', lambda e: self._build_profile())

            info_frame = ttk.Frame(self, padding=6)
            info_frame.pack(side=tk.TOP, fill=tk.X)
            self.lbl_file = ttk.Label(info_frame, text='No file loaded', anchor='w')
            self.lbl_file.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.lbl_status = ttk.Label(info_frame, text='', anchor='e', foreground='gray')
            self.lbl_status.pack(side=tk.RIGHT)

            summary_frame = ttk.LabelFrame(self, text='Profile Summary', padding=6)
            summary_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 4))
            cols = ttk.Frame(summary_frame)
            cols.pack(fill=tk.X)
            self._summary_labels = {}
            fields = [
                ('Name', 'name'), ('Map', 'map'), ('Items', 'items'),
                ('Dinos', 'dinos'), ('Achievements', 'achievements'),
            ]
            for i, (label, key) in enumerate(fields):
                ttk.Label(cols, text=f'{label}:', font=('Segoe UI', 9, 'bold')).grid(
                    row=i // 3, column=(i % 3) * 2, sticky='e', padx=(8, 2), pady=1)
                lbl = ttk.Label(cols, text='–')
                lbl.grid(row=i // 3, column=(i % 3) * 2 + 1, sticky='w', padx=(0, 16), pady=1)
                self._summary_labels[key] = lbl

            self.notebook = ttk.Notebook(self)
            self.notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)

            # Tree tab
            tree_frame = ttk.Frame(self.notebook)
            self.notebook.add(tree_frame, text='  Tree  ')
            tree_scroll_y = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL)
            tree_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
            self.tree = ttk.Treeview(tree_frame, columns=('type', 'value'),
                                     show='tree headings', yscrollcommand=tree_scroll_y.set)
            self.tree.heading('#0', text='Property', anchor='w')
            self.tree.heading('type', text='Type', anchor='w')
            self.tree.heading('value', text='Value', anchor='w')
            self.tree.column('#0', width=280, minwidth=150)
            self.tree.column('type', width=140, minwidth=80)
            self.tree.column('value', width=400, minwidth=100)
            tree_scroll_y.config(command=self.tree.yview)
            self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            # JSON tab
            json_frame = ttk.Frame(self.notebook)
            self.notebook.add(json_frame, text='  JSON  ')
            json_toolbar = ttk.Frame(json_frame, padding=(0, 2))
            json_toolbar.pack(side=tk.TOP, fill=tk.X)
            ttk.Button(json_toolbar, text='Apply Changes', command=self._apply_json).pack(
                side=tk.LEFT, padx=4)
            ttk.Button(json_toolbar, text='Reformat', command=self._reformat_json).pack(
                side=tk.LEFT, padx=4)
            self.lbl_json_status = ttk.Label(json_toolbar, text='', foreground='gray')
            self.lbl_json_status.pack(side=tk.RIGHT, padx=4)
            json_scroll = ttk.Scrollbar(json_frame, orient=tk.VERTICAL)
            json_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.txt_json = tk.Text(json_frame, wrap=tk.NONE, undo=True,
                                    font=('Consolas', 10), yscrollcommand=json_scroll.set)
            json_scroll.config(command=self.txt_json.yview)
            self.txt_json.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.txt_json.bind('<<Modified>>', self._on_json_modified)

            # Log tab
            log_frame = ttk.Frame(self.notebook)
            self.notebook.add(log_frame, text='  Log  ')
            log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL)
            log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.txt_log = tk.Text(log_frame, wrap=tk.WORD, state=tk.DISABLED,
                                   font=('Consolas', 9), yscrollcommand=log_scroll.set)
            log_scroll.config(command=self.txt_log.yview)
            self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            bottom = ttk.Frame(self, padding=4)
            bottom.pack(side=tk.BOTTOM, fill=tk.X)
            ttk.Button(bottom, text='Open .arkprofile', command=self._open_profile).pack(
                side=tk.LEFT, padx=4)
            ttk.Button(bottom, text='Build .arkprofile', command=self._build_profile).pack(
                side=tk.LEFT, padx=4)
            ttk.Button(bottom, text='Verify .arkprofile', command=self._verify_profile).pack(
                side=tk.LEFT, padx=4)
            self.protocol('WM_DELETE_WINDOW', self._on_close)

        def _log(self, msg):
            self.txt_log.config(state=tk.NORMAL)
            self.txt_log.insert(tk.END, msg + '\n')
            self.txt_log.see(tk.END)
            self.txt_log.config(state=tk.DISABLED)

        def _status(self, msg):
            self.lbl_status.config(text=msg)
            self.after(5000, lambda: self.lbl_status.config(text=''))

        def _load_pld(self, pld, source_label):
            self.pld = pld
            self.lbl_file.config(text=source_label)
            self._refresh_summary()
            self._refresh_tree()
            self._refresh_json()
            self._log(f'Loaded: {source_label}')

        def _refresh_summary(self):
            pld = self.pld
            if pld is None:
                for v in self._summary_labels.values():
                    v.config(text='–')
                return
            self._summary_labels['name'].config(text=pld.name or '–')
            self._summary_labels['map'].config(text=pld.map_name or '–')
            self._summary_labels['items'].config(text=str(len(pld.ark_items)))
            self._summary_labels['dinos'].config(text=str(len(pld.tamed_dinos)))
            self._summary_labels['achievements'].config(text=str(len(pld.achievements)))

        def _refresh_tree(self):
            self.tree.delete(*self.tree.get_children())
            if self.pld is None:
                return
            self._insert_tree_node('', 'header', self._header_dict(), 'header')
            self._insert_tree_node('', 'data', self.pld.data, 'data')

        def _header_dict(self):
            pld = self.pld
            return {
                'name': pld.name, 'version': pld.version,
                'file_type': pld.file_type, 'controller': pld.controller,
                'game_mode': pld.game_mode, 'map_name': pld.map_name,
                'map_path': pld.map_path, 'header_size': pld.header_size,
            }

        def _insert_tree_node(self, parent, key, value, node_id=None):
            kw = {'iid': node_id} if node_id else {}
            if isinstance(value, dict):
                ptype = value.get('_type', 'dict')
                if 'value' in value and '_type' in value:
                    display = self._value_preview(value)
                else:
                    display = f'{{{len(value)} keys}}'
                n = self.tree.insert(parent, tk.END, text=key,
                                     values=(ptype, display), open=(parent == ''), **kw)
                for k, v in value.items():
                    if k.startswith('_'):
                        self.tree.insert(n, tk.END, text=k, values=('meta', str(v)[:120]))
                    else:
                        self._insert_tree_node(n, k, v)
            elif isinstance(value, list):
                n = self.tree.insert(parent, tk.END, text=key,
                                     values=('list', f'[{len(value)} items]'), **kw)
                for i, v in enumerate(value[:200]):
                    self._insert_tree_node(n, f'[{i}]', v)
                if len(value) > 200:
                    self.tree.insert(n, tk.END, text=f'... +{len(value) - 200} more',
                                     values=('', ''))
            else:
                self.tree.insert(parent, tk.END, text=key,
                                 values=(type(value).__name__, str(value)[:200]), **kw)

        def _value_preview(self, entry):
            ptype = entry.get('_type', '')
            val = entry.get('value')
            if ptype == 'ArrayProperty':
                return _array_summary(entry)
            if isinstance(val, (int, float, str, bool)):
                return str(val)[:120]
            if isinstance(val, dict):
                return f'{{{len(val)} keys}}'
            if isinstance(val, list):
                return f'[{len(val)} items]'
            return str(val)[:80]

        def _refresh_json(self):
            if self.pld is None:
                return
            self.txt_json.delete('1.0', tk.END)
            self.txt_json.insert('1.0', self.pld.to_json(indent=2))
            self.txt_json.edit_modified(False)
            self.dirty = False
            self.lbl_json_status.config(text='')

        def _open_profile(self):
            path = filedialog.askopenfilename(
                title='Open .arkprofile',
                filetypes=[('ARK Profile', '*.arkprofile'), ('All files', '*.*')])
            if not path:
                return
            try:
                pld = PlayerLocalData(path)
            except Exception as e:
                messagebox.showerror('Error', f'Failed to load:\n{e}')
                self._log(f'ERROR: {e}')
                return
            self.json_path = path + '.json'
            self._load_pld(pld, _shorten(path))
            self._status('Loaded')

        def _open_json(self):
            path = filedialog.askopenfilename(
                title='Open JSON',
                filetypes=[('JSON', '*.json'), ('All files', '*.*')])
            if not path:
                return
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    text = f.read()
                pld = PlayerLocalData.from_json(text)
            except Exception as e:
                messagebox.showerror('Error', f'Failed to load JSON:\n{e}')
                self._log(f'ERROR: {e}')
                return
            self.json_path = path
            self._load_pld(pld, _shorten(path))
            self._status('Loaded JSON')

        def _save_json(self):
            if self.pld is None:
                messagebox.showwarning('No data', 'Load a profile first.')
                return
            if self.json_path is None:
                self._save_json_as()
                return
            self._do_save_json(self.json_path)

        def _save_json_as(self):
            if self.pld is None:
                messagebox.showwarning('No data', 'Load a profile first.')
                return
            path = filedialog.asksaveasfilename(
                title='Save JSON As',
                defaultextension='.json',
                filetypes=[('JSON', '*.json'), ('All files', '*.*')])
            if not path:
                return
            self.json_path = path
            self._do_save_json(path)

        def _do_save_json(self, path):
            text = self.txt_json.get('1.0', tk.END).strip()
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
            except Exception as e:
                messagebox.showerror('Error', f'Failed to save:\n{e}')
                return
            self.dirty = False
            self.txt_json.edit_modified(False)
            self.lbl_json_status.config(text='')
            self._log(f'Saved JSON: {path}')
            self._status('Saved')

        def _build_profile(self):
            if self.pld is None:
                messagebox.showwarning('No data', 'Load a profile first.')
                return
            path = filedialog.asksaveasfilename(
                title='Save .arkprofile',
                defaultextension='.arkprofile',
                filetypes=[('ARK Profile', '*.arkprofile'), ('All files', '*.*')])
            if not path:
                return
            text = self.txt_json.get('1.0', tk.END).strip()
            try:
                pld = PlayerLocalData.from_json(text)
                pld.save(path)
            except Exception as e:
                messagebox.showerror('Error', f'Build failed:\n{e}')
                self._log(f'BUILD ERROR: {e}')
                return
            self._log(f'Built: {path}')
            self._status('Built .arkprofile')
            self._run_verify(path)

        def _verify_profile(self):
            path = filedialog.askopenfilename(
                title='Verify .arkprofile',
                filetypes=[('ARK Profile', '*.arkprofile'), ('All files', '*.*')])
            if not path:
                return
            self._run_verify(path)

        def _run_verify(self, path):
            self.notebook.select(2)  # Log tab
            script = os.path.abspath(__file__)
            python = sys.executable

            def _worker():
                try:
                    result = subprocess.run(
                        [python, script, 'verify', path],
                        capture_output=True, text=True, timeout=30)
                    output = result.stdout + result.stderr
                except Exception as e:
                    output = f'Verification error: {e}'
                self.after(0, lambda: self._log(output.strip()))

            threading.Thread(target=_worker, daemon=True).start()

        def _on_json_modified(self, event=None):
            if self.txt_json.edit_modified():
                self.dirty = True
                self.lbl_json_status.config(text='unsaved changes', foreground='orange')

        def _apply_json(self):
            text = self.txt_json.get('1.0', tk.END).strip()
            if not text:
                return
            try:
                pld = PlayerLocalData.from_json(text)
            except Exception as e:
                messagebox.showerror('Invalid JSON', f'Failed to parse:\n{e}')
                self._log(f'JSON parse error: {e}')
                return
            self.pld = pld
            self._refresh_summary()
            self._refresh_tree()
            self.dirty = False
            self.txt_json.edit_modified(False)
            self.lbl_json_status.config(text='applied', foreground='green')
            self._log('Applied JSON edits')
            self._status('Applied')

        def _reformat_json(self):
            text = self.txt_json.get('1.0', tk.END).strip()
            if not text:
                return
            try:
                d = json.loads(text)
                formatted = json.dumps(d, indent=2, ensure_ascii=False)
            except Exception as e:
                messagebox.showerror('Invalid JSON', str(e))
                return
            self.txt_json.delete('1.0', tk.END)
            self.txt_json.insert('1.0', formatted)
            self.txt_json.edit_modified(False)

        def _clear_array(self, key_path, label):
            if self.pld is None:
                messagebox.showwarning('No data', 'Load a profile first.')
                return
            text = self.txt_json.get('1.0', tk.END).strip()
            try:
                d = json.loads(text)
            except Exception as e:
                messagebox.showerror('Invalid JSON', str(e))
                return
            obj = d
            keys = key_path.split('.')
            for k in keys[:-1]:
                obj = obj[k]
            target = obj[keys[-1]]
            old_len = target.get('length', len(target.get('value', [])))
            target['value'] = []
            target['length'] = 0
            target['_size'] = 4
            text = json.dumps(d, indent=2, ensure_ascii=False)
            self.txt_json.delete('1.0', tk.END)
            self.txt_json.insert('1.0', text)
            self.txt_json.edit_modified(True)
            self.dirty = True
            try:
                self.pld = PlayerLocalData.from_json(text)
                self._refresh_summary()
                self._refresh_tree()
            except Exception:
                pass
            self._log(f'Cleared {label}: {old_len} → 0')
            self._status(f'Cleared {label}')
            self.lbl_json_status.config(text='unsaved changes', foreground='orange')

        def _clear_ark_items(self):
            self._clear_array('data.MyArkData.data.ArkItems', 'ArkItems')

        def _clear_tamed_dinos(self):
            self._clear_array('data.MyArkData.data.ArkTamedDinosData', 'Tamed Dinos')

        def _on_close(self):
            if self.dirty:
                r = messagebox.askyesnocancel('Unsaved changes',
                                              'You have unsaved JSON edits.\nSave before exiting?')
                if r is None:
                    return
                if r:
                    self._save_json()
            self.destroy()

    app = App()
    app.mainloop()
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog='asa_tool_localprofile',
        description='Unified tool for ASA PlayerLocalData.arkprofile files.')
    sub = parser.add_subparsers(dest='command')

    # extract
    p_ext = sub.add_parser('extract', help='.arkprofile → JSON')
    p_ext.add_argument('input', help='Path to the .arkprofile file')
    p_ext.add_argument('-o', '--output', default=None, help='Output JSON path')
    p_ext.add_argument('--indent', type=int, default=2, help='JSON indent (default: 2)')

    # build
    p_bld = sub.add_parser('build', help='JSON → .arkprofile')
    p_bld.add_argument('input', help='Path to the JSON file')
    p_bld.add_argument('-o', '--output', default=None, help='Output .arkprofile path')

    # verify
    p_ver = sub.add_parser('verify', help='Validate property sizes')
    p_ver.add_argument('input', nargs='+', help='.arkprofile file(s)')
    p_ver.add_argument('-v', '--verbose', action='store_true', help='Verbose output')

    # gui
    sub.add_parser('gui', help='Launch graphical editor')

    args = parser.parse_args()
    if args.command is None:
        args.command = 'gui'

    dispatch = {
        'extract': cmd_extract,
        'build': cmd_build,
        'verify': cmd_verify,
        'gui': cmd_gui,
    }
    return dispatch[args.command](args)


if __name__ == '__main__':
    sys.exit(main() or 0)
