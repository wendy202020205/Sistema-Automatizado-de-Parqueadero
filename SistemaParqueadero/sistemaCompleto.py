import mysql.connector
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from datetime import datetime, timedelta
import os
import json
import threading
import time
import re
import sqlite3
import hashlib
from tkcalendar import DateEntry
import configparser
import webbrowser
from fpdf import FPDF
import csv


CONFIG_FILE = "config.ini"
LOCAL_DB = "parqueadero_local.db"
SYNC_INTERVAL = 300  


class Config:
    def __init__(self):
        self.config = configparser.ConfigParser()
        self.load_config()
    
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            self.config.read(CONFIG_FILE)
        else:
            # Configuración por defecto
            self.config['DATABASE'] = {
                'host': 'localhost',
                'user': 'root',
                'password': '',
                'database': 'parqueadero'
            }
            self.config['APP'] = {
                'modo_offline': 'False',
                'ruta_reportes': './reportes',
                'max_espacios': '50'
            }
            self.config['TARIFAS'] = {
                'auto': '2.00',
                'moto': '1.00',
                'camioneta': '3.00'
            }
            self.save_config()
            
            # Crear directorio para reportes
            if not os.path.exists(self.config['APP']['ruta_reportes']):
                os.makedirs(self.config['APP']['ruta_reportes'])
    
    def save_config(self):
        with open(CONFIG_FILE, 'w') as configfile:
            self.config.write(configfile)
    
    def get_mysql_config(self):
        return dict(self.config['DATABASE'])
    
    def get_app_config(self):
        return dict(self.config['APP'])
    
    def get_tarifas(self):
        return dict(self.config['TARIFAS'])
    
    def update_config(self, section, key, value):
        self.config[section][key] = value
        self.save_config()
    
    def toggle_offline_mode(self):
        current = self.config['APP'].getboolean('modo_offline')
        self.config['APP']['modo_offline'] = str(not current)
        self.save_config()
        return not current


