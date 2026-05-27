# -*- coding: utf-8 -*-
import sqlite3
import os
import sys
import shutil
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import math
from datetime import datetime
from collections import defaultdict

import ttkbootstrap as tb
from ttkbootstrap.widgets.scrolled import ScrolledText

# --- Импорт pandapower ---
try:
    import pandapower as pp
    PANDAPOWER_AVAILABLE = True
except ImportError:
    PANDAPOWER_AVAILABLE = False
    print("ОШИБКА: pandapower не установлен. Установите: pip install pandapower")

# ---------- Настройка пути к БД ----------
def get_db_path():
    if sys.platform == "win32":
        base_dir = os.environ.get('APPDATA', '')
        app_dir = os.path.join(base_dir, "EmergencySimulator")
    else:
        base_dir = os.path.expanduser("~")
        app_dir = os.path.join(base_dir, ".emergency_simulator")
    os.makedirs(app_dir, exist_ok=True)
    return os.path.join(app_dir, "energy_monitor.db")

DB_PATH = get_db_path()

# ---------- Миграция БД ----------
def migrate_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = OFF;")
    cursor.execute("PRAGMA table_info(objects)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'city' not in columns:
        cursor.execute("ALTER TABLE objects ADD COLUMN city TEXT")
    if 'pos_x' not in columns:
        cursor.execute("ALTER TABLE objects ADD COLUMN pos_x REAL")
        cursor.execute("ALTER TABLE objects ADD COLUMN pos_y REAL")
    if 'base_kv' not in columns:
        cursor.execute("ALTER TABLE objects ADD COLUMN base_kv REAL")
    if 'r_ohm' not in columns:
        cursor.execute("ALTER TABLE objects ADD COLUMN r_ohm REAL")
        cursor.execute("ALTER TABLE objects ADD COLUMN x_ohm REAL")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS consumers (
            object_id INTEGER PRIMARY KEY,
            p_mw REAL,
            q_mvar REAL,
            FOREIGN KEY (object_id) REFERENCES objects(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='connections'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(connections)")
        conn_cols = [col[1] for col in cursor.fetchall()]
        if 'line_id' not in conn_cols:
            cursor.execute("DROP TABLE connections")
            cursor.execute("""
                CREATE TABLE connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_id INTEGER NOT NULL,
                    from_obj_id INTEGER NOT NULL,
                    to_obj_id INTEGER NOT NULL,
                    offset REAL DEFAULT 0,
                    length_km REAL,
                    r_ohm_per_km REAL,
                    x_ohm_per_km REAL,
                    c_nf_per_km REAL,
                    max_i_ka REAL,
                    std_type TEXT,
                    FOREIGN KEY (line_id) REFERENCES objects(id) ON DELETE CASCADE,
                    FOREIGN KEY (from_obj_id) REFERENCES objects(id) ON DELETE CASCADE,
                    FOREIGN KEY (to_obj_id) REFERENCES objects(id) ON DELETE CASCADE
                )
            """)
        else:
            needed_cols = ['length_km', 'r_ohm_per_km', 'x_ohm_per_km', 'c_nf_per_km', 'max_i_ka', 'std_type']
            for col in needed_cols:
                if col not in conn_cols:
                    cursor.execute(f"ALTER TABLE connections ADD COLUMN {col} REAL")
    conn.commit()
    cursor.execute("PRAGMA foreign_keys = ON;")
    conn.close()

# ---------- Инициализация БД ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS "objects" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT,
            "name" TEXT NOT NULL UNIQUE,
            "type" TEXT NOT NULL CHECK(type IN ('Линия', 'ГЭС', 'Подстанция', 'Потребитель')),
            "city" TEXT,
            "pos_x" REAL,
            "pos_y" REAL,
            "status" TEXT DEFAULT 'normal' CHECK(status IN ('normal', 'emergency')),
            "current_emergency" TEXT,
            "base_kv" REAL,
            "r_ohm" REAL,
            "x_ohm" REAL
        );
        CREATE TABLE IF NOT EXISTS "params" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT,
            "object_id" INTEGER NOT NULL,
            "param_type" TEXT NOT NULL CHECK(param_type IN ('voltage', 'current', 'power_flow', 'power_gen')),
            "nominal" REAL NOT NULL,
            "max_allowed" REAL,
            "min_allowed" REAL,
            "unit" TEXT,
            FOREIGN KEY ("object_id") REFERENCES "objects"("id") ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS "measurements" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT,
            "object_id" INTEGER NOT NULL,
            "param_type" TEXT NOT NULL,
            "value" REAL NOT NULL,
            "timestamp" TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY ("object_id") REFERENCES "objects"("id") ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS "emergency_log" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT,
            "object_id" INTEGER NOT NULL,
            "event_time" TEXT NOT NULL,
            "param_type" TEXT,
            "measured_value" REAL,
            "limit_value" REAL,
            "description" TEXT,
            FOREIGN KEY ("object_id") REFERENCES "objects"("id") ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS "connections" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT,
            "line_id" INTEGER NOT NULL,
            "from_obj_id" INTEGER NOT NULL,
            "to_obj_id" INTEGER NOT NULL,
            "offset" REAL DEFAULT 0,
            "length_km" REAL,
            "r_ohm_per_km" REAL,
            "x_ohm_per_km" REAL,
            "c_nf_per_km" REAL,
            "max_i_ka" REAL,
            "std_type" TEXT,
            FOREIGN KEY ("line_id") REFERENCES "objects"("id") ON DELETE CASCADE,
            FOREIGN KEY ("from_obj_id") REFERENCES "objects"("id") ON DELETE CASCADE,
            FOREIGN KEY ("to_obj_id") REFERENCES "objects"("id") ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_objects_name ON objects(name);
        CREATE INDEX IF NOT EXISTS idx_params_object ON params(object_id);
        CREATE INDEX IF NOT EXISTS idx_measurements_object ON measurements(object_id);
        CREATE INDEX IF NOT EXISTS idx_log_object ON emergency_log(object_id);
        CREATE INDEX IF NOT EXISTS idx_connections_line ON connections(line_id);
    """)
    conn.commit()
    conn.close()

# ---------- Кэш ----------
_cache = {
    'params': {},
    'measurements': {},
    'objects': []
}

def invalidate_cache():
    _cache['params'].clear()
    _cache['measurements'].clear()
    _cache['objects'] = []

def get_cached_objects():
    if not _cache['objects']:
        _cache['objects'] = get_objects_list()
    return _cache['objects']

def get_cached_params(obj_id):
    if obj_id not in _cache['params']:
        _cache['params'][obj_id] = get_params_for_object(obj_id)
    return _cache['params'][obj_id]

def get_cached_measurement(obj_id, param_type):
    key = (obj_id, param_type)
    if key not in _cache['measurements']:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM measurements WHERE object_id=? AND param_type=? ORDER BY timestamp DESC LIMIT 1", (obj_id, param_type))
        row = cursor.fetchone()
        conn.close()
        _cache['measurements'][key] = row[0] if row else None
    return _cache['measurements'][key]

def update_cached_measurement(obj_id, param_type, value):
    _cache['measurements'][(obj_id, param_type)] = value

# ---------- Работа со связями ----------
def get_all_connections():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            c.id, c.line_id, c.from_obj_id, c.to_obj_id, c.offset,
            c.length_km, c.r_ohm_per_km, c.x_ohm_per_km, c.c_nf_per_km, c.max_i_ka, c.std_type
        FROM connections c
        JOIN objects o1 ON c.from_obj_id = o1.id
        JOIN objects o2 ON c.to_obj_id = o2.id
        JOIN objects l ON c.line_id = l.id
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_connection(line_id, from_obj_id, to_obj_id, offset=0, length_km=None, r_ohm_per_km=None, x_ohm_per_km=None, c_nf_per_km=None, max_i_ka=None, std_type=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO connections
        (line_id, from_obj_id, to_obj_id, offset, length_km, r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka, std_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (line_id, from_obj_id, to_obj_id, offset, length_km, r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka, std_type))
    conn.commit()
    conn.close()
    invalidate_cache()

def delete_connection(conn_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM connections WHERE id=?", (conn_id,))
    conn.commit()
    conn.close()
    invalidate_cache()

def update_connection_offset(conn_id, offset):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE connections SET offset=? WHERE id=?", (offset, conn_id))
    conn.commit()
    conn.close()
    invalidate_cache()

# ---------- Основные функции работы с объектами ----------
def get_objects_list():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, type, city, pos_x, pos_y, base_kv, r_ohm, x_ohm FROM objects ORDER BY id")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_params_for_object(obj_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT param_type, nominal, max_allowed, min_allowed, unit FROM params WHERE object_id=?", (obj_id,))
    rows = cursor.fetchall()
    conn.close()
    params = {}
    for row in rows:
        params[row[0]] = {
            'nominal': row[1],
            'max_allowed': row[2],
            'min_allowed': row[3],
            'unit': row[4]
        }
    return params

def get_object_name(obj_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM objects WHERE id=?", (obj_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else f"id{obj_id}"

def get_object_city(obj_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT city FROM objects WHERE id=?", (obj_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

def save_object_to_db(obj_id, name, obj_type, city, pos_x=None, pos_y=None, base_kv=None, r_ohm=None, x_ohm=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    if obj_id is None:
        cursor.execute("""
            INSERT INTO objects (name, type, city, pos_x, pos_y, base_kv, r_ohm, x_ohm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, obj_type, city, pos_x, pos_y, base_kv, r_ohm, x_ohm))
        obj_id = cursor.lastrowid
    else:
        cursor.execute("""
            UPDATE objects
            SET name=?, type=?, city=?, pos_x=?, pos_y=?, base_kv=?, r_ohm=?, x_ohm=?
            WHERE id=?
        """, (name, obj_type, city, pos_x, pos_y, base_kv, r_ohm, x_ohm, obj_id))
    conn.commit()
    conn.close()
    invalidate_cache()
    return obj_id

def delete_object_from_db(obj_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("DELETE FROM objects WHERE id=?", (obj_id,))
    conn.commit()
    conn.close()
    invalidate_cache()

def save_params_for_object(obj_id, params_dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("DELETE FROM params WHERE object_id=?", (obj_id,))
    for param_type, pdata in params_dict.items():
        cursor.execute("""
            INSERT INTO params (object_id, param_type, nominal, max_allowed, min_allowed, unit)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (obj_id, param_type, pdata['nominal'], pdata['max_allowed'], pdata['min_allowed'], pdata['unit']))
    conn.commit()
    conn.close()
    invalidate_cache()

# ---------- Класс для отрисовки схемы ----------
class SchemeCanvas(tk.Canvas):
    def __init__(self, parent, app):
        super().__init__(parent, bg='#2c3e50', highlightthickness=0)
        self.app = app
        self.objects = []
        self.connections = []
        self.node_items = {}
        self.line_items = {}
        self.selected_node = None
        self.edit_mode = False
        self.use_orthogonal_lines = True
        self.bind("<Button-1>", self.on_canvas_click)
        self.bind("<B1-Motion>", self.on_canvas_drag)
        self.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.bind("<Button-3>", self.show_context_menu)
        self.create_context_menu()

    def create_context_menu(self):
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Добавить связь от этого объекта", command=self.add_connection_from_selected)
        self.context_menu.add_command(label="Удалить объект", command=self.delete_selected_object)

    def show_context_menu(self, event):
        if not self.edit_mode:
            return
        obj = self.get_object_at(event.x, event.y)
        if obj:
            self.selected_node = obj
            self.draw_scheme()
            self.context_menu.post(event.x_root, event.y_root)

    def get_object_at(self, x, y):
        for obj in self.objects:
            obj_id, name, typ, city, px, py = obj[:6]
            if abs(px - x) < 60 and abs(py - y) < 50:
                return obj_id
        return None

    def add_connection_from_selected(self):
        if self.selected_node is None:
            return
        objects = get_cached_objects()
        others = [(oid, name) for oid, name, typ, city, px, py, base_kv, r_ohm, x_ohm in objects if oid != self.selected_node]
        if not others:
            messagebox.showinfo("Нет объектов", "Нет других объектов для связи")
            return
        lines = [(oid, name) for oid, name, typ, city, px, py, base_kv, r_ohm, x_ohm in objects if typ in ('Линия', 'Line')]
        if not lines:
            messagebox.showerror("Ошибка", "Нет линий. Сначала добавьте линию.")
            return
        dialog = tb.Toplevel(self.app.root)
        dialog.title("Добавить связь")
        dialog.geometry("600x700")
        dialog.resizable(False, False)
        row = 0
        # Линия
        tb.Label(dialog, text="Линия:").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        line_var = tk.StringVar()
        line_combo = ttk.Combobox(dialog, textvariable=line_var, state="readonly", width=40)
        line_combo['values'] = [f"{name} (id={oid})" for oid, name in lines]
        line_combo.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1
        # От объекта
        tb.Label(dialog, text="От объекта:").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        from_var = tk.StringVar()
        from_combo = ttk.Combobox(dialog, textvariable=from_var, state="readonly", width=40)
        from_combo['values'] = [f"{name} (id={oid})" for oid, name in others]
        from_combo.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1
        # К объекту
        tb.Label(dialog, text="К объекту:").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        to_var = tk.StringVar()
        to_combo = ttk.Combobox(dialog, textvariable=to_var, state="readonly", width=40)
        to_combo['values'] = [f"{name} (id={oid})" for oid, name in others]
        to_combo.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1
        # Смещение
        tb.Label(dialog, text="Смещение (пикселей):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        offset_entry = tb.Entry(dialog, width=10)
        offset_entry.insert(0, "0")
        offset_entry.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1
        # Способ задания параметров
        param_mode = tk.StringVar(value="std")
        frame_mode = ttk.LabelFrame(dialog, text="Способ задания параметров линии")
        frame_mode.grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        row += 1
        tb.Radiobutton(frame_mode, text="Ввести вручную", variable=param_mode, value="manual").pack(side=tk.LEFT, padx=10, pady=5)
        tb.Radiobutton(frame_mode, text="Выбрать стандартный тип", variable=param_mode, value="std").pack(side=tk.LEFT, padx=10, pady=5)
        # Блок ручного ввода
        frame_manual = ttk.LabelFrame(dialog, text="Ручные параметры")
        frame_manual.grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        tb.Label(frame_manual, text="Длина линии (км):").grid(row=0, column=0, padx=10, pady=5, sticky="e")
        length_entry = tb.Entry(frame_manual, width=15)
        length_entry.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_manual, text="Сопротивление R (Ом/км):").grid(row=1, column=0, padx=10, pady=5, sticky="e")
        r_entry = tb.Entry(frame_manual, width=15)
        r_entry.grid(row=1, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_manual, text="Реактивное X (Ом/км):").grid(row=2, column=0, padx=10, pady=5, sticky="e")
        x_entry = tb.Entry(frame_manual, width=15)
        x_entry.grid(row=2, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_manual, text="Ёмкость C (нФ/км):").grid(row=3, column=0, padx=10, pady=5, sticky="e")
        c_entry = tb.Entry(frame_manual, width=15)
        c_entry.grid(row=3, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_manual, text="Макс. ток (кА):").grid(row=4, column=0, padx=10, pady=5, sticky="e")
        i_entry = tb.Entry(frame_manual, width=15)
        i_entry.grid(row=4, column=1, padx=10, pady=5, sticky="w")
        # Блок стандартного типа
        frame_std = ttk.LabelFrame(dialog, text="Стандартный тип")
        frame_std.grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        row += 1
        tb.Label(frame_std, text="Длина линии (км):").grid(row=0, column=0, padx=10, pady=5, sticky="e")
        length_std_entry = tb.Entry(frame_std, width=15)
        length_std_entry.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_std, text="Стандартный тип:").grid(row=1, column=0, padx=10, pady=5, sticky="e")
        std_type_var = tk.StringVar()
        std_type_combo = ttk.Combobox(frame_std, textvariable=std_type_var, state="readonly", width=35)
        # Заполняем список стандартных типов
        if PANDAPOWER_AVAILABLE:
            try:
                std_types = sorted(pp.std_types.keys())
            except:
                std_types = ["NA2XS2Y 1x300 RM/35", "NA2XS2Y 1x400 RM/35", "49-AL1/8-ST1A", "243-AL1/39-ST1A", "380-AL1/49-ST1A"]
        else:
            std_types = ["NA2XS2Y 1x300 RM/35", "NA2XS2Y 1x400 RM/35", "49-AL1/8-ST1A", "243-AL1/39-ST1A", "380-AL1/49-ST1A"]
        std_type_combo['values'] = std_types
        if std_types:
            std_type_combo.current(0)
        std_type_combo.grid(row=1, column=1, padx=10, pady=5, sticky="w")

        def update_param_mode(*args):
            if param_mode.get() == "manual":
                frame_manual.grid()
                frame_std.grid_remove()
            else:
                frame_manual.grid_remove()
                frame_std.grid()
        param_mode.trace_add('write', update_param_mode)
        update_param_mode()

        btn_frame = tb.Frame(dialog)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=15)

        def save():
            line_str = line_var.get()
            from_str = from_var.get()
            to_str = to_var.get()
            if not line_str or not from_str or not to_str:
                messagebox.showerror("Ошибка", "Заполните все поля: линия, от объекта, к объекту")
                return
            try:
                line_id = int(line_str.split("id=")[1].rstrip(")"))
                from_id = int(from_str.split("id=")[1].rstrip(")"))
                to_id = int(to_str.split("id=")[1].rstrip(")"))
            except:
                messagebox.showerror("Ошибка", "Не удалось определить ID объектов")
                return
            if from_id == to_id:
                messagebox.showerror("Ошибка", "Объекты 'от' и 'к' должны быть разными")
                return
            try:
                offset = float(offset_entry.get())
            except ValueError:
                offset = 0

            if param_mode.get() == "manual":
                try:
                    length = float(length_entry.get()) if length_entry.get() else None
                    r_ohm = float(r_entry.get()) if r_entry.get() else None
                    x_ohm = float(x_entry.get()) if x_entry.get() else None
                    c_nf = float(c_entry.get()) if c_entry.get() else None
                    max_i = float(i_entry.get()) if i_entry.get() else None
                    std_type = None
                except ValueError:
                    messagebox.showerror("Ошибка", "Проверьте числовые значения в ручном вводе")
                    return
            else:
                try:
                    length = float(length_std_entry.get()) if length_std_entry.get() else None
                    if not length or length <= 0:
                        messagebox.showerror("Ошибка", "Укажите положительную длину линии")
                        return
                    std_type = std_type_var.get()
                    if not std_type:
                        messagebox.showerror("Ошибка", "Выберите стандартный тип линии")
                        return
                    r_ohm = x_ohm = c_nf = max_i = None
                except ValueError:
                    messagebox.showerror("Ошибка", "Проверьте длину линии")
                    return

            add_connection(line_id, from_id, to_id, offset, length, r_ohm, x_ohm, c_nf, max_i, std_type)
            dialog.destroy()
            self.app.refresh_connections_table()
            self.generate_layout()
            messagebox.showinfo("Успех", "Связь добавлена. Нажмите 'Обновить схему' для пересчёта параметров.")

        tb.Button(btn_frame, text="Сохранить", command=save).pack(side=tk.LEFT, padx=5)
        tb.Button(btn_frame, text="Отмена", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

    def delete_selected_object(self):
        if self.selected_node and messagebox.askyesno("Удалить", "Удалить объект и все его параметры?"):
            delete_object_from_db(self.selected_node)
            self.app.refresh_object_list()
            self.app.refresh_connections_table()
            self.generate_layout()
            self.selected_node = None

    def get_line_voltage(self, line_id):
        params = get_cached_params(line_id)
        if 'voltage' in params:
            return params['voltage']['nominal']
        return 110

    def get_line_current(self, line_id):
        val = get_cached_measurement(line_id, 'current')
        if val is not None:
            return val
        params = get_cached_params(line_id)
        return params.get('current', {}).get('nominal', 0)

    def get_line_status(self, line_id):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM objects WHERE id=?", (line_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 'normal'

    def get_orthogonal_path(self, x1, y1, x2, y2):
        if abs(x1 - x2) < 30 or abs(y1 - y2) < 30:
            return [(x1, y1), (x2, y2)]
        dx = x2 - x1
        dy = y2 - y1
        if abs(dx) > abs(dy):
            mid_x = (x1 + x2) / 2
            return [(x1, y1), (mid_x, y1), (mid_x, y2), (x2, y2)]
        else:
            mid_y = (y1 + y2) / 2
            return [(x1, y1), (x1, mid_y), (x2, mid_y), (x2, y2)]

    def generate_layout(self):
        rows = get_cached_objects()
        if not rows:
            self.objects = []
            self.connections = []
            self.draw_scheme()
            return
        self.objects = []
        for obj in rows:
            obj_id, name, typ, city, pos_x, pos_y, base_kv, r_ohm, x_ohm = obj
            try:
                x = float(pos_x) if pos_x is not None else 0
                y = float(pos_y) if pos_y is not None else 0
            except:
                x, y = 0, 0
            self.objects.append([obj_id, name, typ, city or "", x, y, base_kv, r_ohm, x_ohm])
        need_auto = any(obj[4] == 0 and obj[5] == 0 for obj in self.objects)
        if need_auto:
            self.auto_layout()
        db_conns = get_all_connections()
        self.connections = []
        obj_ids = {obj[0] for obj in self.objects}
        for conn in db_conns:
            if len(conn) > 5:
                conn_id, line_id, from_id, to_id, offset = conn[:5]
                length_km, r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka, std_type = conn[5:]
            else:
                conn_id, line_id, from_id, to_id, offset = conn[:5]
                length_km = r_ohm_per_km = x_ohm_per_km = c_nf_per_km = max_i_ka = std_type = None
            if from_id not in obj_ids or to_id not in obj_ids:
                continue
            from_idx = to_idx = None
            for idx, obj in enumerate(self.objects):
                if obj[0] == from_id:
                    from_idx = idx
                if obj[0] == to_id:
                    to_idx = idx
            if from_idx is not None and to_idx is not None:
                self.connections.append((line_id, from_idx, to_idx, conn_id, offset, length_km, r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka, std_type))
        self.update_idletasks()
        self.draw_scheme()
        self.configure(scrollregion=self.bbox("all"))

    def auto_layout(self):
        groups = defaultdict(list)
        for idx, obj in enumerate(self.objects):
            if obj[4] == 0 and obj[5] == 0:
                city = obj[3] or "Без города"
                groups[city].append(idx)
        if not groups:
            groups[""] = list(range(len(self.objects)))
        city_order = ["Братск", "Усть-Илимск", "Тайшет", "Иркутск", "Без города"]
        def city_key(city):
            try:
                return city_order.index(city)
            except ValueError:
                return len(city_order)
        sorted_cities = sorted(groups.keys(), key=city_key)
        col_width = 300
        row_height = 80
        start_x = 50
        start_y = 50
        max_rows = 6
        col = 0
        for city in sorted_cities:
            idx_list = groups[city]
            def type_order(idx):
                typ = self.objects[idx][2].lower()
                if typ in ('гэс', 'ges'):
                    return 0
                if typ in ('подстанция', 'substation'):
                    return 1
                if typ in ('потребитель', 'consumer'):
                    return 2
                return 3
            idx_list_sorted = sorted(idx_list, key=type_order)
            for chunk_start in range(0, len(idx_list_sorted), max_rows):
                chunk = idx_list_sorted[chunk_start:chunk_start+max_rows]
                for row, idx in enumerate(chunk):
                    x = start_x + col * col_width
                    y = start_y + row * row_height
                    self.objects[idx][4] = x
                    self.objects[idx][5] = y
                col += 1

    def draw_scheme(self):
        self.delete('all')
        self.node_items.clear()
        self.line_items.clear()
        # Рисуем связи
        edge_groups = defaultdict(list)
        for idx, conn in enumerate(self.connections):
            line_id, from_idx, to_idx, conn_id, offset = conn[:5]
            key = (min(from_idx, to_idx), max(from_idx, to_idx))
            edge_groups[key].append((idx, line_id, from_idx, to_idx, conn_id, offset))
        for (a, b), edges in edge_groups.items():
            count = len(edges)
            for i, (_, line_id, from_idx, to_idx, conn_id, user_offset) in enumerate(edges):
                x1, y1 = self.objects[from_idx][4], self.objects[from_idx][5]
                x2, y2 = self.objects[to_idx][4], self.objects[to_idx][5]
                parallel_offset = (i - (count-1)/2) * 20
                total_offset = parallel_offset + (user_offset if user_offset else 0)
                dx = x2 - x1
                dy = y2 - y1
                length = math.hypot(dx, dy)
                if length > 0:
                    perp_x = -dy / length * total_offset
                    perp_y = dx / length * total_offset
                else:
                    perp_x = perp_y = 0
                is_emergency = self.get_line_status(line_id) == 'emergency'
                if is_emergency:
                    color = '#ff4d4d'
                    dash = (6, 4)
                    width = 3
                else:
                    color = '#4da6ff'
                    dash = None
                    width = 3
                if self.use_orthogonal_lines:
                    x1c = x1 + perp_x
                    y1c = y1 + perp_y
                    x2c = x2 + perp_x
                    y2c = y2 + perp_y
                    points = self.get_orthogonal_path(x1c, y1c, x2c, y2c)
                    line_obj = self.create_line(points, fill=color, width=width, dash=dash, smooth=False)
                    if len(points) >= 3:
                        mid_x, mid_y = points[len(points)//2]
                    else:
                        mid_x, mid_y = (x1c + x2c)/2, (y1c + y2c)/2
                else:
                    x1c = x1 + perp_x
                    y1c = y1 + perp_y
                    x2c = x2 + perp_x
                    y2c = y2 + perp_y
                    line_obj = self.create_line(x1c, y1c, x2c, y2c, fill=color, width=width, dash=dash)
                    mid_x, mid_y = (x1c + x2c)/2, (y1c + y2c)/2
                current = self.get_line_current(line_id)
                voltage = self.get_line_voltage(line_id)
                text_obj = self.create_text(mid_x, mid_y - 10, text=f"{current:.1f} A, {voltage:.0f} kV", fill=color, font=('Arial', 8, 'bold'))
                self.line_items[line_id] = (line_obj, text_obj)
        # Рисуем узлы
        for obj in self.objects:
            obj_id, name, typ, city, x, y = obj[:6]
            status = self.get_object_status(obj_id)
            info = self.app.get_object_display_info(obj_id)
            typ_lower = typ.lower()
            if typ_lower in ('гэс', 'ges'):
                r = 25
                symbol = self.create_oval(x-r, y-r, x+r, y+r, fill='#f1c40f', outline='#2c3e50', width=2)
                text_id = self.create_text(x, y, text="ГЭС", fill='#2c3e50', font=('Arial', 14, 'bold'))
                power = info.get('power_gen', 0)
                voltage = info.get('voltage', 0)
                param_text = f"{power:.0f} MW\n{voltage:.0f} kV"
                param_x, param_y = x + 55, y - 25
                param_id = self.create_text(param_x, param_y, text=param_text, fill='white', font=('Arial', 8), anchor='w')
            elif typ_lower in ('подстанция', 'substation'):
                w, h = 40, 30
                symbol = self.create_rectangle(x-w/2, y-h/2, x+w/2, y+h/2, fill='#3498db', outline='white', width=2)
                text_id = self.create_text(x, y, text="ПС", fill='white', font=('Arial', 9, 'bold'))
                voltage = info.get('voltage', 0)
                power = info.get('power_flow', 0)
                param_text = f"{voltage:.0f} kV\n{power:.0f} MW"
                param_x, param_y = x + 55, y - 25
                param_id = self.create_text(param_x, param_y, text=param_text, fill='white', font=('Arial', 8), anchor='w')
            elif typ_lower in ('потребитель', 'consumer'):
                w, h = 30, 30
                symbol = self.create_rectangle(x-w/2, y-h/2, x+w/2, y+h/2, fill='#2ecc71', outline='white', width=2)
                text_id = self.create_text(x, y, text="П", fill='white', font=('Arial', 14, 'bold'))
                voltage = info.get('voltage', 0)
                param_text = f"{voltage:.0f} kV"
                param_x, param_y = x + 45, y - 20
                param_id = self.create_text(param_x, param_y, text=param_text, fill='white', font=('Arial', 8), anchor='w')
            else:
                continue
            if status == 'emergency':
                self.itemconfig(symbol, fill='#e74c3c')
            name_id = self.create_text(x, y + 45, text=name, fill='white', font=('Arial', 9, 'bold'))
            self.node_items[obj_id] = (symbol, text_id, param_id, name_id)
            if self.edit_mode and self.selected_node == obj_id:
                self.create_rectangle(x-32, y-32, x+32, y+32, outline='white', width=2, dash=(4,2), tags="selection")

    def get_object_status(self, obj_id):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM objects WHERE id=?", (obj_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 'normal'

    def update_node_color(self, obj_id):
        if obj_id in self.node_items:
            symbol, text, param, name_id = self.node_items[obj_id]
            status = self.get_object_status(obj_id)
            fill = '#e74c3c' if status == 'emergency' else None
            if fill:
                self.itemconfig(symbol, fill=fill)

    def set_edit_mode(self, enabled):
        self.edit_mode = enabled
        self.config(cursor="hand2" if enabled else "")
        if not enabled:
            self.selected_node = None
        self.draw_scheme()

    def save_positions(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        for obj in self.objects:
            obj_id, name, typ, city, x, y = obj[:6]
            cursor.execute("UPDATE objects SET pos_x=?, pos_y=? WHERE id=?", (x, y, obj_id))
        conn.commit()
        conn.close()
        self.app.add_to_log("Позиции объектов сохранены")
        messagebox.showinfo("Сохранено", "Расположение объектов сохранено в базе данных")
        invalidate_cache()

    def on_canvas_click(self, event):
        if self.edit_mode:
            obj = self.get_object_at(event.x, event.y)
            if obj:
                self.selected_node = obj
                self.draw_scheme()
            else:
                self.selected_node = None
                self.draw_scheme()
        else:
            obj = self.get_object_at(event.x, event.y)
            if obj:
                self.app.show_object_info(obj)

    def on_canvas_drag(self, event):
        if self.edit_mode and self.selected_node is not None:
            for idx, obj in enumerate(self.objects):
                if obj[0] == self.selected_node:
                    self.objects[idx][4] = event.x
                    self.objects[idx][5] = event.y
                    break
            self.draw_scheme()

    def on_canvas_release(self, event):
        if self.edit_mode and self.selected_node is not None:
            self.configure(scrollregion=self.bbox("all"))
            self.selected_node = None
            self.draw_scheme()

# ---------- Основное приложение ----------
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Расчёт аварийных режимов (pandapower)")
        self.root.geometry("1300x850")
        icon = tk.PhotoImage(width=16, height=16)
        icon.put("#4da6ff", to=(0, 0, 15, 15))
        icon.put("#ffffff", to=(4, 4, 11, 11))
        self.root.iconphoto(True, icon)
        self.style = tb.Style(theme='darkly')
        self.current_obj_id = None
        init_db()
        migrate_db()
        self.create_widgets()
        self.refresh_object_list()
        self.refresh_connections_table()
        self.recalculate_network()

    def create_widgets(self):
        self.notebook = tb.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.tab_objects = tb.Frame(self.notebook)
        self.notebook.add(self.tab_objects, text="Объекты и параметры", padding=10)
        self.create_objects_tab()

        self.tab_conn = tb.Frame(self.notebook)
        self.notebook.add(self.tab_conn, text="Связи", padding=10)
        self.create_connections_tab()

        self.tab_scheme = tb.Frame(self.notebook)
        self.notebook.add(self.tab_scheme, text="Схема сети", padding=10)
        self.create_scheme_tab()

        self.tab_log = tb.Frame(self.notebook)
        self.notebook.add(self.tab_log, text="Журнал", padding=10)
        self.create_log_tab()

        self.tab_sim = tb.Frame(self.notebook)
        self.notebook.add(self.tab_sim, text="Симуляция аварий", padding=10)
        self.create_sim_tab()

    def create_connections_tab(self):
        main_frame = tb.Frame(self.tab_conn)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        columns = ("id", "line", "from", "to", "offset", "length", "r_ohm", "x_ohm", "c_nf", "max_i", "std_type")
        self.conn_tree = ttk.Treeview(main_frame, columns=columns, show="headings", height=15)
        self.conn_tree.heading("id", text="ID")
        self.conn_tree.heading("line", text="Линия")
        self.conn_tree.heading("from", text="От объекта")
        self.conn_tree.heading("to", text="К объекту")
        self.conn_tree.heading("offset", text="Смещение")
        self.conn_tree.heading("length", text="Длина, км")
        self.conn_tree.heading("r_ohm", text="R, Ом/км")
        self.conn_tree.heading("x_ohm", text="X, Ом/км")
        self.conn_tree.heading("c_nf", text="C, нФ/км")
        self.conn_tree.heading("max_i", text="Imax, кА")
        self.conn_tree.heading("std_type", text="Тип")
        for col in columns:
            self.conn_tree.column(col, width=80)
        self.conn_tree.column("id", width=50)
        self.conn_tree.column("line", width=150)
        self.conn_tree.column("from", width=150)
        self.conn_tree.column("to", width=150)
        self.conn_tree.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(main_frame, orient=tk.VERTICAL, command=self.conn_tree.yview)
        self.conn_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        btn_frame = tb.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        tb.Button(btn_frame, text="Добавить связь", command=self.add_connection_gui).pack(side=tk.LEFT, padx=5)
        tb.Button(btn_frame, text="Удалить выбранную", command=self.delete_connection_gui).pack(side=tk.LEFT, padx=5)
        tb.Button(btn_frame, text="Обновить", command=self.refresh_connections_table).pack(side=tk.LEFT, padx=5)
        self.conn_tree.bind("<Double-1>", self.edit_connection_offset)

    def edit_connection_offset(self, event):
        region = self.conn_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        column = self.conn_tree.identify_column(event.x)
        if column != '#5':
            return
        item = self.conn_tree.selection()[0]
        conn_id = int(item)
        current_offset = float(self.conn_tree.item(item, 'values')[4])
        new_offset = simpledialog.askfloat("Смещение", "Введите смещение (пиксели, отрицательное для сдвига в другую сторону):", initialvalue=current_offset)
        if new_offset is not None:
            update_connection_offset(conn_id, new_offset)
            self.refresh_connections_table()
            self.refresh_scheme()

    def refresh_connections_table(self):
        for row in self.conn_tree.get_children():
            self.conn_tree.delete(row)
        conns = get_all_connections()
        for conn in conns:
            if len(conn) > 5:
                cid, line_id, from_id, to_id, offset, length_km, r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka, std_type = conn
            else:
                cid, line_id, from_id, to_id, offset = conn[:5]
                length_km = r_ohm_per_km = x_ohm_per_km = c_nf_per_km = max_i_ka = std_type = None
            line_name = get_object_name(line_id)
            from_name = get_object_name(from_id)
            to_name = get_object_name(to_id)
            self.conn_tree.insert("", tk.END, values=(
                cid, line_name, from_name, to_name, offset,
                length_km, r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka, std_type
            ), iid=cid)

    def add_connection_gui(self):
        objects = get_cached_objects()
        if not objects:
            messagebox.showerror("Ошибка", "Нет объектов. Сначала добавьте объекты.")
            return
        lines = [(oid, name) for oid, name, typ, city, px, py, base_kv, r_ohm, x_ohm in objects if typ in ('Линия', 'Line')]
        others = [(oid, name) for oid, name, typ, city, px, py, base_kv, r_ohm, x_ohm in objects if typ not in ('Линия', 'Line')]
        if not lines:
            messagebox.showerror("Ошибка", "Нет линий. Сначала добавьте линию.")
            return
        if len(others) < 2:
            messagebox.showerror("Ошибка", "Нужно минимум два объекта (не линий) для соединения.")
            return

        dialog = tb.Toplevel(self.root)
        dialog.title("Добавить связь")
        dialog.geometry("600x700")
        dialog.resizable(False, False)

        row = 0
        # Линия
        tb.Label(dialog, text="Линия:").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        line_var = tk.StringVar()
        line_combo = ttk.Combobox(dialog, textvariable=line_var, state="readonly", width=40)
        line_combo['values'] = [f"{name} (id={oid})" for oid, name in lines]
        line_combo.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1

        # От объекта
        tb.Label(dialog, text="От объекта:").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        from_var = tk.StringVar()
        from_combo = ttk.Combobox(dialog, textvariable=from_var, state="readonly", width=40)
        from_combo['values'] = [f"{name} (id={oid})" for oid, name in others]
        from_combo.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1

        # К объекту
        tb.Label(dialog, text="К объекту:").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        to_var = tk.StringVar()
        to_combo = ttk.Combobox(dialog, textvariable=to_var, state="readonly", width=40)
        to_combo['values'] = [f"{name} (id={oid})" for oid, name in others]
        to_combo.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1

        # Смещение
        tb.Label(dialog, text="Смещение (пикселей):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        offset_entry = tb.Entry(dialog, width=10)
        offset_entry.insert(0, "0")
        offset_entry.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1

        # Способ задания параметров
        param_mode = tk.StringVar(value="std")
        frame_mode = ttk.LabelFrame(dialog, text="Способ задания параметров линии")
        frame_mode.grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        row += 1
        tb.Radiobutton(frame_mode, text="Ввести вручную", variable=param_mode, value="manual").pack(side=tk.LEFT, padx=10, pady=5)
        tb.Radiobutton(frame_mode, text="Выбрать стандартный тип", variable=param_mode, value="std").pack(side=tk.LEFT, padx=10, pady=5)

        # Блок ручного ввода
        frame_manual = ttk.LabelFrame(dialog, text="Ручные параметры")
        frame_manual.grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        tb.Label(frame_manual, text="Длина линии (км):").grid(row=0, column=0, padx=10, pady=5, sticky="e")
        length_entry = tb.Entry(frame_manual, width=15)
        length_entry.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_manual, text="Сопротивление R (Ом/км):").grid(row=1, column=0, padx=10, pady=5, sticky="e")
        r_entry = tb.Entry(frame_manual, width=15)
        r_entry.grid(row=1, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_manual, text="Реактивное X (Ом/км):").grid(row=2, column=0, padx=10, pady=5, sticky="e")
        x_entry = tb.Entry(frame_manual, width=15)
        x_entry.grid(row=2, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_manual, text="Ёмкость C (нФ/км):").grid(row=3, column=0, padx=10, pady=5, sticky="e")
        c_entry = tb.Entry(frame_manual, width=15)
        c_entry.grid(row=3, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_manual, text="Макс. ток (кА):").grid(row=4, column=0, padx=10, pady=5, sticky="e")
        i_entry = tb.Entry(frame_manual, width=15)
        i_entry.grid(row=4, column=1, padx=10, pady=5, sticky="w")

        # Блок стандартного типа
        frame_std = ttk.LabelFrame(dialog, text="Стандартный тип")
        frame_std.grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        row += 1
        tb.Label(frame_std, text="Длина линии (км):").grid(row=0, column=0, padx=10, pady=5, sticky="e")
        length_std_entry = tb.Entry(frame_std, width=15)
        length_std_entry.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        tb.Label(frame_std, text="Стандартный тип:").grid(row=1, column=0, padx=10, pady=5, sticky="e")
        std_type_var = tk.StringVar()
        std_type_combo = ttk.Combobox(frame_std, textvariable=std_type_var, state="readonly", width=35)
        if PANDAPOWER_AVAILABLE:
            try:
                std_types = sorted(pp.std_types.keys())
            except:
                std_types = ["NA2XS2Y 1x300 RM/35", "NA2XS2Y 1x400 RM/35", "49-AL1/8-ST1A", "243-AL1/39-ST1A", "380-AL1/49-ST1A"]
        else:
            std_types = ["NA2XS2Y 1x300 RM/35", "NA2XS2Y 1x400 RM/35", "49-AL1/8-ST1A", "243-AL1/39-ST1A", "380-AL1/49-ST1A"]
        std_type_combo['values'] = std_types
        if std_types:
            std_type_combo.current(0)
        std_type_combo.grid(row=1, column=1, padx=10, pady=5, sticky="w")

        def update_param_mode(*args):
            if param_mode.get() == "manual":
                frame_manual.grid()
                frame_std.grid_remove()
            else:
                frame_manual.grid_remove()
                frame_std.grid()
        param_mode.trace_add('write', update_param_mode)
        update_param_mode()

        btn_frame = tb.Frame(dialog)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=15)

        def save():
            line_str = line_var.get()
            from_str = from_var.get()
            to_str = to_var.get()
            if not line_str or not from_str or not to_str:
                messagebox.showerror("Ошибка", "Заполните все поля: линия, от объекта, к объекту")
                return
            try:
                line_id = int(line_str.split("id=")[1].rstrip(")"))
                from_id = int(from_str.split("id=")[1].rstrip(")"))
                to_id = int(to_str.split("id=")[1].rstrip(")"))
            except:
                messagebox.showerror("Ошибка", "Не удалось определить ID объектов")
                return
            if from_id == to_id:
                messagebox.showerror("Ошибка", "Объекты 'от' и 'к' должны быть разными")
                return
            try:
                offset = float(offset_entry.get())
            except ValueError:
                offset = 0

            if param_mode.get() == "manual":
                try:
                    length = float(length_entry.get()) if length_entry.get() else None
                    r_ohm = float(r_entry.get()) if r_entry.get() else None
                    x_ohm = float(x_entry.get()) if x_entry.get() else None
                    c_nf = float(c_entry.get()) if c_entry.get() else None
                    max_i = float(i_entry.get()) if i_entry.get() else None
                    std_type = None
                except ValueError:
                    messagebox.showerror("Ошибка", "Проверьте числовые значения в ручном вводе")
                    return
            else:
                try:
                    length = float(length_std_entry.get()) if length_std_entry.get() else None
                    if not length or length <= 0:
                        messagebox.showerror("Ошибка", "Укажите положительную длину линии")
                        return
                    std_type = std_type_var.get()
                    if not std_type:
                        messagebox.showerror("Ошибка", "Выберите стандартный тип линии")
                        return
                    r_ohm = x_ohm = c_nf = max_i = None
                except ValueError:
                    messagebox.showerror("Ошибка", "Проверьте длину линии")
                    return

            add_connection(line_id, from_id, to_id, offset, length, r_ohm, x_ohm, c_nf, max_i, std_type)
            dialog.destroy()
            self.refresh_connections_table()
            self.refresh_scheme()
            messagebox.showinfo("Успех", "Связь добавлена. Нажмите 'Обновить схему' для пересчёта параметров.")

        tb.Button(btn_frame, text="Сохранить", command=save).pack(side=tk.LEFT, padx=5)
        tb.Button(btn_frame, text="Отмена", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

    def delete_connection_gui(self):
        selected = self.conn_tree.selection()
        if not selected:
            messagebox.showinfo("Информация", "Выберите связь для удаления")
            return
        conn_id = int(selected[0])
        if messagebox.askyesno("Подтверждение", "Удалить выбранную связь?"):
            delete_connection(conn_id)
            self.refresh_connections_table()
            self.refresh_scheme()
            self.recalculate_network()

    def create_objects_tab(self):
        top_frame = tb.Frame(self.tab_objects)
        top_frame.pack(fill=tk.X, pady=5)
        tb.Button(top_frame, text="Добавить", command=self.add_object).pack(side=tk.LEFT, padx=5)
        tb.Button(top_frame, text="Редактировать", command=self.edit_object).pack(side=tk.LEFT, padx=5)
        tb.Button(top_frame, text="Изменить параметры", command=self.change_parameter_value).pack(side=tk.LEFT, padx=5)
        tb.Button(top_frame, text="Удалить", command=self.delete_object).pack(side=tk.LEFT, padx=5)
        tb.Separator(top_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        tb.Button(top_frame, text="Экспорт БД", command=self.export_db).pack(side=tk.LEFT, padx=5)
        tb.Button(top_frame, text="Импорт БД", command=self.import_db).pack(side=tk.LEFT, padx=5)
        main_frame = tb.Frame(self.tab_objects)
        main_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        left_frame = ttk.LabelFrame(main_frame, text="Список объектов")
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0,5))
        columns = ("type", "city", "base_kv", "r_ohm", "x_ohm")
        self.tree = ttk.Treeview(left_frame, columns=columns, show="tree headings", height=20)
        self.tree.heading("#0", text="Название")
        self.tree.heading("type", text="Тип")
        self.tree.heading("city", text="Город")
        self.tree.heading("base_kv", text="Ном. кВ")
        self.tree.heading("r_ohm", text="R, Ом")
        self.tree.heading("x_ohm", text="X, Ом")
        self.tree.column("#0", width=200)
        self.tree.column("type", width=100)
        self.tree.column("city", width=100)
        self.tree.column("base_kv", width=80)
        self.tree.column("r_ohm", width=80)
        self.tree.column("x_ohm", width=80)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_tree = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll_tree.set)
        scroll_tree.pack(side=tk.RIGHT, fill=tk.Y)
        right_frame = ttk.LabelFrame(main_frame, text="Параметры объекта")
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5,0))
        self.param_frame = tb.Frame(right_frame)
        self.param_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.tree.bind("<<TreeviewSelect>>", self.on_object_select)

    def create_scheme_tab(self):
        canvas_frame = tb.Frame(self.tab_scheme)
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        self.scheme_canvas = SchemeCanvas(canvas_frame, self)
        h_scroll = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL, command=self.scheme_canvas.xview)
        v_scroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.scheme_canvas.yview)
        self.scheme_canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        self.scheme_canvas.grid(row=0, column=0, sticky="nsew")
        h_scroll.grid(row=1, column=0, sticky="ew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)
        btn_frame = tb.Frame(self.tab_scheme)
        btn_frame.pack(fill=tk.X, pady=5)
        tb.Button(btn_frame, text="Обновить схему", command=self.refresh_scheme).pack(side=tk.LEFT, padx=5)
        tb.Button(btn_frame, text="Обновить параметры", command=self.refresh_scheme_params).pack(side=tk.LEFT, padx=5)
        self.edit_mode_var = tk.BooleanVar(value=False)
        self.orthogonal_var = tk.BooleanVar(value=True)
        tb.Checkbutton(btn_frame, text="Режим редактирования", variable=self.edit_mode_var, command=self.toggle_edit_mode).pack(side=tk.LEFT, padx=5)
        tb.Checkbutton(btn_frame, text="Ортогональные линии", variable=self.orthogonal_var, command=self.toggle_line_style).pack(side=tk.LEFT, padx=5)
        tb.Button(btn_frame, text="Сохранить позиции", command=self.save_scheme_positions).pack(side=tk.LEFT, padx=5)
        self.root.update()
        self.scheme_canvas.generate_layout()

    def build_pandapower_network(self):
        if not PANDAPOWER_AVAILABLE:
            return None
        net = pp.create_empty_network()
        objects = get_cached_objects()
        bus_map = {}
        # Создаём шины
        for obj in objects:
            obj_id, name, typ, city, pos_x, pos_y, base_kv, r_ohm, x_ohm = obj
            typ_lower = typ.lower()
            if typ_lower in ('гэс', 'ges', 'подстанция', 'substation', 'потребитель', 'consumer'):
                vn_kv = base_kv if base_kv and base_kv > 0 else 110.0
                bus = pp.create_bus(net, vn_kv=vn_kv, name=name)
                bus_map[obj_id] = bus
                if typ_lower in ('гэс', 'ges'):
                    params = get_cached_params(obj_id)
                    p_gen = params.get('power_gen', {}).get('nominal', 0)
                    if p_gen > 0:
                        pp.create_gen(net, bus, p_mw=p_gen, vm_pu=1.02, name=f"Gen_{name}")
                elif typ_lower in ('потребитель', 'consumer'):
                    params = get_cached_params(obj_id)
                    p_load = params.get('power_flow', {}).get('nominal', 0)
                    if p_load > 0:
                        q_load = p_load * 0.33
                        pp.create_load(net, bus, p_mw=p_load, q_mvar=q_load, name=f"Load_{name}")
        # Добавляем внешнюю сеть (slack) на первую ГЭС
        for obj in objects:
            obj_id, name, typ = obj[:3]
            if typ.lower() in ('гэс', 'ges') and obj_id in bus_map:
                pp.create_ext_grid(net, bus_map[obj_id], vm_pu=1.02, name=f"Slack_{name}")
                break
        else:
            for obj in objects:
                obj_id, name, typ = obj[:3]
                if typ.lower() in ('подстанция', 'substation') and obj_id in bus_map:
                    pp.create_ext_grid(net, bus_map[obj_id], vm_pu=1.02, name=f"Slack_{name}")
                    break
        # Создаём линии
        conns = get_all_connections()
        for conn in conns:
            if len(conn) > 5:
                cid, line_id, from_id, to_id, offset, length_km, r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka, std_type = conn
            else:
                continue
            if from_id in bus_map and to_id in bus_map and length_km and length_km > 0:
                if r_ohm_per_km and x_ohm_per_km:
                    pp.create_line_from_parameters(
                        net, from_bus=bus_map[from_id], to_bus=bus_map[to_id],
                        length_km=length_km, r_ohm_per_km=r_ohm_per_km, x_ohm_per_km=x_ohm_per_km,
                        c_nf_per_km=c_nf_per_km or 0, max_i_ka=max_i_ka or 0.5,
                        name=f"Line_{line_id}"
                    )
                elif std_type:
                    pp.create_line(net, from_bus=bus_map[from_id], to_bus=bus_map[to_id],
                                   length_km=length_km, std_type=std_type, name=f"Line_{line_id}")
                else:
                    pp.create_line_from_parameters(net, from_bus=bus_map[from_id], to_bus=bus_map[to_id],
                                                   length_km=length_km, r_ohm_per_km=0.27, x_ohm_per_km=0.36,
                                                   c_nf_per_km=10, max_i_ka=0.5, name=f"Line_{line_id}")
        return net

    def recalculate_network(self):
        if not PANDAPOWER_AVAILABLE:
            self.add_to_log("ОШИБКА: pandapower не установлен. Расчёт невозможен.")
            messagebox.showerror("Ошибка", "pandapower не установлен. Установите: pip install pandapower")
            return
        self.root.config(cursor="watch")
        self.root.update()
        try:
            self.recalculate_network_pandapower()
        finally:
            self.root.config(cursor="")

    def recalculate_network_pandapower(self):
        try:
            net = self.build_pandapower_network()
            if net is None or len(net.bus) == 0:
                self.add_to_log("Ошибка: не удалось построить сеть для pandapower")
                return
            pp.runpp(net, algorithm='nr', max_iteration=20, tolerance_mva=1e-6)
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            obj_to_bus = {}
            for obj in get_cached_objects():
                obj_id, name = obj[0], obj[1]
                for idx, bus_name in enumerate(net.bus.name):
                    if bus_name == name:
                        obj_to_bus[obj_id] = idx
                        break
            for obj in get_cached_objects():
                obj_id, name, typ = obj[0], obj[1], obj[2]
                if obj_id not in obj_to_bus:
                    continue
                bus_idx = obj_to_bus[obj_id]
                vm_pu = net.res_bus.vm_pu.iloc[bus_idx]
                vn_kv = net.bus.vn_kv.iloc[bus_idx]
                voltage_kv = vm_pu * vn_kv
                self.save_measurement(obj_id, 'voltage', voltage_kv)
                p_kw = net.res_bus.p_kw.iloc[bus_idx]
                q_kvar = net.res_bus.q_kvar.iloc[bus_idx]
                s_kva = math.hypot(p_kw, q_kvar)
                if voltage_kv > 0:
                    current_a = s_kva / (math.sqrt(3) * voltage_kv) if s_kva > 0 else 0
                    self.save_measurement(obj_id, 'current', current_a)
                p_mw = p_kw / 1000.0
                self.save_measurement(obj_id, 'power_flow', p_mw)
                if typ.lower() in ('гэс', 'ges'):
                    for gen_idx, gen_bus in enumerate(net.gen.bus):
                        if gen_bus == bus_idx:
                            p_gen = net.res_gen.p_mw.iloc[gen_idx]
                            self.save_measurement(obj_id, 'power_gen', p_gen)
                            break
            for conn_row in get_all_connections():
                if len(conn_row) > 5:
                    cid, line_id, from_id, to_id, offset = conn_row[:5]
                else:
                    continue
                line_name = f"Line_{line_id}"
                line_indices = [idx for idx, name in enumerate(net.line.name) if name == line_name]
                if line_indices:
                    line_idx = line_indices[0]
                    i_ka = net.res_line.i_ka.iloc[line_idx]
                    self.save_measurement(line_id, 'current', i_ka * 1000)
                    from_bus = net.line.from_bus.iloc[line_idx]
                    to_bus = net.line.to_bus.iloc[line_idx]
                    v_from = net.res_bus.vm_pu.iloc[from_bus] * net.bus.vn_kv.iloc[from_bus]
                    v_to = net.res_bus.vm_pu.iloc[to_bus] * net.bus.vn_kv.iloc[to_bus]
                    v_avg = (v_from + v_to) / 2
                    self.save_measurement(line_id, 'voltage', v_avg)
                    p_mw = net.res_line.p_from_mw.iloc[line_idx]
                    self.save_measurement(line_id, 'power_flow', p_mw)
            conn.commit()
            conn.close()
            self.add_to_log("Расчёт pandapower выполнен успешно")
            self.refresh_scheme_params()
        except Exception as e:
            self.add_to_log(f"Ошибка расчёта pandapower: {e}")
            messagebox.showerror("Ошибка расчёта", f"Не удалось рассчитать режим:\n{e}\nПроверьте параметры объектов и связей.")

    def save_measurement(self, obj_id, param_type, value):
        if value is None or math.isnan(value):
            return
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO measurements (object_id, param_type, value, timestamp)
            VALUES (?, ?, ?, datetime('now'))
        """, (obj_id, param_type, value))
        conn.commit()
        conn.close()
        update_cached_measurement(obj_id, param_type, value)

    def toggle_edit_mode(self):
        self.scheme_canvas.set_edit_mode(self.edit_mode_var.get())
        self.scheme_canvas.update_idletasks()

    def toggle_line_style(self):
        self.scheme_canvas.use_orthogonal_lines = self.orthogonal_var.get()
        self.refresh_scheme_params()

    def save_scheme_positions(self):
        self.scheme_canvas.save_positions()

    def refresh_scheme(self):
        self.scheme_canvas.generate_layout()
        self.scheme_canvas.update_idletasks()

    def refresh_scheme_params(self):
        self.scheme_canvas.draw_scheme()
        self.scheme_canvas.update_idletasks()
        self.scheme_canvas.configure(scrollregion=self.scheme_canvas.bbox("all"))

    def change_parameter_value(self):
        if self.current_obj_id is None:
            messagebox.showinfo("Информация", "Выберите объект")
            return
        params = get_cached_params(self.current_obj_id)
        if not params:
            return
        dialog = tb.Toplevel(self.root)
        dialog.title("Изменение параметров")
        dialog.geometry("350x300")
        vars_dict = {}
        row = 0
        for param, pdata in params.items():
            tb.Label(dialog, text=param).grid(row=row, column=0, padx=5, pady=5)
            var = tk.DoubleVar(value=pdata['nominal'])
            tb.Entry(dialog, textvariable=var).grid(row=row, column=1, padx=5, pady=5)
            vars_dict[param] = var
            row += 1
        def save():
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            for param, var in vars_dict.items():
                value = var.get()
                cursor.execute("UPDATE params SET nominal=? WHERE object_id=? AND param_type=?",
                               (value, self.current_obj_id, param))
                self.save_measurement(self.current_obj_id, param, value)
                _cache['params'][self.current_obj_id][param]['nominal'] = value
            conn.commit()
            conn.close()
            self.refresh_scheme_params()
            self.load_params_for_object(self.current_obj_id)
            dialog.destroy()
        tb.Button(dialog, text="Сохранить", command=save).grid(row=row, column=0, columnspan=2, pady=15)

    def create_log_tab(self):
        self.log_text = ScrolledText(self.tab_log, height=20, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        tb.Button(self.tab_log, text="Очистить журнал", command=self.clear_log).pack(pady=5)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def add_to_log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)

    def create_sim_tab(self):
        sim_frame = tb.Frame(self.tab_sim)
        sim_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        tb.Label(sim_frame, text="Симуляция аварийных режимов", font=("Segoe UI", 16)).pack(pady=10)
        random_frame = ttk.LabelFrame(sim_frame, text="Случайная авария")
        random_frame.pack(fill=tk.X, pady=10, padx=10)
        tb.Label(random_frame, text="Выбрать случайный объект и случайный параметр с отклонением").pack(pady=5)
        tb.Button(random_frame, text="Запустить случайную симуляцию", command=self.run_random_simulation).pack(pady=5)
        manual_frame = ttk.LabelFrame(sim_frame, text="Ручное задание аварии")
        manual_frame.pack(fill=tk.X, pady=10, padx=10)
        tb.Label(manual_frame, text="Объект:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.sim_obj_var = tk.StringVar()
        self.sim_obj_combo = ttk.Combobox(manual_frame, textvariable=self.sim_obj_var, state="readonly", width=30)
        self.sim_obj_combo.grid(row=0, column=1, padx=5, pady=5)
        tb.Label(manual_frame, text="Параметр:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.sim_param_var = tk.StringVar()
        self.sim_param_combo = ttk.Combobox(manual_frame, textvariable=self.sim_param_var, state="readonly", width=20)
        self.sim_param_combo.grid(row=1, column=1, padx=5, pady=5)
        tb.Label(manual_frame, text="Новое значение:").grid(row=2, column=0, padx=5, pady=5, sticky="e")
        self.sim_value_entry = tb.Entry(manual_frame, width=20)
        self.sim_value_entry.grid(row=2, column=1, padx=5, pady=5)
        tb.Button(manual_frame, text="Вызвать аварию (принудительно)", command=self.manual_emergency).grid(row=3, column=0, columnspan=2, pady=10)
        tb.Button(sim_frame, text="Сбросить все аварии", command=self.reset_emergencies).pack(pady=10)
        self.update_sim_combo()
        self.sim_obj_combo.bind("<<ComboboxSelected>>", self.update_param_combo)

    def update_sim_combo(self):
        objects = get_cached_objects()
        self.sim_obj_combo['values'] = [f"{name} (id={oid})" for oid, name, typ, city, px, py, base_kv, r_ohm, x_ohm in objects]
        if objects:
            self.sim_obj_combo.current(0)
            self.update_param_combo()

    def update_param_combo(self, event=None):
        selected = self.sim_obj_combo.get()
        if not selected:
            return
        obj_id = int(selected.split("id=")[1].rstrip(")"))
        params = get_cached_params(obj_id)
        self.sim_param_combo['values'] = list(params.keys())
        if params:
            self.sim_param_combo.current(0)

    def run_random_simulation(self):
        import random
        objects = get_cached_objects()
        if not objects:
            messagebox.showinfo("Нет объектов", "Добавьте объекты в базу данных.")
            return
        obj_id, obj_name, obj_type, city, px, py = objects[random.randint(0, len(objects)-1)][:6]
        params = get_cached_params(obj_id)
        if not params:
            self.add_to_log(f"Нет параметров у {obj_name}")
            return
        param_type = random.choice(list(params.keys()))
        pdata = params[param_type]
        deviation = random.uniform(-0.3, 0.5)
        new_value = pdata['nominal'] * (1 + deviation)
        self.check_and_create_emergency(obj_id, obj_name, obj_type, param_type, new_value, pdata)

    def manual_emergency(self):
        selected = self.sim_obj_combo.get()
        if not selected:
            messagebox.showinfo("Выбор", "Выберите объект")
            return
        obj_id = int(selected.split("id=")[1].rstrip(")"))
        obj_name = selected.split(" (id=")[0]
        obj_type = self.get_object_type(obj_id)
        param_type = self.sim_param_var.get()
        if not param_type:
            messagebox.showinfo("Выбор", "Выберите параметр")
            return
        try:
            new_value = float(self.sim_value_entry.get())
        except ValueError:
            messagebox.showerror("Ошибка", "Введите числовое значение")
            return
        params = get_cached_params(obj_id)
        if param_type not in params:
            messagebox.showerror("Ошибка", "Параметр не найден")
            return
        pdata = params[param_type]
        self.check_and_create_emergency(obj_id, obj_name, obj_type, param_type, new_value, pdata)

    def get_object_type(self, obj_id):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT type FROM objects WHERE id=?", (obj_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else "Unknown"

    def check_and_create_emergency(self, obj_id, obj_name, obj_type, param_type, new_value, pdata):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            limit_exceeded = False
            limit_value = None
            if pdata['max_allowed'] is not None and new_value > pdata['max_allowed']:
                limit_exceeded = True
                limit_value = pdata['max_allowed']
            if pdata['min_allowed'] is not None and new_value < pdata['min_allowed']:
                limit_exceeded = True
                limit_value = pdata['min_allowed']
            if limit_exceeded:
                cursor.execute("UPDATE objects SET status='emergency', current_emergency=? WHERE id=?", (f"Превышение {param_type}", obj_id))
                desc = f"Авария на {obj_type} '{obj_name}': {param_type} = {new_value:.2f} (предел: {limit_value:.2f} {pdata['unit']})"
                self.add_to_log(desc)
                cursor.execute("INSERT INTO emergency_log (object_id, event_time, param_type, measured_value, limit_value, description) VALUES (?, datetime('now'), ?, ?, ?, ?)",
                               (obj_id, param_type, new_value, limit_value, desc))
                conn.commit()
                self.scheme_canvas.update_node_color(obj_id)
                self.recalculate_network()
                messagebox.showwarning("Авария", f"На объекте {obj_name} произошла авария!\n{desc}")
            else:
                self.add_to_log(f"Измерение на {obj_type} '{obj_name}': {param_type} = {new_value:.2f} {pdata['unit']} (в пределах нормы)")
                self.save_measurement(obj_id, param_type, new_value)
                messagebox.showinfo("Норма", f"На объекте {obj_name} отклонение в пределах нормы.\n{param_type} = {new_value:.2f} {pdata['unit']}")
        except Exception as e:
            conn.rollback()
            self.add_to_log(f"Ошибка БД: {e}")
        finally:
            conn.close()

    def reset_emergencies(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE objects SET status='normal', current_emergency=NULL")
        conn.commit()
        conn.close()
        self.add_to_log("Сброс всех аварий. Все объекты переведены в нормальный режим.")
        self.recalculate_network()
        messagebox.showinfo("Сброс", "Все аварии сброшены.")

    def show_object_info(self, obj_id):
        name = get_object_name(obj_id)
        typ = self.get_object_type(obj_id)
        status = self.scheme_canvas.get_object_status(obj_id)
        city = get_object_city(obj_id)
        messagebox.showinfo("Информация об объекте", f"Название: {name}\nТип: {typ}\nГород: {city}\nСтатус: {status}")

    def get_object_display_info(self, obj_id):
        info = {}
        params = get_cached_params(obj_id)
        for ptype in ['voltage', 'current', 'power_flow', 'power_gen']:
            if ptype in params:
                val = get_cached_measurement(obj_id, ptype)
                info[ptype] = val if val is not None else params[ptype]['nominal']
        return info

    def refresh_object_list(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        objects = get_cached_objects()
        for obj in objects:
            obj_id, name, typ, city, pos_x, pos_y, base_kv, r_ohm, x_ohm = obj
            self.tree.insert("", tk.END, text=name, values=(typ, city or "", base_kv, r_ohm, x_ohm), iid=obj_id)

    def on_object_select(self, event):
        selection = self.tree.selection()
        if not selection:
            return
        self.current_obj_id = int(selection[0])
        self.load_params_for_object(self.current_obj_id)

    def load_params_for_object(self, obj_id):
        for widget in self.param_frame.winfo_children():
            widget.destroy()
        params = get_cached_params(obj_id)
        if not params:
            tb.Label(self.param_frame, text="Нет параметров. Добавьте через кнопку 'Редактировать'.").pack()
            return
        for param_type, pdata in params.items():
            frame = tb.Frame(self.param_frame)
            frame.pack(fill=tk.X, pady=5)
            tb.Label(frame, text=f"{param_type} ({pdata['unit']}):", width=15, anchor="e").pack(side=tk.LEFT, padx=5)
            tb.Label(frame, text=f"Номинал: {pdata['nominal']}").pack(side=tk.LEFT, padx=5)
            limits = []
            if pdata['min_allowed'] is not None:
                limits.append(f"мин: {pdata['min_allowed']}")
            if pdata['max_allowed'] is not None:
                limits.append(f"макс: {pdata['max_allowed']}")
            tb.Label(frame, text=", ".join(limits)).pack(side=tk.LEFT, padx=5)

    def add_object(self):
        self.edit_object(new=True)

    def edit_object(self, new=False):
        if not new and self.current_obj_id is None:
            messagebox.showinfo("Информация", "Сначала выберите объект из списка.")
            return
        obj_id = None if new else self.current_obj_id
        old_name = ""
        old_type = ""
        old_city = ""
        old_base_kv = None
        old_r_ohm = None
        old_x_ohm = None
        old_params = {}
        if not new:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT name, type, city, base_kv, r_ohm, x_ohm FROM objects WHERE id=?", (obj_id,))
            row = cursor.fetchone()
            if row:
                old_name, old_type, old_city, old_base_kv, old_r_ohm, old_x_ohm = row
                old_city = old_city or ""
                old_base_kv = old_base_kv or ""
                old_r_ohm = old_r_ohm or ""
                old_x_ohm = old_x_ohm or ""
            old_params = get_cached_params(obj_id)
            conn.close()
        dialog = tb.Toplevel(self.root)
        dialog.title("Добавить/редактировать объект")
        dialog.geometry("600x750")
        dialog.resizable(False, False)
        row = 0
        tb.Label(dialog, text="Название объекта:").grid(row=row, column=0, padx=10, pady=10, sticky="e")
        name_entry = tb.Entry(dialog, width=30)
        name_entry.insert(0, old_name)
        name_entry.grid(row=row, column=1, padx=10, pady=10, sticky="w")
        row += 1
        tb.Label(dialog, text="Тип объекта:").grid(row=row, column=0, padx=10, pady=10, sticky="e")
        type_var = tk.StringVar(value=old_type)
        type_combo = ttk.Combobox(dialog, textvariable=type_var, values=["Линия", "ГЭС", "Подстанция", "Потребитель", "Line", "GES", "Substation", "Consumer"], state="readonly")
        type_combo.grid(row=row, column=1, padx=10, pady=10, sticky="w")
        row += 1
        tb.Label(dialog, text="Город:").grid(row=row, column=0, padx=10, pady=10, sticky="e")
        city_entry = tb.Entry(dialog, width=30)
        city_entry.insert(0, old_city)
        city_entry.grid(row=row, column=1, padx=10, pady=10, sticky="w")
        row += 1
        tb.Label(dialog, text="Номинальное напряжение (кВ):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        base_kv_entry = tb.Entry(dialog, width=15)
        base_kv_entry.insert(0, str(old_base_kv) if old_base_kv else "")
        base_kv_entry.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1
        tb.Label(dialog, text="Активное сопротивление R (Ом):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        r_ohm_entry = tb.Entry(dialog, width=15)
        r_ohm_entry.insert(0, str(old_r_ohm) if old_r_ohm else "")
        r_ohm_entry.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1
        tb.Label(dialog, text="Реактивное сопротивление X (Ом):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        x_ohm_entry = tb.Entry(dialog, width=15)
        x_ohm_entry.insert(0, str(old_x_ohm) if old_x_ohm else "")
        x_ohm_entry.grid(row=row, column=1, padx=10, pady=5, sticky="w")
        row += 1
        params_frame = ttk.LabelFrame(dialog, text="Параметры (заполните нужные)")
        params_frame.grid(row=row, column=0, columnspan=2, padx=10, pady=10, sticky="ew")
        param_vars = {}
        def update_param_fields(*args):
            for widget in params_frame.winfo_children():
                widget.destroy()
            param_vars.clear()
            obj_type = type_var.get()
            if obj_type == "Линия":
                needed = [("voltage", "Номинальное напряжение (кВ)"),
                          ("current", "Номинальный ток (А)"),
                          ("power_flow", "Максимальный переток (МВт)")]
            elif obj_type == "ГЭС":
                needed = [("voltage", "Номинальное напряжение (кВ)"),
                          ("power_gen", "Номинальная мощность (МВт)")]
            elif obj_type == "Подстанция":
                needed = [("voltage", "Номинальное напряжение (кВ)"),
                          ("power_flow", "Максимальный переток (МВт)")]
            elif obj_type == "Потребитель":
                needed = [("voltage", "Номинальное напряжение (кВ)")]
            else:
                return
            for i, (param, label) in enumerate(needed):
                r = i
                tb.Label(params_frame, text=label).grid(row=r, column=0, padx=5, pady=5, sticky="e")
                var = tk.DoubleVar()
                if param in old_params:
                    var.set(old_params[param]['nominal'])
                entry = tb.Entry(params_frame, textvariable=var, width=15)
                entry.grid(row=r, column=1, padx=5, pady=5, sticky="w")
                param_vars[param] = var
        type_combo.bind("<<ComboboxSelected>>", update_param_fields)
        if old_type:
            update_param_fields()
        row += 1
        def save_changes():
            name = name_entry.get().strip()
            obj_type = type_var.get()
            city = city_entry.get().strip()
            if not name or not obj_type:
                messagebox.showerror("Ошибка", "Заполните название и тип объекта")
                return
            try:
                base_kv = float(base_kv_entry.get()) if base_kv_entry.get() else None
                r_ohm = float(r_ohm_entry.get()) if r_ohm_entry.get() else None
                x_ohm = float(x_ohm_entry.get()) if x_ohm_entry.get() else None
            except:
                base_kv = r_ohm = x_ohm = None
            new_id = save_object_to_db(obj_id if not new else None, name, obj_type, city, None, None, base_kv, r_ohm, x_ohm)
            new_params = {}
            for param, var in param_vars.items():
                nominal = var.get()
                if nominal == 0.0:
                    continue
                if param == "voltage":
                    unit = "кВ"
                    max_allowed = nominal * 1.1
                    min_allowed = nominal * 0.9
                else:
                    unit = "А" if param == "current" else "МВт"
                    max_allowed = nominal * 1.2
                    min_allowed = None
                new_params[param] = {
                    'nominal': nominal,
                    'max_allowed': max_allowed,
                    'min_allowed': min_allowed,
                    'unit': unit
                }
            save_params_for_object(new_id, new_params)
            messagebox.showinfo("Успех", "Данные сохранены")
            dialog.destroy()
            self.refresh_object_list()
            self.update_sim_combo()
            self.refresh_connections_table()
            if new_id == self.current_obj_id:
                self.load_params_for_object(new_id)
            self.refresh_scheme()
            self.recalculate_network()
        tb.Button(dialog, text="Сохранить", command=save_changes).grid(row=row, column=0, columnspan=2, pady=20)

    def delete_object(self):
        if self.current_obj_id is None:
            messagebox.showinfo("Информация", "Сначала выберите объект из списка.")
            return
        if messagebox.askyesno("Подтверждение", "Удалить выбранный объект и все его параметры? Отменить невозможно."):
            delete_object_from_db(self.current_obj_id)
            self.current_obj_id = None
        self.refresh_object_list()
        for widget in self.param_frame.winfo_children():
            widget.destroy()
        self.update_sim_combo()
        self.refresh_connections_table()
        self.refresh_scheme()
        self.recalculate_network()
        messagebox.showinfo("Успех", "Объект удалён")

    def export_db(self):
        target_file = filedialog.asksaveasfilename(
            title="Сохранить базу данных как",
            defaultextension=".db",
            initialfile=os.path.basename(DB_PATH),
            filetypes=[("SQLite DB files", "*.db"), ("All files", "*.*")]
        )
        if not target_file:
            return
        try:
            shutil.copy2(DB_PATH, target_file)
            messagebox.showinfo("Успех", f"База данных экспортирована в:\n{target_file}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось скопировать файл:\n{e}")

    def import_db(self):
        source_file = filedialog.askopenfilename(
            title="Выберите файл БД для импорта",
            filetypes=[("SQLite DB files", "*.db"), ("All files", "*.*")]
        )
        if not source_file:
            return
        if not messagebox.askyesno("Подтверждение", "Импорт заменит текущую БД. Продолжить?"):
            return
        try:
            shutil.copy2(source_file, DB_PATH)
            messagebox.showinfo("Успех", "База данных импортирована. Приложение будет перезапущено.")
            self.root.destroy()
            new_root = tb.Window(themename='darkly')
            App(new_root)
            new_root.mainloop()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось импортировать:\n{e}")

if __name__ == "__main__":
    root = tb.Window(themename='darkly')
    app = App(root)
    root.mainloop()