class BaseDatos:
    def __init__(self, config):
        self.config = config
        self.conexion = None
        self.conexion_local = None
        self.init_local_db()
        self.pending_sync = []
    
    def init_local_db(self):
        """Inicializa la base de datos SQLite local"""
        self.conexion_local = sqlite3.connect(LOCAL_DB)
        cursor = self.conexion_local.cursor()
        
        # Tabla de usuarios
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            rol TEXT NOT NULL
        )
        ''')
        
        # Tabla de espacios
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS espacios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero INTEGER UNIQUE NOT NULL,
            ocupado BOOLEAN NOT NULL DEFAULT 0
        )
        ''')
        
        # Tabla de vehículos
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS vehiculos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placa VARCHAR(7) NOT NULL,
            tipo TEXT NOT NULL,
            hora_ingreso TIMESTAMP NOT NULL,
            hora_salida TIMESTAMP,
            espacio_asignado INTEGER,
            conductor TEXT,
            estado TEXT DEFAULT 'activo',
            tiempo_estacionado REAL,
            tarifa REAL,
            total_cobrado REAL,
            pendiente_sync BOOLEAN DEFAULT 1
        )
        ''')
        
        # Tabla de reportes
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS reportes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL UNIQUE,
            ingresos INTEGER DEFAULT 0,
            egresos INTEGER DEFAULT 0,
            total_cobrado REAL DEFAULT 0,
            pendiente_sync BOOLEAN DEFAULT 1
        )
        ''')
        
        # Crear usuario admin por defecto si no existe
        cursor.execute("SELECT * FROM usuarios WHERE username = 'admin'")
        if not cursor.fetchone():
            password_hash = hashlib.sha256("admin123".encode()).hexdigest()
            cursor.execute("INSERT INTO usuarios (username, password, rol) VALUES (?, ?, ?)", 
                          ('admin', password_hash, 'administrador'))
        
        # Crear espacios iniciales si no existen
        cursor.execute("SELECT COUNT(*) FROM espacios")
        count = cursor.fetchone()[0]
        
        if count == 0:
            max_espacios = int(self.config.get_app_config().get('max_espacios', 50))
            for i in range(1, max_espacios + 1):
                cursor.execute("INSERT INTO espacios (numero, ocupado) VALUES (?, ?)", (i, False))
        
        self.conexion_local.commit()
    
    def conectar_mysql(self):
        """Conecta con la base de datos MySQL"""
        if self.config.get_app_config().get('modo_offline') == 'True':
            return False
            
        try:
            db_config = self.config.get_mysql_config()
            self.conexion = mysql.connector.connect(
                host=db_config['host'],
                user=db_config['user'],
                password=db_config['password'],
                database=db_config['database']
            )
            return True
        except mysql.connector.Error as err:
            print(f"Error al conectar con MySQL: {err}")
            return False
    
    def _execute_local(self, query, params=None):
        """Ejecuta una consulta en la base de datos local"""
        cursor = self.conexion_local.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return cursor
    
    def _execute_mysql(self, query, params=None):
        """Ejecuta una consulta en la base de datos MySQL"""
        if not self.conectar_mysql():
            return None
            
        cursor = self.conexion.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return cursor
    
    def registrar_ingreso(self, placa, tipo, conductor=None):
        """Registra el ingreso de un vehículo"""
        # Verificar si el vehículo ya está registrado
        cursor = self._execute_local(
            "SELECT * FROM vehiculos WHERE placa = ? AND estado = 'activo'", 
            (placa,)
        )
        vehiculo = cursor.fetchone()
        
        if vehiculo:
            return False, "El vehículo ya está ingresado."
        
        # Buscar espacio libre
        cursor = self._execute_local(
            "SELECT * FROM espacios WHERE ocupado = 0 LIMIT 1"
        )
        espacio = cursor.fetchone()

        if not espacio:
            return False, "No hay espacios disponibles."
        
        espacio_id = espacio[0]
        # Asignar espacio al vehículo
        self._execute_local(
            "UPDATE espacios SET ocupado = 1 WHERE id = ?", 
            (espacio_id,)
        )
        
        # Registrar el vehículo
        hora_ingreso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._execute_local(
            "INSERT INTO vehiculos (placa, tipo, hora_ingreso, espacio_asignado, conductor) VALUES (?, ?, ?, ?, ?)", 
            (placa, tipo, hora_ingreso, espacio_id, conductor)
        )
        
        # Actualizar reporte diario
        fecha = datetime.now().strftime('%Y-%m-%d')
        cursor = self._execute_local("SELECT * FROM reportes WHERE fecha = ?", (fecha,))
        reporte = cursor.fetchone()
        
        if reporte:
            self._execute_local(
                "UPDATE reportes SET ingresos = ingresos + 1 WHERE fecha = ?", 
                (fecha,)
            )
        else:
            self._execute_local(
                "INSERT INTO reportes (fecha, ingresos) VALUES (?, 1)", 
                (fecha,)
            )
        
        self.conexion_local.commit()
        self.add_pending_sync("ingreso", placa)
        return True, f"Vehículo {placa} registrado con éxito. Espacio: {espacio[1]}"
    
    def registrar_salida(self, placa):
        """Registra la salida de un vehículo"""
        cursor = self._execute_local(
            "SELECT * FROM vehiculos WHERE placa = ? AND estado = 'activo'", 
            (placa,)
        )
        vehiculo = cursor.fetchone()
        
        if not vehiculo:
            return False, f"El vehículo con placa {placa} no está registrado."
        
        hora_salida = datetime.now()
        hora_ingreso = datetime.strptime(vehiculo[3], '%Y-%m-%d %H:%M:%S')
        tiempo_estacionado = (hora_salida - hora_ingreso).total_seconds() / 60  # tiempo en minutos
        espacio_id = vehiculo[5]
        
        # Calcular tarifa según tipo de vehículo
        tarifas = self.config.get_tarifas()
        tipo_vehiculo = vehiculo[2]
        tarifa_por_hora = float(tarifas.get(tipo_vehiculo.lower(), 2.00))
        
        # Calcular el total a cobrar (tiempo en horas * tarifa)
        horas = tiempo_estacionado / 60
        if horas < 1:
            horas = 1  # Mínimo una hora
        total_cobrado = horas * tarifa_por_hora
        
        # Actualizar registro del vehículo
        self._execute_local(
            "UPDATE vehiculos SET hora_salida = ?, tiempo_estacionado = ?, estado = 'salido', tarifa = ?, total_cobrado = ? WHERE id = ?", 
            (hora_salida.strftime('%Y-%m-%d %H:%M:%S'), tiempo_estacionado, tarifa_por_hora, total_cobrado, vehiculo[0])
        )
        
        # Liberar el espacio
        self._execute_local(
            "UPDATE espacios SET ocupado = 0 WHERE id = ?", 
            (espacio_id,)
        )
        
        # Actualizar reporte diario
        fecha = datetime.now().strftime('%Y-%m-%d')
        cursor = self._execute_local("SELECT * FROM reportes WHERE fecha = ?", (fecha,))
        reporte = cursor.fetchone()
        
        if reporte:
            self._execute_local(
                "UPDATE reportes SET egresos = egresos + 1, total_cobrado = total_cobrado + ? WHERE fecha = ?", 
                (total_cobrado, fecha)
            )
        else:
            self._execute_local(
                "INSERT INTO reportes (fecha, egresos, total_cobrado) VALUES (?, 1, ?)", 
                (fecha, total_cobrado)
            )
        
        self.conexion_local.commit()
        self.add_pending_sync("salida", placa)
        
        return True, {
            "placa": placa,
            "ingreso": hora_ingreso.strftime('%Y-%m-%d %H:%M:%S'),
            "salida": hora_salida.strftime('%Y-%m-%d %H:%M:%S'),
            "tiempo": f"{tiempo_estacionado:.2f} minutos",
            "tarifa": f"S/.{tarifa_por_hora:.2f}/hora",
            "total": f"S/.{total_cobrado:.2f}"
        }
    
    def get_vehiculos_activos(self):
        """Obtiene la lista de vehículos actualmente en el parqueadero"""
        cursor = self._execute_local("SELECT * FROM vehiculos WHERE estado = 'activo'")
        return cursor.fetchall()
    
    def get_espacios_disponibles(self):
        """Obtiene la cantidad de espacios disponibles"""
        cursor = self._execute_local("SELECT COUNT(*) FROM espacios WHERE ocupado = 0")
        return cursor.fetchone()[0]
    
    def get_reporte_por_fecha(self, fecha):
        """Obtiene el reporte de una fecha específica"""
        cursor = self._execute_local("SELECT * FROM reportes WHERE fecha = ?", (fecha,))
        return cursor.fetchone()
    
    def get_reporte_rango(self, fecha_inicio, fecha_fin):
        """Obtiene reportes en un rango de fechas"""
        cursor = self._execute_local(
            "SELECT * FROM reportes WHERE fecha BETWEEN ? AND ? ORDER BY fecha",
            (fecha_inicio, fecha_fin)
        )
        return cursor.fetchall()
    
    def get_vehiculos_por_fecha(self, fecha):
        """Obtiene los vehículos que entraron en una fecha específica"""
        fecha_inicio = f"{fecha} 00:00:00"
        fecha_fin = f"{fecha} 23:59:59"
        
        cursor = self._execute_local(
            "SELECT * FROM vehiculos WHERE hora_ingreso BETWEEN ? AND ?",
            (fecha_inicio, fecha_fin)
        )
        return cursor.fetchall()
    
    def verificar_usuario(self, username, password):
        """Verifica las credenciales de un usuario"""
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        cursor = self._execute_local(
            "SELECT * FROM usuarios WHERE username = ? AND password = ?",
            (username, password_hash)
        )
        return cursor.fetchone()
    
    def crear_usuario(self, username, password, rol):
        """Crea un nuevo usuario"""
        try:
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            self._execute_local(
                "INSERT INTO usuarios (username, password, rol) VALUES (?, ?, ?)",
                (username, password_hash, rol)
            )
            self.conexion_local.commit()
            return True, "Usuario creado exitosamente"
        except sqlite3.IntegrityError:
            return False, "El nombre de usuario ya existe"
    
    def cambiar_password(self, usuario_id, new_password):
        """Cambia la contraseña de un usuario"""
        password_hash = hashlib.sha256(new_password.encode()).hexdigest()
        self._execute_local(
            "UPDATE usuarios SET password = ? WHERE id = ?",
            (password_hash, usuario_id)
        )
        self.conexion_local.commit()
        return True
    
    def get_usuarios(self):
        """Obtiene la lista de usuarios"""
        cursor = self._execute_local("SELECT id, username, rol FROM usuarios")
        return cursor.fetchall()
    
    def eliminar_usuario(self, usuario_id):
        """Elimina un usuario"""
        try:
            self._execute_local("DELETE FROM usuarios WHERE id = ?", (usuario_id,))
            self.conexion_local.commit()
            return True
        except Exception as e:
            print(f"Error al eliminar usuario: {e}")
            return False
    
    def add_pending_sync(self, tipo, placa):
        """Añade una operación pendiente de sincronización"""
        if self.config.get_app_config().get('modo_offline') == 'True':
            self.pending_sync.append({
                'tipo': tipo,
                'placa': placa,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })
    
    def sincronizar_datos(self):
        """Sincroniza datos entre la base local y MySQL"""
        if not self.conectar_mysql() or not self.pending_sync:
            return False
        
        try:
            for operacion in self.pending_sync:
                if operacion['tipo'] == "ingreso":
                    # Obtener datos del vehículo de la base local
                    cursor = self._execute_local(
                        "SELECT * FROM vehiculos WHERE placa = ? AND estado = 'activo'", 
                        (operacion['placa'],)
                    )
                    vehiculo = cursor.fetchone()
                    
                    if vehiculo:
                        # Insertar en MySQL
                        cursor_mysql = self._execute_mysql(
                            "INSERT INTO vehiculos (placa, tipo, hora_ingreso, espacio_asignado, conductor) VALUES (%s, %s, %s, %s, %s)",
                            (vehiculo[1], vehiculo[2], vehiculo[3], vehiculo[5], vehiculo[6])
                        )
                        self.conexion.commit()
                
                elif operacion['tipo'] == "salida":
                    # Obtener datos del vehículo de la base local
                    cursor = self._execute_local(
                        "SELECT * FROM vehiculos WHERE placa = ? AND estado = 'salido' ORDER BY id DESC LIMIT 1", 
                        (operacion['placa'],)
                    )
                    vehiculo = cursor.fetchone()
                    
                    if vehiculo:
                        # Actualizar en MySQL
                        cursor_mysql = self._execute_mysql(
                            "UPDATE vehiculos SET hora_salida = %s, tiempo_estacionado = %s, estado = 'salido', tarifa = %s, total_cobrado = %s WHERE placa = %s AND estado = 'activo'",
                            (vehiculo[4], vehiculo[8], vehiculo[9], vehiculo[10], vehiculo[1])
                        )
                        self.conexion.commit()
            
            # Marcar como sincronizados en la base local
            self._execute_local("UPDATE vehiculos SET pendiente_sync = 0 WHERE pendiente_sync = 1")
            self._execute_local("UPDATE reportes SET pendiente_sync = 0 WHERE pendiente_sync = 1")
            self.conexion_local.commit()
            
            self.pending_sync = []
            return True
        except Exception as e:
            print(f"Error durante la sincronización: {e}")
            return False
    
    def crear_respaldo(self, archivo):
        """Crea un respaldo de la base de datos local"""
        try:
            # Conectar a la base de datos original
            conn = sqlite3.connect(LOCAL_DB)
            
            # Conectar a la base de datos de respaldo
            backup_conn = sqlite3.connect(archivo)
            
            # Hacer el respaldo
            conn.backup(backup_conn)
            
            # Cerrar conexiones
            backup_conn.close()
            conn.close()
            
            return True
        except Exception as e:
            print(f"Error al crear respaldo: {e}")
            return False
    
    def restaurar_respaldo(self, archivo):
        """Restaura la base de datos desde un respaldo"""
        try:
            # Cerrar conexión actual si existe
            if self.conexion_local:
                self.conexion_local.close()
            
            # Copiar el archivo de respaldo sobre la base de datos actual
            import shutil
            shutil.copyfile(archivo, LOCAL_DB)
            
            # Reconectar
            self.conexion_local = sqlite3.connect(LOCAL_DB)
            
            return True
        except Exception as e:
            print(f"Error al restaurar respaldo: {e}")
            return False
    
    def cerrar(self):
        """Cierra las conexiones a las bases de datos"""
        if self.conexion_local:
            self.conexion_local.close()
        
        if self.conexion:
            self.conexion.close()


class AplicacionParqueadero:
    def __init__(self, root):
        self.root = root
        self.intentos_login = 0
        self.root.title("Sistema de Parqueadero")
        self.root.geometry("1000x600")
        self.root.protocol("WM_DELETE_WINDOW", self.salir)
        
        # Inicialización de configuración y base de datos
        self.config = Config()
        self.db = BaseDatos(self.config)
        
        # Variables de sesión
        self.usuario_actual = None
        self.rol_usuario = None
        
        # Iniciar con pantalla de login
        self.mostrar_login()
        
        # Iniciar hilo de sincronización
        self.sync_thread = threading.Thread(target=self.sincronizacion_periodica, daemon=True)
        self.sync_thread.start()
    
    def mostrar_login(self):
        """Muestra la pantalla de inicio de sesión"""
        # Limpiar ventana
        for widget in self.root.winfo_children():
            widget.destroy()
        
        # Frame de login
        frame_login = ttk.Frame(self.root, padding="20")
        frame_login.pack(expand=True)
        
        # Título
        ttk.Label(frame_login, text="Sistema de Parqueadero", font=("Arial", 18, "bold")).pack(pady=10)
        
        # Campos de login
        ttk.Label(frame_login, text="Usuario:").pack(pady=5)
        self.entry_usuario = ttk.Entry(frame_login, width=30)
        self.entry_usuario.pack(pady=5)
        
        ttk.Label(frame_login, text="Contraseña:").pack(pady=5)
        self.entry_password = ttk.Entry(frame_login, width=30, show="*")
        self.entry_password.pack(pady=5)
        
        # Botón de login
        ttk.Button(frame_login, text="Iniciar Sesión", command=self.login).pack(pady=20)
        
        # Botón modo offline
        if self.config.get_app_config().get('modo_offline') == 'True':
            offline_text = "Modo Offline Activado"
        else:
            offline_text = "Activar Modo Offline"
        
        ttk.Button(frame_login, text=offline_text, command=self.toggle_offline).pack(pady=5)
        
        # Focus en el primer campo
        self.entry_usuario.focus_set()
        
        # Acción al presionar Enter
        self.root.bind("<Return>", lambda event: self.login())
    
    def login(self):
        """Verifica las credenciales e inicia sesión"""
        username = self.entry_usuario.get()
        password = self.entry_password.get()
        
        if not username or not password:
            messagebox.showerror("Error", "Debe completar todos los campos")
            return
        
        usuario = self.db.verificar_usuario(username, password)
        if usuario:
            self.intentos_login = 0
            self.usuario_actual = usuario[0]  # ID del usuario
            self.rol_usuario = usuario[3]     # Rol del usuario
            self.mostrar_dashboard()
        else:
            self.intentos_login += 1
            if self.intentos_login >= 3:
                messagebox.showerror("Bloqueado", "Demasiados intentos. Espere 30 segundos.")
                self.btn_login.config(state="disabled")
                self.root.after(30000, lambda: self.btn_login.config(state="normal"))
                self.intentos_login = 0

            else:
                messagebox.showerror("Error", f"Credenciales incorrectas. Intentos: {self.intentos_login}/3")
    
    def toggle_offline(self):
        """Activa o desactiva el modo offline"""
        is_offline = self.config.toggle_offline_mode()
        if is_offline:
            messagebox.showinfo("Modo Offline", "Modo offline activado. Los datos se sincronizarán cuando vuelva a estar en línea.")
        else:
            messagebox.showinfo("Modo Online", "Modo online activado. Los datos se sincronizarán automáticamente.")
        self.mostrar_login()
    
    def mostrar_dashboard(self):
        """Muestra el panel principal de la aplicación"""
        # Limpiar ventana
        for widget in self.root.winfo_children():
            widget.destroy()
        
        # Frame principal con notebook (pestañas)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        # Crear las pestañas
        self.tab_vehiculos = ttk.Frame(self.notebook)
        self.tab_reportes = ttk.Frame(self.notebook)
        self.tab_config = ttk.Frame(self.notebook)
        
        self.notebook.add(self.tab_vehiculos, text="Gestión de Vehículos")
        self.notebook.add(self.tab_reportes, text="Reportes")
        
        # La pestaña de configuración solo es visible para administradores
        if self.rol_usuario == "administrador":
            self.notebook.add(self.tab_config, text="Configuración")
        
        # Configurar cada pestaña
        self.configurar_tab_vehiculos()
        self.configurar_tab_reportes()
        
        if self.rol_usuario == "administrador":
            self.configurar_tab_config()
        
        # Barra de estado
        self.statusbar = ttk.Label(self.root, text="", relief=tk.SUNKEN, anchor=tk.W)
        self.statusbar.pack(side=tk.BOTTOM, fill=tk.X)
        
        # Actualizar barra de estado
        self.actualizar_estado()
    
    def configurar_tab_vehiculos(self):
        """Configura la pestaña de gestión de vehículos"""
        # Frame izquierdo para registro
        frame_registro = ttk.LabelFrame(self.tab_vehiculos, text="Registro de Vehículos")
        frame_registro.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Frame derecho para vista de ocupación
        frame_ocupacion = ttk.LabelFrame(self.tab_vehiculos, text="Estado del Parqueadero")
        frame_ocupacion.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Campos para ingreso de vehículos
        ttk.Label(frame_registro, text="Placa:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.entry_placa = ttk.Entry(frame_registro, width=15)
        self.entry_placa.grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(frame_registro, text="Tipo de vehículo:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.combo_tipo = ttk.Combobox(frame_registro, width=15, values=["Auto", "Moto", "Camioneta"])
        self.combo_tipo.grid(row=1, column=1, padx=5, pady=5)
        self.combo_tipo.current(0)
        
        ttk.Label(frame_registro, text="Conductor (opcional):").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.entry_conductor = ttk.Entry(frame_registro, width=25)
        self.entry_conductor.grid(row=2, column=1, padx=5, pady=5, columnspan=2)
        
        # Botones de acción
        ttk.Button(frame_registro, text="Registrar Ingreso", command=self.ingresar_vehiculo).grid(
            row=3, column=0, padx=5, pady=20, sticky=tk.W+tk.E)
        ttk.Button(frame_registro, text="Registrar Salida", command=self.salida_vehiculo).grid(
            row=3, column=1, padx=5, pady=20, sticky=tk.W+tk.E)
        
        # Tabla de vehículos actuales
        ttk.Label(frame_registro, text="Vehículos Actualmente en el Parqueadero:", font=("Arial", 10, "bold")).grid(
            row=4, column=0, columnspan=3, padx=5, pady=5, sticky=tk.W)
        
        # Crear tabla con scrollbar
        self.tree_vehiculos = ttk.Treeview(frame_registro, columns=("Placa", "Tipo", "Ingreso", "Espacio", "Conductor"), show="headings")
        self.tree_vehiculos.grid(row=5, column=0, columnspan=3, padx=5, pady=5, sticky=tk.NSEW)
        
        # Configurar scrollbar
        scrollbar = ttk.Scrollbar(frame_registro, orient=tk.VERTICAL, command=self.tree_vehiculos.yview)
        scrollbar.grid(row=5, column=3, sticky=tk.NS)
        self.tree_vehiculos.configure(yscrollcommand=scrollbar.set)
        
        # Configurar columnas
        self.tree_vehiculos.heading("Placa", text="Placa")
        self.tree_vehiculos.heading("Tipo", text="Tipo")
        self.tree_vehiculos.heading("Ingreso", text="Hora de Ingreso")
        self.tree_vehiculos.heading("Espacio", text="Espacio")
        self.tree_vehiculos.heading("Conductor", text="Conductor")
        
        self.tree_vehiculos.column("Placa", width=100)
        self.tree_vehiculos.column("Tipo", width=80)
        self.tree_vehiculos.column("Ingreso", width=150)
        self.tree_vehiculos.column("Espacio", width=60)
        self.tree_vehiculos.column("Conductor", width=150)
        
        # Hacer que las filas se expandan
        frame_registro.grid_rowconfigure(5, weight=1)
        
        # Botón de refrescar lista
        ttk.Button(frame_registro, text="Refrescar Lista", command=self.actualizar_lista_vehiculos).grid(
            row=6, column=0, columnspan=3, padx=5, pady=5, sticky=tk.W+tk.E)
        
        # Visualización de espacios
        ttk.Label(frame_ocupacion, text="Espacios de Estacionamiento", font=("Arial", 10, "bold")).pack(pady=5)
        
        # Frame para los espacios
        self.frame_espacios = ttk.Frame(frame_ocupacion)
        self.frame_espacios.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Inicializar visualización de espacios
        self.actualizar_espacios()
        
        # Cargar datos iniciales
        self.actualizar_lista_vehiculos()
    
    def configurar_tab_reportes(self):
        """Configura la pestaña de reportes"""
        # Frame superior para selección de fechas
        frame_fechas = ttk.Frame(self.tab_reportes)
        frame_fechas.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(frame_fechas, text="Fecha Inicio:").grid(row=0, column=0, padx=5, pady=5)
        self.date_inicio = DateEntry(frame_fechas, width=12, background='darkblue', foreground='white', date_pattern='yyyy-mm-dd')
        self.date_inicio.grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(frame_fechas, text="Fecha Fin:").grid(row=0, column=2, padx=5, pady=5)
        self.date_fin = DateEntry(frame_fechas, width=12, background='darkblue', foreground='white', date_pattern='yyyy-mm-dd')
        self.date_fin.grid(row=0, column=3, padx=5, pady=5)
        
        ttk.Button(frame_fechas, text="Generar Reporte", command=self.generar_reporte).grid(row=0, column=4, padx=20, pady=5)
        ttk.Button(frame_fechas, text="Exportar a PDF", command=self.exportar_pdf).grid(row=0, column=5, padx=5, pady=5)
        ttk.Button(frame_fechas, text="Exportar a CSV", command=self.exportar_csv).grid(row=0, column=6, padx=5, pady=5)
        
        # Frame para el reporte
        frame_reporte = ttk.LabelFrame(self.tab_reportes, text="Reporte")
        frame_reporte.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Tabla de reporte
        self.tree_reporte = ttk.Treeview(frame_reporte, columns=("Fecha", "Ingresos", "Egresos", "Total"), show="headings")
        self.tree_reporte.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Scrollbar para la tabla
        scrollbar = ttk.Scrollbar(frame_reporte, orient=tk.VERTICAL, command=self.tree_reporte.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_reporte.configure(yscrollcommand=scrollbar.set)
        
        # Configurar columnas
        self.tree_reporte.heading("Fecha", text="Fecha")
        self.tree_reporte.heading("Ingresos", text="Ingresos")
        self.tree_reporte.heading("Egresos", text="Egresos")
        self.tree_reporte.heading("Total", text="Total Cobrado (S/.)")
        
        self.tree_reporte.column("Fecha", width=100)
        self.tree_reporte.column("Ingresos", width=80)
        self.tree_reporte.column("Egresos", width=80)
        self.tree_reporte.column("Total", width=120)
        
        # Botón para ver detalles
        ttk.Button(frame_reporte, text="Ver Detalles", command=self.ver_detalles_dia).pack(side=tk.BOTTOM, padx=5, pady=5)
    
    def configurar_tab_config(self):
        """Configura la pestaña de configuración"""
        # Notebook para sub-pestañas de configuración
        config_notebook = ttk.Notebook(self.tab_config)
        config_notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Sub-pestañas
        tab_general = ttk.Frame(config_notebook)
        tab_tarifas = ttk.Frame(config_notebook)
        tab_usuarios = ttk.Frame(config_notebook)
        tab_backup = ttk.Frame(config_notebook)
        
        config_notebook.add(tab_general, text="General")
        config_notebook.add(tab_tarifas, text="Tarifas")
        config_notebook.add(tab_usuarios, text="Usuarios")
        config_notebook.add(tab_backup, text="Respaldo")
        
        # Configuración General
        ttk.Label(tab_general, text="Configuración General", font=("Arial", 12, "bold")).pack(pady=10)
        
        # Frame para configuraciones
        frame_config = ttk.Frame(tab_general)
        frame_config.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        # Base de datos
        ttk.Label(frame_config, text="Configuración de Base de Datos", font=("Arial", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=10)
        
        ttk.Label(frame_config, text="Host:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_host = ttk.Entry(frame_config, width=30)
        self.entry_host.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_host.insert(0, self.config.get_mysql_config()['host'])
        
        ttk.Label(frame_config, text="Usuario:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_dbuser = ttk.Entry(frame_config, width=30)
        self.entry_dbuser.grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_dbuser.insert(0, self.config.get_mysql_config()['user'])
        
        ttk.Label(frame_config, text="Contraseña:").grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_dbpass = ttk.Entry(frame_config, width=30, show="*")
        self.entry_dbpass.grid(row=3, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_dbpass.insert(0, self.config.get_mysql_config()['password'])
        
        ttk.Label(frame_config, text="Base de datos:").grid(row=4, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_dbname = ttk.Entry(frame_config, width=30)
        self.entry_dbname.grid(row=4, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_dbname.insert(0, self.config.get_mysql_config()['database'])
        
        # Configuración de la aplicación
        ttk.Label(frame_config, text="Configuración de la Aplicación", font=("Arial", 10, "bold")).grid(
            row=5, column=0, columnspan=2, sticky=tk.W, pady=10)
        
        ttk.Label(frame_config, text="Máximo de espacios:").grid(row=6, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_maxespacios = ttk.Entry(frame_config, width=10)
        self.entry_maxespacios.grid(row=6, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_maxespacios.insert(0, self.config.get_app_config()['max_espacios'])
        
        ttk.Label(frame_config, text="Ruta de reportes:").grid(row=7, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_rutareportes = ttk.Entry(frame_config, width=30)
        self.entry_rutareportes.grid(row=7, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_rutareportes.insert(0, self.config.get_app_config()['ruta_reportes'])
        
        # Botón para guardar configuración
        ttk.Button(tab_general, text="Guardar Configuración", command=self.guardar_config_general).pack(pady=20)
        
        # Configuración de Tarifas
        ttk.Label(tab_tarifas, text="Configuración de Tarifas", font=("Arial", 12, "bold")).pack(pady=10)
        
        # Frame para tarifas
        frame_tarifas = ttk.Frame(tab_tarifas)
        frame_tarifas.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        tarifas = self.config.get_tarifas()
        
        ttk.Label(frame_tarifas, text="Tarifa por hora para Autos (S/.):").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_auto = ttk.Entry(frame_tarifas, width=10)
        self.entry_tarifa_auto.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_auto.insert(0, tarifas.get('auto', '2.00'))
        
        ttk.Label(frame_tarifas, text="Tarifa por hora para Motos (S/.):").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_moto = ttk.Entry(frame_tarifas, width=10)
        self.entry_tarifa_moto.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_moto.insert(0, tarifas.get('moto', '1.00'))
        
        ttk.Label(frame_tarifas, text="Tarifa por hora para Camionetas (S/.):").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_camioneta = ttk.Entry(frame_tarifas, width=10)
        self.entry_tarifa_camioneta.grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_camioneta.insert(0, tarifas.get('camioneta', '3.00'))
        
        # Botón para guardar tarifas
        ttk.Button(tab_tarifas, text="Guardar Tarifas", command=self.guardar_tarifas).pack(pady=20)
        
        # Configuración de Usuarios
        ttk.Label(tab_usuarios, text="Gestión de Usuarios", font=("Arial", 12, "bold")).pack(pady=10)
        
        # Frame para crear usuario
        frame_crear_usuario = ttk.LabelFrame(tab_usuarios, text="Crear Nuevo Usuario")
        frame_crear_usuario.pack(fill=tk.X, padx=20, pady=10)
        
        ttk.Label(frame_crear_usuario, text="Nombre de usuario:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_nuevo_usuario = ttk.Entry(frame_crear_usuario, width=20)
        self.entry_nuevo_usuario.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        
        ttk.Label(frame_crear_usuario, text="Contraseña:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_nuevo_password = ttk.Entry(frame_crear_usuario, width=20, show="*")
        self.entry_nuevo_password.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        
        ttk.Label(frame_crear_usuario, text="Rol:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        self.combo_rol = ttk.Combobox(frame_crear_usuario, width=20, values=["administrador", "operador"])
        self.combo_rol.grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)
        self.combo_rol.current(1)  # Operador por defecto
        
        ttk.Button(frame_crear_usuario, text="Crear Usuario", command=self.crear_nuevo_usuario).grid(
            row=3, column=0, columnspan=2, pady=10)
        
        # Lista de usuarios
        frame_lista_usuarios = ttk.LabelFrame(tab_usuarios, text="Usuarios Existentes")
        frame_lista_usuarios.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        # Tabla de usuarios
        self.tree_usuarios = ttk.Treeview(frame_lista_usuarios, columns=("ID", "Usuario", "Rol"), show="headings")
        self.tree_usuarios.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Scrollbar para la tabla
        scrollbar = ttk.Scrollbar(frame_lista_usuarios, orient=tk.VERTICAL, command=self.tree_usuarios.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_usuarios.configure(yscrollcommand=scrollbar.set)
        
        # Configurar columnas
        self.tree_usuarios.heading("ID", text="ID")
        self.tree_usuarios.heading("Usuario", text="Usuario")
        self.tree_usuarios.heading("Rol", text="Rol")
        
        self.tree_usuarios.column("ID", width=50)
        self.tree_usuarios.column("Usuario", width=150)
        self.tree_usuarios.column("Rol", width=100)
        
        # Botones para gestionar usuarios
        frame_botones_usuarios = ttk.Frame(tab_usuarios)
        frame_botones_usuarios.pack(fill=tk.X, padx=20, pady=10)
        
        ttk.Button(frame_botones_usuarios, text="Refrescar Lista", command=self.cargar_usuarios).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame_botones_usuarios, text="Cambiar Contraseña", command=self.cambiar_password_usuario).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame_botones_usuarios, text="Eliminar Usuario", command=self.eliminar_usuario).pack(side=tk.LEFT, padx=5)
        
        # Cargar lista de usuarios
        self.cargar_usuarios()
        
        # Configuración de Respaldo
        ttk.Label(tab_backup, text="Respaldo y Restauración", font=("Arial", 12, "bold")).pack(pady=10)
        
        # Frame para respaldo
        frame_backup = ttk.Frame(tab_backup)
        frame_backup.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        ttk.Label(frame_backup, text="Realizar respaldo de la base de datos local:").grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=10)
        
        ttk.Button(frame_backup, text="Crear Respaldo", command=self.crear_respaldo).grid(
            row=1, column=0, sticky=tk.W, padx=5, pady=5)
        
        ttk.Label(frame_backup, text="Restaurar desde respaldo:").grid(
            row=2, column=0, columnspan=2, sticky=tk.W, pady=10)
        
        ttk.Button(frame_backup, text="Restaurar Respaldo", command=self.restaurar_respaldo).grid(
            row=3, column=0, sticky=tk.W, padx=5, pady=5)
    
    def ingresar_vehiculo(self):
        """Procesa el ingreso de un nuevo vehículo"""
        placa = self.entry_placa.get().upper()
        tipo = self.combo_tipo.get()
        conductor = self.entry_conductor.get() or None
        
        if not placa:
            messagebox.showerror("Error", "Debe ingresar la placa del vehículo")
            return
        
        if not re.match(r'^([A-Z]{3}-\d{3}|[A-Z]{2}-\d{4}|[A-Z0-9]{3}-\d{3})$', placa):
            messagebox.showerror("Error", "Formato de placa inválido. Ej: ABC-123, AB-1234, M1A-234")
            return

        result, mensaje = self.db.registrar_ingreso(placa, tipo, conductor)
        
        if result:
            messagebox.showinfo("Éxito", mensaje)
            # Limpiar campos
            self.entry_placa.delete(0, tk.END)
            self.entry_conductor.delete(0, tk.END)
            self.combo_tipo.current(0)
            # Actualizar lista y espacios
            self.actualizar_lista_vehiculos()
            self.actualizar_espacios()
            self.actualizar_estado()
            # Generar ticket
            self.generar_ticket_ingreso(placa, tipo, conductor)
        else:
            messagebox.showerror("Error", mensaje)
    
    def salida_vehiculo(self):
        """Procesa la salida de un vehículo"""
        placa = self.entry_placa.get().upper()
        
        if not placa:
            messagebox.showerror("Error", "Debe ingresar la placa del vehículo")
            return
        
        result, datos = self.db.registrar_salida(placa)
        
        if result:
            messagebox.showinfo("Éxito", f"Vehículo {placa} ha salido del parqueadero")
            # Mostrar detalles de facturación
            self.mostrar_factura(datos)
            # Limpiar campos
            self.entry_placa.delete(0, tk.END)
            # Actualizar lista y espacios
            self.actualizar_lista_vehiculos()
            self.actualizar_espacios()
            self.actualizar_estado()
        else:
            messagebox.showerror("Error", datos)
    
    def mostrar_factura(self, datos):
        """Muestra la ventana de facturación"""
        ventana_factura = tk.Toplevel(self.root)
        ventana_factura.title("Comprobante de Salida")
        ventana_factura.geometry("400x300")
        ventana_factura.transient(self.root)
        ventana_factura.grab_set()
        
        # Frame para la factura
        frame_factura = ttk.Frame(ventana_factura, padding="20")
        frame_factura.pack(fill=tk.BOTH, expand=True)
        
        # Título
        ttk.Label(frame_factura, text="COMPROBANTE DE PAGO", font=("Arial", 14, "bold")).pack(pady=10)
        
        # Detalles
        ttk.Label(frame_factura, text=f"Placa: {datos['placa']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Hora de ingreso: {datos['ingreso']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Hora de salida: {datos['salida']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Tiempo de permanencia: {datos['tiempo']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Tarifa: {datos['tarifa']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Total a pagar: {datos['total']}", font=("Arial", 14, "bold")).pack(anchor=tk.W, pady=10)
        
        # Botones
        frame_botones = ttk.Frame(frame_factura)
        frame_botones.pack(fill=tk.X, pady=10)
        
        ttk.Button(frame_botones, text="Imprimir", command=lambda: self.imprimir_factura(datos)).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame_botones, text="Cerrar", command=ventana_factura.destroy).pack(side=tk.RIGHT, padx=5)
    
    def imprimir_factura(self, datos):
        """Genera un PDF con la factura"""
        try:
            # Crear directorio para comprobantes si no existe
            dir_comprobantes = os.path.join(self.config.get_app_config()['ruta_reportes'], 'comprobantes')
            if not os.path.exists(dir_comprobantes):
                os.makedirs(dir_comprobantes)
            
            # Generar nombre del archivo
            fecha_actual = datetime.now().strftime('%Y%m%d%H%M%S')
            archivo = os.path.join(dir_comprobantes, f"comprobante_{datos['placa']}_{fecha_actual}.pdf")
            
            # Crear PDF
            pdf = FPDF()
            pdf.add_page()
            
            # Encabezado
            pdf.set_font('Arial', 'B', 16)
            pdf.cell(190, 10, 'COMPROBANTE DE PAGO', 0, 1, 'C')
            pdf.ln(10)
            
            # Detalles
            pdf.set_font('Arial', '', 12)
            pdf.cell(190, 10, f"Placa: {datos['placa']}", 0, 1)
            pdf.cell(190, 10, f"Hora de ingreso: {datos['ingreso']}", 0, 1)
            pdf.cell(190, 10, f"Hora de salida: {datos['salida']}", 0, 1)
            pdf.cell(190, 10, f"Tiempo de permanencia: {datos['tiempo']}", 0, 1)
            pdf.cell(190, 10, f"Tarifa: {datos['tarifa']}", 0, 1)
            
            # Total
            pdf.ln(5)
            pdf.set_font('Arial', 'B', 14)
            pdf.cell(190, 10, f"Total a pagar: {datos['total']}", 0, 1)
            
            # Pie de página
            pdf.ln(20)
            pdf.set_font('Arial', 'I', 10)
            pdf.cell(190, 10, 'Gracias por utilizar nuestro servicio de parqueadero', 0, 1, 'C')
            
            # Guardar PDF
            pdf.output(archivo)
            
            # Abrir el PDF
            webbrowser.open(archivo)
            
        except Exception as e:
            messagebox.showerror("Error", f"Error al generar el comprobante: {str(e)}")
    
    def generar_ticket_ingreso(self, placa, tipo, conductor):
        """Genera un ticket de ingreso"""
        try:
            # Crear directorio para tickets si no existe
            dir_tickets = os.path.join(self.config.get_app_config()['ruta_reportes'], 'tickets')
            if not os.path.exists(dir_tickets):
                os.makedirs(dir_tickets)
            
            # Obtener fecha y hora actual
            fecha_hora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            fecha_archivo = datetime.now().strftime('%Y%m%d%H%M%S')
            
            # Generar nombre del archivo
            archivo = os.path.join(dir_tickets, f"ticket_{placa}_{fecha_archivo}.pdf")
            
            # Crear PDF
            pdf = FPDF()
            pdf.add_page()
            
            # Encabezado
            pdf.set_font('Arial', 'B', 16)
            pdf.cell(190, 10, 'TICKET DE INGRESO', 0, 1, 'C')
            pdf.ln(10)
            
            # Detalles
            pdf.set_font('Arial', '', 12)
            pdf.cell(190, 10, f"Placa: {placa}", 0, 1)
            pdf.cell(190, 10, f"Tipo de vehículo: {tipo}", 0, 1)
            if conductor:
                pdf.cell(190, 10, f"Conductor: {conductor}", 0, 1)
            pdf.cell(190, 10, f"Fecha y hora de ingreso: {fecha_hora}", 0, 1)
            
            # Pie de página
            pdf.ln(20)
            pdf.set_font('Arial', 'I', 10)
            pdf.cell(190, 10, 'Conserve este ticket para la salida del vehículo', 0, 1, 'C')
            
            # Guardar PDF
            pdf.output(archivo)
            
            # Abrir el PDF
            webbrowser.open(archivo)
            
        except Exception as e:
            messagebox.showerror("Error", f"Error al generar el ticket: {str(e)}")
    
    def actualizar_lista_vehiculos(self):
        """Actualiza la lista de vehículos en el parqueadero"""
        # Limpiar lista actual
        for item in self.tree_vehiculos.get_children():
            self.tree_vehiculos.delete(item)
        
        # Obtener vehículos activos
        vehiculos = self.db.get_vehiculos_activos()
        
        # Llenar la tabla
        for vehiculo in vehiculos:
            # Formatear datos para la tabla
            id_vehiculo = vehiculo[0]
            placa = vehiculo[1]
            tipo = vehiculo[2]
            hora_ingreso = vehiculo[3]
            espacio = vehiculo[5]
            conductor = vehiculo[6] or ""
            
            self.tree_vehiculos.insert("", tk.END, values=(placa, tipo, hora_ingreso, espacio, conductor))
    
    def actualizar_espacios(self):
        """Actualiza la visualización de espacios"""
        # Limpiar frame de espacios
        for widget in self.frame_espacios.winfo_children():
            widget.destroy()
        
        # Obtener configuración
        max_espacios = int(self.config.get_app_config().get('max_espacios', 50))
        
        # Consultar espacios ocupados
        cursor = self.db._execute_local("SELECT id, numero, ocupado FROM espacios ORDER BY numero")
        espacios = cursor.fetchall()
        
        # Crear botones para cada espacio
        row, col = 0, 0
        for espacio in espacios:
            id_espacio, numero, ocupado = espacio
            
            # Color según estado
            color = "#FF6B6B" if ocupado else "#4CAF50"
            
            # Crear botón
            btn = tk.Button(self.frame_espacios, text=str(numero), width=4, height=2, bg=color)
            btn.grid(row=row, column=col, padx=2, pady=2)
            
            # Actualizar posición para el siguiente botón
            col += 1
            if col > 9:  # 10 columnas máximo
                col = 0
                row += 1
    
    def actualizar_estado(self):
        """Actualiza la barra de estado"""
        espacios_disponibles = self.db.get_espacios_disponibles()
        total_espacios = int(self.config.get_app_config().get('max_espacios', 50))
        
        # Obtener modo (online/offline)
        modo = "OFFLINE" if self.config.get_app_config().get('modo_offline') == 'True' else "ONLINE"
        
        # Actualizar texto de la barra de estado
        self.statusbar.config(text=f" Modo: {modo} | Espacios disponibles: {espacios_disponibles}/{total_espacios} | Usuario: {self.rol_usuario}")
    
    def generar_reporte(self):
        """Genera un reporte en el rango de fechas seleccionado"""
        fecha_inicio = self.date_inicio.get()
        fecha_fin = self.date_fin.get()
        
        if not fecha_inicio or not fecha_fin:
            messagebox.showerror("Error", "Debe seleccionar el rango de fechas")
            return
        
        # Validar fechas
        try:
            datetime.strptime(fecha_inicio, '%Y-%m-%d')
            datetime.strptime(fecha_fin, '%Y-%m-%d')
        except ValueError:
            messagebox.showerror("Error", "Formato de fecha inválido")
            return
        
        # Limpiar tabla actual
        for item in self.tree_reporte.get_children():
            self.tree_reporte.delete(item)
        
        # Obtener reportes
        reportes = self.db.get_reporte_rango(fecha_inicio, fecha_fin)
        
        if not reportes:
            messagebox.showinfo("Información", "No hay reportes en el rango de fechas seleccionado")
            return
        
        # Llenar la tabla
        total_general = 0
        for reporte in reportes:
            id_reporte, fecha, ingresos, egresos, total, _ = reporte
            total_general += total if total else 0
            
            self.tree_reporte.insert("", tk.END, values=(fecha, ingresos, egresos, f"s/.{total:.2f}" if total else "s/.0.00"))
        
        # Añadir fila de total general
        self.tree_reporte.insert("", tk.END, values=("TOTAL", "", "", f"s/.{total_general:.2f}"))
    
    def ver_detalles_dia(self):
        """Muestra los detalles de los vehículos en un día específico"""
        # Obtener el ítem seleccionado
        seleccion = self.tree_reporte.selection()
        if not seleccion:
            messagebox.showerror("Error", "Debe seleccionar un día del reporte")
            return
        
        # Obtener la fecha del ítem seleccionado
        valores = self.tree_reporte.item(seleccion, 'values')
        fecha = valores[0]
        
        # Verificar si es la fila de total
        if fecha == "TOTAL":
            messagebox.showerror("Error", "Debe seleccionar un día específico, no el total")
            return
        
        # Obtener vehículos para esa fecha
        vehiculos = self.db.get_vehiculos_por_fecha(fecha)
        
        if not vehiculos:
            messagebox.showinfo("Información", f"No hay vehículos registrados para el {fecha}")
            return
        
        # Crear ventana de detalles
        ventana_detalles = tk.Toplevel(self.root)
        ventana_detalles.title(f"Detalles del {fecha}")
        ventana_detalles.geometry("800x400")
        
        # Frame principal
        frame_principal = ttk.Frame(ventana_detalles)
        frame_principal.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Tabla de vehículos
        tree_vehiculos = ttk.Treeview(frame_principal, columns=("Placa", "Tipo", "Ingreso", "Salida", "Espacio", "Conductor", "Total"), show="headings")
        tree_vehiculos.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # Scrollbar
        scrollbar = ttk.Scrollbar(frame_principal, orient=tk.VERTICAL, command=tree_vehiculos.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree_vehiculos.configure(yscrollcommand=scrollbar.set)
        
        # Configurar columnas
        tree_vehiculos.heading("Placa", text="Placa")
        tree_vehiculos.heading("Tipo", text="Tipo")
        tree_vehiculos.heading("Ingreso", text="Hora Ingreso")
        tree_vehiculos.heading("Salida", text="Hora Salida")
        tree_vehiculos.heading("Espacio", text="Espacio")
        tree_vehiculos.heading("Conductor", text="Conductor")
        tree_vehiculos.heading("Total", text="Total")
        
        tree_vehiculos.column("Placa", width=80)
        tree_vehiculos.column("Tipo", width=60)
        tree_vehiculos.column("Ingreso", width=120)
        tree_vehiculos.column("Salida", width=120)
        tree_vehiculos.column("Espacio", width=60)
        tree_vehiculos.column("Conductor", width=150)
        tree_vehiculos.column("Total", width=80)
        
        # Llenar la tabla
        for vehiculo in vehiculos:
            placa = vehiculo[1]
            tipo = vehiculo[2]
            ingreso = vehiculo[3]
            salida = vehiculo[4] if vehiculo[4] else ""
            espacio = vehiculo[5] if vehiculo[5] else ""
            conductor = vehiculo[6] if vehiculo[6] else ""
            total = f"S/.{vehiculo[10]:.2f}" if vehiculo[10] else ""
            
            tree_vehiculos.insert("", tk.END, values=(placa, tipo, ingreso, salida, espacio, conductor, total))
    
    def exportar_pdf(self):
        """Exporta el reporte actual a PDF"""
        if not self.tree_reporte.get_children():
            messagebox.showerror("Error", "No hay datos para exportar")
            return
        
        fecha_inicio = self.date_inicio.get()
        fecha_fin = self.date_fin.get()
        
        try:
            # Crear directorio para reportes si no existe
            ruta_reportes = self.config.get_app_config()['ruta_reportes']
            if not os.path.exists(ruta_reportes):
                os.makedirs(ruta_reportes)
            
            # Generar nombre del archivo
            fecha_actual = datetime.now().strftime('%Y%m%d%H%M%S')
            archivo = os.path.join(ruta_reportes, f"reporte_{fecha_inicio}_{fecha_fin}_{fecha_actual}.pdf")
            
            # Crear PDF
            pdf = FPDF()
            pdf.add_page()
            
            # Encabezado
            pdf.set_font('Arial', 'B', 16)
            pdf.cell(190, 10, 'REPORTE DE PARQUEADERO', 0, 1, 'C')
            pdf.set_font('Arial', '', 12)
            pdf.cell(190, 10, f"Período: {fecha_inicio} - {fecha_fin}", 0, 1, 'C')
            pdf.ln(10)
            
            # Cabecera de la tabla
            pdf.set_font('Arial', 'B', 12)
            pdf.cell(47.5, 10, 'Fecha', 1, 0, 'C')
            pdf.cell(47.5, 10, 'Ingresos', 1, 0, 'C')
            pdf.cell(47.5, 10, 'Egresos', 1, 0, 'C')
            pdf.cell(47.5, 10, 'Total Cobrado', 1, 1, 'C')
            
            # Contenido de la tabla
            pdf.set_font('Arial', '', 12)
            for item in self.tree_reporte.get_children():
                valores = self.tree_reporte.item(item, 'values')
                pdf.cell(47.5, 10, str(valores[0]), 1, 0, 'C')
                pdf.cell(47.5, 10, str(valores[1]), 1, 0, 'C')
                pdf.cell(47.5, 10, str(valores[2]), 1, 0, 'C')
                pdf.cell(47.5, 10, str(valores[3]), 1, 1, 'C')
            
            # Pie de página
            pdf.ln(10)
            pdf.set_font('Arial', 'I', 10)
            pdf.cell(190, 10, f"Generado el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 0, 1, 'R')
            
            # Guardar PDF
            pdf.output(archivo)
            
            # Abrir el PDF
            webbrowser.open(archivo)
            
            messagebox.showinfo("Éxito", f"Reporte exportado como {archivo}")
            
        except Exception as e:
            messagebox.showerror("Error", f"Error al exportar el reporte: {str(e)}")
    
    def exportar_csv(self):
        """Exporta el reporte actual a CSV"""
        if not self.tree_reporte.get_children():
            messagebox.showerror("Error", "No hay datos para exportar")
            return
        
        fecha_inicio = self.date_inicio.get()
        fecha_fin = self.date_fin.get()
        
        try:
            # Crear directorio para reportes si no existe
            ruta_reportes = self.config.get_app_config()['ruta_reportes']
            if not os.path.exists(ruta_reportes):
                os.makedirs(ruta_reportes)
            
            # Generar nombre del archivo
            fecha_actual = datetime.now().strftime('%Y%m%d%H%M%S')
            archivo = os.path.join(ruta_reportes, f"reporte_{fecha_inicio}_{fecha_fin}_{fecha_actual}.csv")
            
            # Crear archivo CSV
            with open(archivo, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                
                # Escribir encabezado
                writer.writerow(['Fecha', 'Ingresos', 'Egresos', 'Total Cobrado'])
                
                # Escribir datos
                for item in self.tree_reporte.get_children():
                    valores = self.tree_reporte.item(item, 'values')
                    # Limpiar el formato de moneda si es necesario
                    total = valores[3].replace('s/.', '') if valores[3].startswith('s/.') else valores[3]
                    writer.writerow([valores[0], valores[1], valores[2], total])
            
            messagebox.showinfo("Éxito", f"Reporte exportado como {archivo}")
            
        except Exception as e:
            messagebox.showerror("Error", f"Error al exportar el reporte: {str(e)}")
    
    def guardar_config_general(self):
        """Guarda la configuración general"""
        try:
            # Actualizar configuración de base de datos
            self.config.update_config('DATABASE', 'host', self.entry_host.get())
            self.config.update_config('DATABASE', 'user', self.entry_dbuser.get())
            self.config.update_config('DATABASE', 'password', self.entry_dbpass.get())
            self.config.update_config('DATABASE', 'database', self.entry_dbname.get())
            
            # Actualizar configuración de la aplicación
            max_espacios = int(self.entry_maxespacios.get())
            if max_espacios <= 0:
                raise ValueError("El número de espacios debe ser mayor a cero")
            
            self.config.update_config('APP', 'max_espacios', str(max_espacios))
            self.config.update_config('APP', 'ruta_reportes', self.entry_rutareportes.get())
            
            messagebox.showinfo("Éxito", "Configuración guardada correctamente")
            
            # Actualizar espacios si es necesario
            self.actualizar_espacios()
            self.actualizar_estado()
            
        except ValueError as e:
            messagebox.showerror("Error", f"Valor inválido: {str(e)}")
        except Exception as e:
            messagebox.showerror("Error", f"Error al guardar configuración: {str(e)}")
    
    def guardar_tarifas(self):
        """Guarda las tarifas en la configuración"""
        try:
            # Validar y guardar tarifas
            tarifa_auto = float(self.entry_tarifa_auto.get())
            tarifa_moto = float(self.entry_tarifa_moto.get())
            tarifa_camioneta = float(self.entry_tarifa_camioneta.get())
            
            if tarifa_auto <= 0 or tarifa_moto <= 0 or tarifa_camioneta <= 0:
                raise ValueError("Las tarifas deben ser mayores a cero")
            
            self.config.update_config('TARIFAS', 'auto', f"{tarifa_auto:.2f}")
            self.config.update_config('TARIFAS', 'moto', f"{tarifa_moto:.2f}")
            self.config.update_config('TARIFAS', 'camioneta', f"{tarifa_camioneta:.2f}")
            
            messagebox.showinfo("Éxito", "Tarifas actualizadas correctamente")
            
        except ValueError as e:
            messagebox.showerror("Error", f"Valor inválido: {str(e)}")
        except Exception as e:
            messagebox.showerror("Error", f"Error al guardar tarifas: {str(e)}")
    
    def cargar_usuarios(self):
        """Carga la lista de usuarios en la tabla"""
        # Limpiar tabla
        for item in self.tree_usuarios.get_children():
            self.tree_usuarios.delete(item)
        
        # Obtener usuarios
        usuarios = self.db.get_usuarios()
        
        # Llenar tabla
        for usuario in usuarios:
            self.tree_usuarios.insert("", tk.END, values=(usuario[0], usuario[1], usuario[2]))
    
    def crear_nuevo_usuario(self):
        """Crea un nuevo usuario"""
        username = self.entry_nuevo_usuario.get()
        password = self.entry_nuevo_password.get()
        rol = self.combo_rol.get()
        
        if not username or not password:
            messagebox.showerror("Error", "Debe completar todos los campos")
            return
        
        if len(password) < 6:
            messagebox.showerror("Error", "La contraseña debe tener al menos 6 caracteres")
            return
        
        result, mensaje = self.db.crear_usuario(username, password, rol)
        
        if result:
            messagebox.showinfo("Éxito", mensaje)
            # Limpiar campos
            self.entry_nuevo_usuario.delete(0, tk.END)
            self.entry_nuevo_password.delete(0, tk.END)
            # Actualizar lista
            self.cargar_usuarios()
        else:
            messagebox.showerror("Error", mensaje)
    
    def cambiar_password_usuario(self):
        """Cambia la contraseña de un usuario seleccionado"""
        seleccion = self.tree_usuarios.selection()
        if not seleccion:
            messagebox.showerror("Error", "Debe seleccionar un usuario")
            return
        
        # Obtener ID del usuario seleccionado
        usuario_id = self.tree_usuarios.item(seleccion, 'values')[0]
        
        # Pedir nueva contraseña
        nueva_password = simpledialog.askstring("Cambiar contraseña", "Ingrese la nueva contraseña:", show='*')
        if not nueva_password:
            return
        
        if len(nueva_password) < 6:
            messagebox.showerror("Error", "La contraseña debe tener al menos 6 caracteres")
            return
        
        # Confirmar cambio
        if messagebox.askyesno("Confirmar", "¿Está seguro de cambiar la contraseña de este usuario?"):
            if self.db.cambiar_password(usuario_id, nueva_password):
                messagebox.showinfo("Éxito", "Contraseña cambiada correctamente")
            else:
                messagebox.showerror("Error", "No se pudo cambiar la contraseña")
    
    def eliminar_usuario(self):
        """Elimina un usuario seleccionado"""
        seleccion = self.tree_usuarios.selection()
        if not seleccion:
            messagebox.showerror("Error", "Debe seleccionar un usuario")
            return
        
        # Obtener datos del usuario seleccionado
        usuario_id, username, rol = self.tree_usuarios.item(seleccion, 'values')
        
        # No permitir eliminar al usuario actual
        if int(usuario_id) == self.usuario_actual:
            messagebox.showerror("Error", "No puede eliminarse a sí mismo")
            return
        
        # Confirmar eliminación
        if messagebox.askyesno("Confirmar", f"¿Está seguro de eliminar al usuario {username} ({rol})?"):
            if self.db.eliminar_usuario(usuario_id):
                messagebox.showinfo("Éxito", "Usuario eliminado correctamente")
                self.cargar_usuarios()
            else:
                messagebox.showerror("Error", "No se pudo eliminar el usuario")
    
    def crear_respaldo(self):
        """Crea un respaldo de la base de datos local"""
        try:
            # Pedir ubicación para guardar el respaldo
            from tkinter import filedialog
            archivo = filedialog.asksaveasfilename(
                defaultextension=".db",
                filetypes=[("Archivos de base de datos", "*.db"), ("Todos los archivos", "*.*")],
                title="Guardar respaldo como"
            )
            
            if not archivo:
                return  # Usuario canceló
            
            if self.db.crear_respaldo(archivo):
                messagebox.showinfo("Éxito", f"Respaldo creado correctamente en {archivo}")
            else:
                messagebox.showerror("Error", "No se pudo crear el respaldo")
                
        except Exception as e:
            messagebox.showerror("Error", f"Error al crear respaldo: {str(e)}")
    
    def restaurar_respaldo(self):
        """Restaura la base de datos desde un respaldo"""
        try:
            # Pedir ubicación del archivo de respaldo
            from tkinter import filedialog
            archivo = filedialog.askopenfilename(
                filetypes=[("Archivos de base de datos", "*.db"), ("Todos los archivos", "*.*")],
                title="Seleccionar archivo de respaldo"
            )
            
            if not archivo:
                return  # Usuario canceló
            
            # Confirmar restauración
            if messagebox.askyesno("Confirmar", "¿Está seguro de restaurar desde este respaldo? Todos los datos actuales serán reemplazados."):
                if self.db.restaurar_respaldo(archivo):
                    messagebox.showinfo("Éxito", "Respaldo restaurado correctamente. La aplicación se reiniciará.")
                    self.salir()
                else:
                    messagebox.showerror("Error", "No se pudo restaurar el respaldo")
                    
        except Exception as e:
            messagebox.showerror("Error", f"Error al restaurar respaldo: {str(e)}")
    
    def sincronizacion_periodica(self):
        """Ejecuta la sincronización periódica con la base de datos MySQL"""
        while True:
            time.sleep(SYNC_INTERVAL)
            if self.db.sincronizar_datos():
                print("Sincronización completada exitosamente")
            else:
                print("No se pudo completar la sincronización")
    
    def salir(self):
        """Cierra la aplicación"""
        self.db.cerrar()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = AplicacionParqueadero(root)
    root.mainloop()

if __name__ == "__main__":
    main()