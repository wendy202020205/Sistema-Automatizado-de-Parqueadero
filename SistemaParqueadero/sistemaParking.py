import cv2
import pytesseract
import sqlite3
import mysql.connector
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk, simpledialog, filedialog
from PIL import Image, ImageTk
import os
import logging
import configparser
import threading
import numpy as np
import re
import hashlib
from tkcalendar import DateEntry
import webbrowser
from fpdf import FPDF
import csv
import time
import shutil

# ******************** CONFIGURACIÓN INICIAL ********************
logging.basicConfig(
    filename='estacionamiento.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

CONFIG_FILE = "configs.ini"
LOCAL_DB = "estacionamientos.db"
SYNC_INTERVAL = 300  # 5 minutos

class Config:
    def __init__(self):
        self.config = configparser.ConfigParser()
        self.load_config()
    
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            self.config.read(CONFIG_FILE)
        else:
            self.config['SETTINGS'] = {
                'tesseract_path': r'C:\Users\Huendy\AppData\Local\Programs\Tesseract-OCR\tesseract.exe',
                'tarifa_auto': '2.0',
                'tarifa_moto': '1.0',
                'tarifa_camioneta': '3.0',
                'tiempo_apertura_puerta': '3000',
                'max_espacios': '50',
                'ruta_reportes': './reportes',
                'modo_offline': 'False',
                'modelo_lbph': './modelos/lbph_model.xml'
            }
            self.config['DATABASE'] = {
                'host': 'localhost',
                'user': 'root',
                'password': '',
                'database': 'estacionamiento'
            }
            self.save_config()
            
            if not os.path.exists(self.config['SETTINGS']['ruta_reportes']):
                os.makedirs(self.config['SETTINGS']['ruta_reportes'])
    
    def save_config(self):
        with open(CONFIG_FILE, 'w') as configfile:
            self.config.write(configfile)
    
    def get_tesseract_path(self):
        return self.config['SETTINGS']['tesseract_path']
    
    def get_tarifas(self):
        return {
            'auto': float(self.config['SETTINGS']['tarifa_auto']),
            'moto': float(self.config['SETTINGS']['tarifa_moto']),
            'camioneta': float(self.config['SETTINGS']['tarifa_camioneta'])
        }
    
    def get_tiempo_apertura(self):
        return int(self.config['SETTINGS']['tiempo_apertura_puerta'])
    
    def get_max_espacios(self):
        return int(self.config['SETTINGS']['max_espacios'])
    
    def get_ruta_reportes(self):
        return self.config['SETTINGS']['ruta_reportes']
    
    def get_modo_offline(self):
        return self.config['SETTINGS']['modo_offline'] == 'True'
    
    def get_mysql_config(self):
        return dict(self.config['DATABASE'])
    
    def update_config(self, section, key, value):
        self.config[section][key] = str(value)
        self.save_config()
    
    def toggle_offline_mode(self):
        current = self.get_modo_offline()
        new_mode = not current
        self.config['SETTINGS']['modo_offline'] = str(new_mode)
        self.save_config()
        return new_mode

# ******************** RECONOCIMIENTO DE PLACAS ********************
class PlateRecognizer:
    @staticmethod
    def preprocess_image(img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5,5), 0)
        edged = cv2.Canny(blur, 50, 200)
        return edged
    
    @staticmethod
    def find_plate_contour(edged):
        contours, _ = cv2.findContours(edged, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:10]
        
        for contour in contours:
            perimeter = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
            if len(approx) == 4:
                x, y, w, h = cv2.boundingRect(contour)
                aspect_ratio = w / h
                if 2 <= aspect_ratio <= 5:
                    return approx
        return None
    
    @staticmethod
    def extract_plate(img, contour):
        mask = np.zeros(img.shape[:2], np.uint8)
        cv2.drawContours(mask, [contour], 0, 255, -1)
        return cv2.bitwise_and(img, img, mask=mask)
    
    @staticmethod
    def read_plate(image_path):
        try:
            img = cv2.imread(image_path)
            edged = PlateRecognizer.preprocess_image(img)
            plate_contour = PlateRecognizer.find_plate_contour(edged)
            
            if plate_contour is not None:
                plate_img = PlateRecognizer.extract_plate(img, plate_contour)
                x, y, w, h = cv2.boundingRect(plate_contour)
                plate_cropped = img[y:y+h, x:x+w]
            else:
                plate_cropped = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            _, thresh = cv2.threshold(plate_cropped, 150, 255, cv2.THRESH_BINARY)
            return pytesseract.image_to_string(
                thresh, 
                config='--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
            ).strip().replace(" ", "").replace("\n", "")
        except Exception as e:
            logging.error(f"Error en OCR: {e}")
            return ""

class FaceRecognizer:
    def __init__(self, model_path):
        self.model_path = model_path
        self.face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        self.recognizer = cv2.face.LBPHFaceRecognizer_create()
        
        # Cargar modelo si existe
        if os.path.exists(self.model_path):
            self.recognizer.read(self.model_path)

    def detect_faces(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self.face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
        return faces, gray

    def recognize_face(self, face_roi_gray):
        if not os.path.exists(self.model_path):
            return None, 0
        
        label, confidence = self.recognizer.predict(face_roi_gray)
        return label, confidence

    def train_model(self, faces, labels):
        self.recognizer.train(faces, np.array(labels))
        self.recognizer.save(self.model_path)

# ******************** BASE DE DATOS ********************
class DatabaseManager:
    def __init__(self, config):
        self.config = config
        self.conn = None
        self.conn_local = sqlite3.connect(LOCAL_DB, check_same_thread=False)
        self.cursor_local = self.conn_local.cursor()
        self.pending_sync = []
        self._initialize_db()
        
    def _initialize_db(self):
        # Tabla de movimientos (entradas/salidas)
        self.cursor_local.execute('''
            CREATE TABLE IF NOT EXISTS movimientos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                placa TEXT,
                tipo_vehiculo TEXT,
                hora_entrada TEXT,
                hora_salida TEXT,
                espacio_asignado INTEGER,
                conductor TEXT,
                tarifa REAL,
                tiempo_estacionado REAL,
                total_cobrado REAL,
                estado TEXT DEFAULT 'activo',
                pendiente_sync BOOLEAN DEFAULT 1
            )
        ''')
        
        # Tabla de espacios
        self.cursor_local.execute('''
            CREATE TABLE IF NOT EXISTS espacios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numero INTEGER UNIQUE NOT NULL,
                ocupado BOOLEAN NOT NULL DEFAULT 0
            )
        ''')
        
        # Tabla de usuarios
        self.cursor_local.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                rol TEXT NOT NULL
            )
        ''')
        
        # Tabla de reportes
        self.cursor_local.execute('''
            CREATE TABLE IF NOT EXISTS reportes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT NOT NULL UNIQUE,
                ingresos INTEGER DEFAULT 0,
                egresos INTEGER DEFAULT 0,
                total_cobrado REAL DEFAULT 0,
                pendiente_sync BOOLEAN DEFAULT 1
            )
        ''')
        
        # Crear índices
        self.cursor_local.execute('CREATE INDEX IF NOT EXISTS idx_placa ON movimientos(placa)')
        self.cursor_local.execute('CREATE INDEX IF NOT EXISTS idx_hora_salida ON movimientos(hora_salida)')
        
        # Crear usuario admin por defecto si no existe
        self.cursor_local.execute("SELECT * FROM usuarios WHERE username = 'admin'")
        if not self.cursor_local.fetchone():
            password_hash = hashlib.sha256("admin123".encode()).hexdigest()
            self.cursor_local.execute(
                "INSERT INTO usuarios (username, password, rol) VALUES (?, ?, ?)", 
                ('admin', password_hash, 'administrador')
            )
        
        # Crear espacios iniciales si no existen
        self.cursor_local.execute("SELECT COUNT(*) FROM espacios")
        count = self.cursor_local.fetchone()[0]
        
        if count == 0:
            max_espacios = self.config.get_max_espacios()
            for i in range(1, max_espacios + 1):
                self.cursor_local.execute("INSERT INTO espacios (numero, ocupado) VALUES (?, ?)", (i, False))
        
        self.conn_local.commit()
    
    def conectar_mysql(self):
        if self.config.get_modo_offline():
            return False
            
        try:
            db_config = self.config.get_mysql_config()
            self.conn = mysql.connector.connect(
                host=db_config['host'],
                user=db_config['user'],
                password=db_config['password'],
                database=db_config['database']
            )
            return True
        except mysql.connector.Error as err:
            logging.error(f"Error al conectar con MySQL: {err}")
            return False
    
    def execute_query(self, query, params=(), local_only=False):
        try:
            if not local_only and self.conectar_mysql():
                cursor = self.conn.cursor()
                cursor.execute(query, params)
                self.conn.commit()
                return cursor
                
            cursor = self.conn_local.cursor()
            cursor.execute(query, params)
            self.conn_local.commit()
            return cursor
        except Exception as e:
            logging.error(f"Error en base de datos: {e}")
            raise
    
    def registrar_ingreso(self, placa, tipo_vehiculo, conductor=None):
        # Verificar si el vehículo ya está registrado
        cursor = self.execute_query(
            "SELECT * FROM movimientos WHERE placa = ? AND estado = 'activo'", 
            (placa,),
            local_only=True
        )
        if cursor.fetchone():
            return False, "El vehículo ya está ingresado."
        
        # Buscar espacio libre
        cursor = self.execute_query(
            "SELECT * FROM espacios WHERE ocupado = 0 LIMIT 1",
            local_only=True
        )
        espacio = cursor.fetchone()

        if not espacio:
            return False, "No hay espacios disponibles."
        
        espacio_id = espacio[0]
        # Asignar espacio al vehículo
        self.execute_query(
            "UPDATE espacios SET ocupado = 1 WHERE id = ?", 
            (espacio_id,),
            local_only=True
        )
        
        # Registrar el vehículo
        hora_ingreso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.execute_query(
            "INSERT INTO movimientos (placa, tipo_vehiculo, hora_entrada, espacio_asignado, conductor) VALUES (?, ?, ?, ?, ?)", 
            (placa, tipo_vehiculo, hora_ingreso, espacio_id, conductor),
            local_only=True
        )
        
        # Actualizar reporte diario
        fecha = datetime.now().strftime('%Y-%m-%d')
        cursor = self.execute_query("SELECT * FROM reportes WHERE fecha = ?", (fecha,), local_only=True)
        if not cursor.fetchone():
            self.execute_query(
                "INSERT INTO reportes (fecha, ingresos) VALUES (?, 1)", 
                (fecha,),
                local_only=True
            )
        else:
            self.execute_query(
                "UPDATE reportes SET ingresos = ingresos + 1 WHERE fecha = ?", 
                (fecha,),
                local_only=True
            )
        
        self.add_pending_sync("ingreso", placa)
        return True, f"Vehículo {placa} registrado con éxito. Espacio: {espacio[1]}"
    
    def registrar_salida(self, placa):
        cursor = self.execute_query(
            "SELECT * FROM movimientos WHERE placa = ? AND estado = 'activo'", 
            (placa,),
            local_only=True
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
        tipo_vehiculo = vehiculo[2].lower()
        tarifa_por_hora = tarifas.get(tipo_vehiculo, 2.00)
        
        # Calcular el total a cobrar (tiempo en horas * tarifa)
        horas = tiempo_estacionado / 60
        if horas < 1:
            horas = 1  # Mínimo una hora
        total_cobrado = horas * tarifa_por_hora
        
        # Actualizar registro del vehículo
        self.execute_query(
            "UPDATE movimientos SET hora_salida = ?, tiempo_estacionado = ?, estado = 'salido', tarifa = ?, total_cobrado = ? WHERE id = ?", 
            (hora_salida.strftime('%Y-%m-%d %H:%M:%S'), tiempo_estacionado, tarifa_por_hora, total_cobrado, vehiculo[0]),
            local_only=True
        )
        
        # Liberar el espacio
        self.execute_query(
            "UPDATE espacios SET ocupado = 0 WHERE id = ?", 
            (espacio_id,),
            local_only=True
        )
        
        # Actualizar reporte diario
        fecha = datetime.now().strftime('%Y-%m-%d')
        cursor = self.execute_query("SELECT * FROM reportes WHERE fecha = ?", (fecha,), local_only=True)
        if not cursor.fetchone():
            self.execute_query(
                "INSERT INTO reportes (fecha, egresos, total_cobrado) VALUES (?, 1, ?)", 
                (fecha, total_cobrado),
                local_only=True
            )
        else:
            self.execute_query(
                "UPDATE reportes SET egresos = egresos + 1, total_cobrado = total_cobrado + ? WHERE fecha = ?", 
                (total_cobrado, fecha),
                local_only=True
            )
        
        self.add_pending_sync("salida", placa)
        
        return True, {
            "placa": placa,
            "tipo": vehiculo[2],
            "ingreso": hora_ingreso.strftime('%Y-%m-%d %H:%M:%S'),
            "salida": hora_salida.strftime('%Y-%m-%d %H:%M:%S'),
            "tiempo": f"{tiempo_estacionado:.2f} minutos",
            "tarifa": f"S/.{tarifa_por_hora:.2f}/hora",
            "total": f"S/.{total_cobrado:.2f}"
        }
    
    def get_vehiculos_activos(self):
        cursor = self.execute_query(
            "SELECT * FROM movimientos WHERE estado = 'activo'",
            local_only=True
        )
        return cursor.fetchall()
    
    def get_user_by_id(self, user_id):
        cursor = self.execute_query(
            "SELECT * FROM usuarios WHERE id = ?",
            (user_id,),
            local_only=True
            )
        return cursor.fetchone()
    
    def add_face_to_user(self, user_id, face_image):
        # Guardar imagen de rostro asociada al usuario (opcional)
        pass
    
    def get_espacios_disponibles(self):
        cursor = self.execute_query(
            "SELECT COUNT(*) FROM espacios WHERE ocupado = 0",
            local_only=True
        )
        return cursor.fetchone()[0]
    
    def get_reporte_rango(self, fecha_inicio, fecha_fin):
        cursor = self.execute_query(
            "SELECT * FROM reportes WHERE fecha BETWEEN ? AND ? ORDER BY fecha",
            (fecha_inicio, fecha_fin),
            local_only=True
        )
        return cursor.fetchall()
    
    def get_vehiculos_por_fecha(self, fecha):
        fecha_inicio = f"{fecha} 00:00:00"
        fecha_fin = f"{fecha} 23:59:59"
        
        cursor = self.execute_query(
            "SELECT * FROM movimientos WHERE hora_entrada BETWEEN ? AND ?",
            (fecha_inicio, fecha_fin),
            local_only=True
        )
        return cursor.fetchall()
    
    def verificar_usuario(self, username, password):
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        cursor = self.execute_query(
            "SELECT * FROM usuarios WHERE username = ? AND password = ?",
            (username, password_hash),
            local_only=True
        )
        return cursor.fetchone()
    
    def crear_usuario(self, username, password, rol):
        try:
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            self.execute_query(
                "INSERT INTO usuarios (username, password, rol) VALUES (?, ?, ?)",
                (username, password_hash, rol),
                local_only=True
            )
            return True, "Usuario creado exitosamente"
        except sqlite3.IntegrityError:
            return False, "El nombre de usuario ya existe"
    
    def get_usuarios(self):
        cursor = self.execute_query(
            "SELECT id, username, rol FROM usuarios",
            local_only=True
        )
        return cursor.fetchall()
    
    def cambiar_password(self, usuario_id, new_password):
        password_hash = hashlib.sha256(new_password.encode()).hexdigest()
        self.execute_query(
            "UPDATE usuarios SET password = ? WHERE id = ?",
            (password_hash, usuario_id),
            local_only=True
        )
        return True
    
    def eliminar_usuario(self, usuario_id):
        try:
            self.execute_query(
                "DELETE FROM usuarios WHERE id = ?",
                (usuario_id,),
                local_only=True
            )
            return True
        except Exception as e:
            logging.error(f"Error al eliminar usuario: {e}")
            return False
    
    def add_pending_sync(self, tipo, placa):
        if self.config.get_modo_offline():
            self.pending_sync.append({
                'tipo': tipo,
                'placa': placa,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })
    
    def sincronizar_datos(self):
        if self.config.get_modo_offline() or not self.pending_sync:
            return False
        
        try:
            for operacion in self.pending_sync:
                if operacion['tipo'] == "ingreso":
                    cursor = self.execute_query(
                        "SELECT * FROM movimientos WHERE placa = ? AND estado = 'activo'", 
                        (operacion['placa'],),
                        local_only=True
                    )
                    vehiculo = cursor.fetchone()
                    
                    if vehiculo:
                        self.execute_query(
                            "INSERT INTO movimientos (placa, tipo_vehiculo, hora_entrada, espacio_asignado, conductor) VALUES (%s, %s, %s, %s, %s)",
                            (vehiculo[1], vehiculo[2], vehiculo[3], vehiculo[5], vehiculo[6])
                        )
                
                elif operacion['tipo'] == "salida":
                    cursor = self.execute_query(
                        "SELECT * FROM movimientos WHERE placa = ? AND estado = 'salido' ORDER BY id DESC LIMIT 1", 
                        (operacion['placa'],),
                        local_only=True
                    )
                    vehiculo = cursor.fetchone()
                    
                    if vehiculo:
                        self.execute_query(
                            "UPDATE movimientos SET hora_salida = %s, tiempo_estacionado = %s, estado = 'salido', tarifa = %s, total_cobrado = %s WHERE placa = %s AND estado = 'activo'",
                            (vehiculo[4], vehiculo[8], vehiculo[9], vehiculo[10], vehiculo[1]))
            
            # Marcar como sincronizados en la base local
            self.execute_query("UPDATE movimientos SET pendiente_sync = 0 WHERE pendiente_sync = 1", local_only=True)
            self.execute_query("UPDATE reportes SET pendiente_sync = 0 WHERE pendiente_sync = 1", local_only=True)
            
            self.pending_sync = []
            return True
        except Exception as e:
            logging.error(f"Error durante la sincronización: {e}")
            return False
    
    def crear_respaldo(self, archivo):
        try:
            backup_conn = sqlite3.connect(archivo)
            self.conn_local.backup(backup_conn)
            backup_conn.close()
            return True
        except Exception as e:
            logging.error(f"Error al crear respaldo: {e}")
            return False
    
    def restaurar_respaldo(self, archivo):
        try:
            if self.conn_local:
                self.conn_local.close()
            
            shutil.copyfile(archivo, LOCAL_DB)
            self.conn_local = sqlite3.connect(LOCAL_DB, check_same_thread=False)
            self.cursor_local = self.conn_local.cursor()
            return True
        except Exception as e:
            logging.error(f"Error al restaurar respaldo: {e}")
            return False
    
    def cerrar(self):
        if self.conn_local:
            self.conn_local.close()
        if self.conn:
            self.conn.close()

# ******************** INTERFAZ GRÁFICA ********************
class ParkingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sistema de Estacionamiento Automatizado")
        self.geometry("1000x600")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Configuración
        self.config = Config()
        pytesseract.pytesseract.tesseract_cmd = self.config.get_tesseract_path()
        
        # Base de datos
        self.db = DatabaseManager(self.config)
        
        # Variables de sesión
        self.usuario_actual = None
        self.rol_usuario = None
        self.intentos_login = 0
        
        # Iniciar con pantalla de login
        self.mostrar_login()
        
        # Iniciar hilo de sincronización
        self.sync_thread = threading.Thread(target=self.sincronizacion_periodica, daemon=True)
        self.sync_thread.start()
    
    def mostrar_login(self):
        for widget in self.winfo_children():
            widget.destroy()
        
        frame_login = ttk.Frame(self, padding="20")
        frame_login.pack(expand=True)
        
        ttk.Label(frame_login, text="Sistema de Estacionamiento Automatizado", font=("Arial", 18, "bold")).pack(pady=10)
        
        ttk.Label(frame_login, text="Usuario:").pack(pady=5)
        self.entry_usuario = ttk.Entry(frame_login, width=30)
        self.entry_usuario.pack(pady=5)
        
        ttk.Label(frame_login, text="Contraseña:").pack(pady=5)
        self.entry_password = ttk.Entry(frame_login, width=30, show="*")
        self.entry_password.pack(pady=5)
        
        ttk.Button(frame_login, text="Iniciar Sesión", command=self.login).pack(pady=20)
        ttk.Button(frame_login, text="Iniciar Sesión con Reconocimiento Facial", command=self.login_facial).pack(pady=10)

        if self.config.get_modo_offline():
            offline_text = "Modo Offline Activado"
        else:
            offline_text = "Activar Modo Offline"
        
        ttk.Button(frame_login, text=offline_text, command=self.toggle_offline).pack(pady=5)
        
        self.entry_usuario.focus_set()
        self.bind("<Return>", lambda event: self.login())
    
    def login(self):
        username = self.entry_usuario.get()
        password = self.entry_password.get()
        
        if not username or not password:
            messagebox.showerror("Error", "Debe completar todos los campos")
            return
        
        usuario = self.db.verificar_usuario(username, password)
        if usuario:
            self.intentos_login = 0
            self.usuario_actual = usuario[0]
            self.rol_usuario = usuario[3]
            self.mostrar_dashboard()
        else:
            self.intentos_login += 1
            if self.intentos_login >= 3:
                messagebox.showerror("Bloqueado", "Demasiados intentos. Espere 30 segundos.")
                self.after(30000, lambda: None)
                self.intentos_login = 0
            else:
                messagebox.showerror("Error", f"Credenciales incorrectas. Intentos: {self.intentos_login}/3")
    
    def login_facial(self):
        # Inicializar reconocedor facial
        face_recognizer = FaceRecognizer(self.config.config.get('SETTINGS', 'modelo_lbph'))
        
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            messagebox.showerror("Error", "No se pudo acceder a la cámara")
            return
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                
                # Detectar rostros
                faces, gray = face_recognizer.detect_faces(frame)
                
                for (x, y, w, h) in faces:
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                    face_roi_gray = gray[y:y+h, x:x+w]
                    
                    # Reconocer rostro
                    label, confidence = face_recognizer.recognize_face(face_roi_gray)
                    
                    if confidence < 50:  # Umbral de confianza
                        usuario = self.db.get_user_by_id(label)
                        if usuario:
                            self.usuario_actual = usuario[0]
                            self.rol_usuario = usuario[3]
                            messagebox.showinfo("Éxito", f"Bienvenido, {usuario[1]}!")
                            self.mostrar_dashboard()
                            return
                cv2.imshow("Reconocimiento Facial (Presione 'q' para salir)", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
        finally:
            cap.release()
            cv2.destroyAllWindows()

    def toggle_offline(self):
        is_offline = self.config.toggle_offline_mode()
        if is_offline:
            messagebox.showinfo("Modo Offline", "Modo offline activado. Los datos se sincronizarán cuando vuelva a estar en línea.")
        else:
            messagebox.showinfo("Modo Online", "Modo online activado. Los datos se sincronizarán automáticamente.")
        self.mostrar_login()
    
    def mostrar_dashboard(self):
        for widget in self.winfo_children():
            widget.destroy()
        
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        self.tab_vehiculos = ttk.Frame(self.notebook)
        self.tab_reportes = ttk.Frame(self.notebook)
        self.tab_config = ttk.Frame(self.notebook)
        
        self.notebook.add(self.tab_vehiculos, text="Gestión de Vehículos")
        self.notebook.add(self.tab_reportes, text="Reportes")
        
        if self.rol_usuario == "administrador":
            self.notebook.add(self.tab_config, text="Configuración")
        
        self.configurar_tab_vehiculos()
        self.configurar_tab_reportes()
        
        if self.rol_usuario == "administrador":
            self.configurar_tab_config()
        
        self.statusbar = ttk.Label(self, text="", relief=tk.SUNKEN, anchor=tk.W)
        self.statusbar.pack(side=tk.BOTTOM, fill=tk.X)
        
        self.actualizar_estado()
    
    def configurar_tab_vehiculos(self):
        frame_registro = ttk.LabelFrame(self.tab_vehiculos, text="Registro Automático de Vehículos")
        frame_registro.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        frame_ocupacion = ttk.LabelFrame(self.tab_vehiculos, text="Estado del Estacionamiento")
        frame_ocupacion.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Botón para capturar placa automáticamente
        ttk.Button(frame_registro, text="Capturar Placa con Cámara", command=self.capturar_placa).pack(pady=10)
        
        ttk.Label(frame_registro, text="Placa:").pack(pady=5)
        self.entry_placa = ttk.Entry(frame_registro, width=15)
        self.entry_placa.pack(pady=5)
        
        ttk.Label(frame_registro, text="Tipo de vehículo:").pack(pady=5)
        self.combo_tipo = ttk.Combobox(frame_registro, width=15, values=["Auto", "Moto", "Camioneta"])
        self.combo_tipo.pack(pady=5)
        self.combo_tipo.current(0)
        
        ttk.Label(frame_registro, text="Conductor (opcional):").pack(pady=5)
        self.entry_conductor = ttk.Entry(frame_registro, width=25)
        self.entry_conductor.pack(pady=5)
        
        ttk.Button(frame_registro, text="Registrar Ingreso", command=self.registrar_entrada).pack(pady=10)
        ttk.Button(frame_registro, text="Registrar Salida", command=self.registrar_salida).pack(pady=10)
        
        # Tabla de vehículos actuales
        ttk.Label(frame_registro, text="Vehículos Actualmente en el Estacionamiento:", font=("Arial", 10, "bold")).pack(pady=5)
        
        self.tree_vehiculos = ttk.Treeview(frame_registro, columns=("Placa", "Tipo", "Ingreso", "Espacio", "Conductor"), show="headings")
        self.tree_vehiculos.pack(fill=tk.BOTH, expand=True, pady=5)
        
        scrollbar = ttk.Scrollbar(frame_registro, orient=tk.VERTICAL, command=self.tree_vehiculos.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_vehiculos.configure(yscrollcommand=scrollbar.set)
        
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
        
        ttk.Button(frame_registro, text="Refrescar Lista", command=self.actualizar_lista_vehiculos).pack(pady=5)
        
        # Visualización de espacios
        self.frame_espacios = ttk.Frame(frame_ocupacion)
        self.frame_espacios.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.actualizar_espacios()
        self.actualizar_lista_vehiculos()
    
    def configurar_tab_reportes(self):
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
        
        frame_reporte = ttk.LabelFrame(self.tab_reportes, text="Reporte")
        frame_reporte.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.tree_reporte = ttk.Treeview(frame_reporte, columns=("Fecha", "Ingresos", "Egresos", "Total"), show="headings")
        self.tree_reporte.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        scrollbar = ttk.Scrollbar(frame_reporte, orient=tk.VERTICAL, command=self.tree_reporte.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_reporte.configure(yscrollcommand=scrollbar.set)
        
        self.tree_reporte.heading("Fecha", text="Fecha")
        self.tree_reporte.heading("Ingresos", text="Ingresos")
        self.tree_reporte.heading("Egresos", text="Egresos")
        self.tree_reporte.heading("Total", text="Total Cobrado (S/.)")
        
        self.tree_reporte.column("Fecha", width=100)
        self.tree_reporte.column("Ingresos", width=80)
        self.tree_reporte.column("Egresos", width=80)
        self.tree_reporte.column("Total", width=120)
        
        ttk.Button(frame_reporte, text="Ver Detalles", command=self.ver_detalles_dia).pack(side=tk.BOTTOM, padx=5, pady=5)
    
    def configurar_tab_config(self):
        config_notebook = ttk.Notebook(self.tab_config)
        config_notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        tab_general = ttk.Frame(config_notebook)
        tab_tarifas = ttk.Frame(config_notebook)
        tab_usuarios = ttk.Frame(config_notebook)
        tab_backup = ttk.Frame(config_notebook)
        tab_face_reg = ttk.Frame(config_notebook)

        config_notebook.add(tab_general, text="General")
        config_notebook.add(tab_tarifas, text="Tarifas")
        config_notebook.add(tab_usuarios, text="Usuarios")
        config_notebook.add(tab_backup, text="Respaldo")
        config_notebook.add(tab_face_reg, text="Registro Facial")
        
        # Configuración General
        ttk.Label(tab_general, text="Configuración General", font=("Arial", 12, "bold")).pack(pady=10)
        
        frame_config = ttk.Frame(tab_general)
        frame_config.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        ttk.Label(frame_config, text="Tesseract OCR Path:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_tesseract = ttk.Entry(frame_config, width=40)
        self.entry_tesseract.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_tesseract.insert(0, self.config.get_tesseract_path())
        
        ttk.Label(frame_config, text="Tiempo apertura puerta (ms):").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_tiempo_puerta = ttk.Entry(frame_config, width=10)
        self.entry_tiempo_puerta.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_tiempo_puerta.insert(0, self.config.get_tiempo_apertura())
        
        ttk.Label(frame_config, text="Máximo de espacios:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_maxespacios = ttk.Entry(frame_config, width=10)
        self.entry_maxespacios.grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_maxespacios.insert(0, self.config.get_max_espacios())
        
        ttk.Label(frame_config, text="Ruta de reportes:").grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_rutareportes = ttk.Entry(frame_config, width=40)
        self.entry_rutareportes.grid(row=3, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_rutareportes.insert(0, self.config.get_ruta_reportes())
        
        ttk.Button(tab_general, text="Guardar Configuración", command=self.guardar_config_general).pack(pady=20)
        
        # Configuración de Tarifas
        ttk.Label(tab_tarifas, text="Configuración de Tarifas", font=("Arial", 12, "bold")).pack(pady=10)
        
        frame_tarifas = ttk.Frame(tab_tarifas)
        frame_tarifas.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        tarifas = self.config.get_tarifas()
        
        ttk.Label(frame_tarifas, text="Tarifa por hora para Autos (S/.):").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_auto = ttk.Entry(frame_tarifas, width=10)
        self.entry_tarifa_auto.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_auto.insert(0, tarifas['auto'])
        
        ttk.Label(frame_tarifas, text="Tarifa por hora para Motos (S/.):").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_moto = ttk.Entry(frame_tarifas, width=10)
        self.entry_tarifa_moto.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_moto.insert(0, tarifas['moto'])
        
        ttk.Label(frame_tarifas, text="Tarifa por hora para Camionetas (S/.):").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_camioneta = ttk.Entry(frame_tarifas, width=10)
        self.entry_tarifa_camioneta.grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)
        self.entry_tarifa_camioneta.insert(0, tarifas['camioneta'])
        
        ttk.Button(tab_tarifas, text="Guardar Tarifas", command=self.guardar_tarifas).pack(pady=20)
        
        # Configuración de Usuarios
        ttk.Label(tab_usuarios, text="Gestión de Usuarios", font=("Arial", 12, "bold")).pack(pady=10)
        
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
        self.combo_rol.current(1)
        
        ttk.Button(frame_crear_usuario, text="Crear Usuario", command=self.crear_nuevo_usuario).grid(row=3, column=0, columnspan=2, pady=10)
        
        frame_lista_usuarios = ttk.LabelFrame(tab_usuarios, text="Usuarios Existentes")
        frame_lista_usuarios.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        self.tree_usuarios = ttk.Treeview(frame_lista_usuarios, columns=("ID", "Usuario", "Rol"), show="headings")
        self.tree_usuarios.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        scrollbar = ttk.Scrollbar(frame_lista_usuarios, orient=tk.VERTICAL, command=self.tree_usuarios.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_usuarios.configure(yscrollcommand=scrollbar.set)
        
        self.tree_usuarios.heading("ID", text="ID")
        self.tree_usuarios.heading("Usuario", text="Usuario")
        self.tree_usuarios.heading("Rol", text="Rol")
        
        self.tree_usuarios.column("ID", width=50)
        self.tree_usuarios.column("Usuario", width=150)
        self.tree_usuarios.column("Rol", width=100)
        
        frame_botones_usuarios = ttk.Frame(tab_usuarios)
        frame_botones_usuarios.pack(fill=tk.X, padx=20, pady=10)
        
        ttk.Button(frame_botones_usuarios, text="Refrescar Lista", command=self.cargar_usuarios).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame_botones_usuarios, text="Cambiar Contraseña", command=self.cambiar_password_usuario).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame_botones_usuarios, text="Eliminar Usuario", command=self.eliminar_usuario).pack(side=tk.LEFT, padx=5)
        
        self.cargar_usuarios()
        
        # Configuración de Respaldo
        ttk.Label(tab_backup, text="Respaldo y Restauración", font=("Arial", 12, "bold")).pack(pady=10)
        
        frame_backup = ttk.Frame(tab_backup)
        frame_backup.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        ttk.Label(frame_backup, text="Realizar respaldo de la base de datos local:").grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=10)
        
        ttk.Button(frame_backup, text="Crear Respaldo", command=self.crear_respaldo).grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        
        ttk.Label(frame_backup, text="Restaurar desde respaldo:").grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=10)
        
        ttk.Button(frame_backup, text="Restaurar Respaldo", command=self.restaurar_respaldo).grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)

        #Configuracion de Reconocimiento Facial 
        ttk.Label(tab_face_reg, text="Registro de Rostros", font=("Arial", 12, "bold")).pack(pady=10)

        self.btn_capturar_rostro = ttk.Button(tab_face_reg, text="Capturar Rostro", command=self.capturar_rostro)
        self.btn_capturar_rostro.pack(pady=10)
        
        self.combo_usuario_rostro = ttk.Combobox(tab_face_reg, values=[u[1] for u in self.db.get_usuarios()])
        self.combo_usuario_rostro.pack(pady=5)
        
        self.btn_entrenar_modelo = ttk.Button(tab_face_reg, text="Entrenar Modelo", command=self.entrenar_modelo_facial)
        self.btn_entrenar_modelo.pack(pady=10)
        
    def capturar_placa(self):
        """Captura una imagen de la cámara y procesa la placa"""
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            messagebox.showerror("Error", "No se pudo acceder a la cámara")
            return
            
        filename = "placa_temp.jpg"
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                cv2.imshow("Captura de Placa (Presione ESPACIO para capturar)", frame)
                key = cv2.waitKey(1)
                if key % 256 == 32:  # Tecla ESPACIO
                    cv2.imwrite(filename, frame)
                    break
        finally:
            cap.release()
            cv2.destroyAllWindows()
        
        placa = PlateRecognizer.read_plate(filename)
        os.remove(filename)
        
        if placa and len(placa) >= 6:  # Validación básica de placa
            self.entry_placa.delete(0, tk.END)
            self.entry_placa.insert(0, placa)
            messagebox.showinfo("Placa detectada", f"Placa detectada: {placa}")
        else:
            messagebox.showerror("Error", "No se pudo detectar la placa. Por favor ingrésela manualmente.")

    def capturar_rostro(self):
        """Captura imágenes del rostro para un usuario seleccionado"""
        usuario = self.combo_usuario_rostro.get()
        if not usuario:
            messagebox.showerror("Error", "Debe seleccionar un usuario para capturar el rostro")
            return
        
        # Crear carpeta para el usuario si no existe
        carpeta_usuario = os.path.join("rostros", usuario)
        if not os.path.exists(carpeta_usuario):
            os.makedirs(carpeta_usuario)
        
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            messagebox.showerror("Error", "No se pudo acceder a la cámara")
            return
        
        count = 0
        messagebox.showinfo("Instrucciones", "Presione ESPACIO para capturar una imagen del rostro. Presione ESC para salir.")
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
                
                for (x, y, w, h) in faces:
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                
                cv2.imshow("Captura de Rostro", frame)
                key = cv2.waitKey(1)
                
                if key == 27:  # ESC
                    break
                elif key == 32:  # ESPACIO
                    if len(faces) == 0:
                        messagebox.showwarning("Advertencia", "No se detectó ningún rostro. Intente nuevamente.")
                        continue
                    for (x, y, w, h) in faces:
                        rostro = gray[y:y+h, x:x+w]
                        rostro = cv2.resize(rostro, (200, 200))
                        archivo = os.path.join(carpeta_usuario, f"{usuario}_{count}.jpg")
                        cv2.imwrite(archivo, rostro)
                        count += 1
                        messagebox.showinfo("Captura", f"Imagen {count} capturada")
        finally:
            cap.release()
            cv2.destroyAllWindows()
    
    def entrenar_modelo_facial(self):
        """Entrena el modelo LBPH con las imágenes capturadas"""
        ruta_rostros = "rostros"
        if not os.path.exists(ruta_rostros):
            messagebox.showerror("Error", "No hay imágenes de rostros para entrenar")
            return
        
        faces = []
        labels = []
        label_map = {}
        current_label = 0
        
        for usuario in os.listdir(ruta_rostros):
            carpeta_usuario = os.path.join(ruta_rostros, usuario)
            if not os.path.isdir(carpeta_usuario):
                continue
            
            if usuario not in label_map:
                label_map[usuario] = current_label
                current_label += 1
            
            for archivo in os.listdir(carpeta_usuario):
                if archivo.endswith(".jpg") or archivo.endswith(".png"):
                    img_path = os.path.join(carpeta_usuario, archivo)
                    img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
                    if img is None:
                        continue
                    faces.append(img)
                    labels.append(label_map[usuario])
        
        if len(faces) == 0:
            messagebox.showerror("Error", "No se encontraron imágenes válidas para entrenar")
            return
        
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.train(faces, np.array(labels))
        
        modelo_path = self.config.config['SETTINGS']['modelo_lbph']
        modelo_dir = os.path.dirname(modelo_path)
        if not os.path.exists(modelo_dir):
            os.makedirs(modelo_dir)
        
        recognizer.save(modelo_path)
        messagebox.showinfo("Éxito", "Modelo facial entrenado correctamente")
    
    def registrar_entrada(self):
        """Registra la entrada de un vehículo"""
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
            self.entry_placa.delete(0, tk.END)
            self.entry_conductor.delete(0, tk.END)
            self.combo_tipo.current(0)
            self.actualizar_lista_vehiculos()
            self.actualizar_espacios()
            self.actualizar_estado()
            self.generar_ticket_ingreso(placa, tipo, conductor)
        else:
            messagebox.showerror("Error", mensaje)
    
    def registrar_salida(self):
        """Registra la salida de un vehículo"""
        placa = self.entry_placa.get().upper()
        
        if not placa:
            messagebox.showerror("Error", "Debe ingresar la placa del vehículo")
            return
        
        result, datos = self.db.registrar_salida(placa)
        
        if result:
            messagebox.showinfo("Éxito", f"Vehículo {placa} ha salido del estacionamiento")
            self.mostrar_factura(datos)
            self.entry_placa.delete(0, tk.END)
            self.actualizar_lista_vehiculos()
            self.actualizar_espacios()
            self.actualizar_estado()
        else:
            messagebox.showerror("Error", datos)
    
    def mostrar_factura(self, datos):
        ventana_factura = tk.Toplevel(self)
        ventana_factura.title("Comprobante de Salida")
        ventana_factura.geometry("400x300")
        
        frame_factura = ttk.Frame(ventana_factura, padding="20")
        frame_factura.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(frame_factura, text="COMPROBANTE DE PAGO", font=("Arial", 14, "bold")).pack(pady=10)
        
        ttk.Label(frame_factura, text=f"Placa: {datos['placa']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Tipo: {datos['tipo']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Hora de ingreso: {datos['ingreso']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Hora de salida: {datos['salida']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Tiempo de permanencia: {datos['tiempo']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Tarifa: {datos['tarifa']}", font=("Arial", 12)).pack(anchor=tk.W, pady=5)
        ttk.Label(frame_factura, text=f"Total a pagar: {datos['total']}", font=("Arial", 14, "bold")).pack(anchor=tk.W, pady=10)
        
        frame_botones = ttk.Frame(frame_factura)
        frame_botones.pack(fill=tk.X, pady=10)
        
        ttk.Button(frame_botones, text="Imprimir", command=lambda: self.imprimir_factura(datos)).pack(side=tk.LEFT, padx=5)
        ttk.Button(frame_botones, text="Cerrar", command=ventana_factura.destroy).pack(side=tk.RIGHT, padx=5)
    
    def imprimir_factura(self, datos):
        try:
            dir_comprobantes = os.path.join(self.config.get_ruta_reportes(), 'comprobantes')
            if not os.path.exists(dir_comprobantes):
                os.makedirs(dir_comprobantes)
            
            fecha_actual = datetime.now().strftime('%Y%m%d%H%M%S')
            archivo = os.path.join(dir_comprobantes, f"comprobante_{datos['placa']}_{fecha_actual}.pdf")
            
            pdf = FPDF()
            pdf.add_page()
            
            pdf.set_font('Arial', 'B', 16)
            pdf.cell(190, 10, 'COMPROBANTE DE PAGO', 0, 1, 'C')
            pdf.ln(10)
            
            pdf.set_font('Arial', '', 12)
            pdf.cell(190, 10, f"Placa: {datos['placa']}", 0, 1)
            pdf.cell(190, 10, f"Tipo: {datos['tipo']}", 0, 1)
            pdf.cell(190, 10, f"Hora de ingreso: {datos['ingreso']}", 0, 1)
            pdf.cell(190, 10, f"Hora de salida: {datos['salida']}", 0, 1)
            pdf.cell(190, 10, f"Tiempo de permanencia: {datos['tiempo']}", 0, 1)
            pdf.cell(190, 10, f"Tarifa: {datos['tarifa']}", 0, 1)
            
            pdf.ln(5)
            pdf.set_font('Arial', 'B', 14)
            pdf.cell(190, 10, f"Total a pagar: {datos['total']}", 0, 1)
            
            pdf.ln(20)
            pdf.set_font('Arial', 'I', 10)
            pdf.cell(190, 10, 'Gracias por utilizar nuestro servicio de estacionamiento', 0, 1, 'C')
            
            pdf.output(archivo)
            webbrowser.open(archivo)
        except Exception as e:
            messagebox.showerror("Error", f"Error al generar el comprobante: {str(e)}")
    
    def generar_ticket_ingreso(self, placa, tipo, conductor):
        try:
            dir_tickets = os.path.join(self.config.get_ruta_reportes(), 'tickets')
            if not os.path.exists(dir_tickets):
                os.makedirs(dir_tickets)
            
            fecha_hora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            fecha_archivo = datetime.now().strftime('%Y%m%d%H%M%S')
            
            archivo = os.path.join(dir_tickets, f"ticket_{placa}_{fecha_archivo}.pdf")
            
            pdf = FPDF()
            pdf.add_page()
            
            pdf.set_font('Arial', 'B', 16)
            pdf.cell(190, 10, 'TICKET DE INGRESO', 0, 1, 'C')
            pdf.ln(10)
            
            pdf.set_font('Arial', '', 12)
            pdf.cell(190, 10, f"Placa: {placa}", 0, 1)
            pdf.cell(190, 10, f"Tipo de vehículo: {tipo}", 0, 1)
            if conductor:
                pdf.cell(190, 10, f"Conductor: {conductor}", 0, 1)
            pdf.cell(190, 10, f"Fecha y hora de ingreso: {fecha_hora}", 0, 1)
            
            pdf.ln(20)
            pdf.set_font('Arial', 'I', 10)
            pdf.cell(190, 10, 'Conserve este ticket para la salida del vehículo', 0, 1, 'C')
            
            pdf.output(archivo)
            webbrowser.open(archivo)
        except Exception as e:
            messagebox.showerror("Error", f"Error al generar el ticket: {str(e)}")
    
    def actualizar_lista_vehiculos(self):
        for item in self.tree_vehiculos.get_children():
            self.tree_vehiculos.delete(item)
        
        vehiculos = self.db.get_vehiculos_activos()
        
        for vehiculo in vehiculos:
            self.tree_vehiculos.insert("", tk.END, values=(
                vehiculo[1], vehiculo[2], vehiculo[3], vehiculo[5], vehiculo[6] or ""
            ))
    
    def actualizar_espacios(self):
        for widget in self.frame_espacios.winfo_children():
            widget.destroy()
        
        max_espacios = self.config.get_max_espacios()
        
        cursor = self.db.execute_query(
            "SELECT id, numero, ocupado FROM espacios ORDER BY numero",
            local_only=True
        )
        espacios = cursor.fetchall()
        
        row, col = 0, 0
        for espacio in espacios:
            id_espacio, numero, ocupado = espacio
            
            color = "#FF6B6B" if ocupado else "#4CAF50"
            
            btn = tk.Button(self.frame_espacios, text=str(numero), width=4, height=2, bg=color)
            btn.grid(row=row, column=col, padx=2, pady=2)
            
            col += 1
            if col > 9:
                col = 0
                row += 1
    
    def actualizar_estado(self):
        espacios_disponibles = self.db.get_espacios_disponibles()
        total_espacios = self.config.get_max_espacios()
        
        modo = "OFFLINE" if self.config.get_modo_offline() else "ONLINE"
        
        self.statusbar.config(
            text=f" Modo: {modo} | Espacios disponibles: {espacios_disponibles}/{total_espacios} | Usuario: {self.rol_usuario}"
        )
    
    def generar_reporte(self):
        fecha_inicio = self.date_inicio.get()
        fecha_fin = self.date_fin.get()
        
        if not fecha_inicio or not fecha_fin:
            messagebox.showerror("Error", "Debe seleccionar el rango de fechas")
            return
        
        try:
            datetime.strptime(fecha_inicio, '%Y-%m-%d')
            datetime.strptime(fecha_fin, '%Y-%m-%d')
        except ValueError:
            messagebox.showerror("Error", "Formato de fecha inválido")
            return
        
        for item in self.tree_reporte.get_children():
            self.tree_reporte.delete(item)
        
        reportes = self.db.get_reporte_rango(fecha_inicio, fecha_fin)
        
        if not reportes:
            messagebox.showinfo("Información", "No hay reportes en el rango de fechas seleccionado")
            return
        
        total_general = 0
        for reporte in reportes:
            id_reporte, fecha, ingresos, egresos, total, _ = reporte
            total_general += total if total else 0
            
            self.tree_reporte.insert("", tk.END, values=(
                fecha, ingresos, egresos, f"s/.{total:.2f}" if total else "s/.0.00"
            ))
        
        self.tree_reporte.insert("", tk.END, values=("TOTAL", "", "", f"s/.{total_general:.2f}"))
    
    def ver_detalles_dia(self):
        seleccion = self.tree_reporte.selection()
        if not seleccion:
            messagebox.showerror("Error", "Debe seleccionar un día del reporte")
            return
        
        valores = self.tree_reporte.item(seleccion, 'values')
        fecha = valores[0]
        
        if fecha == "TOTAL":
            messagebox.showerror("Error", "Debe seleccionar un día específico, no el total")
            return
        
        vehiculos = self.db.get_vehiculos_por_fecha(fecha)
        
        if not vehiculos:
            messagebox.showinfo("Información", f"No hay vehículos registrados para el {fecha}")
            return
        
        ventana_detalles = tk.Toplevel(self)
        ventana_detalles.title(f"Detalles del {fecha}")
        ventana_detalles.geometry("800x400")
        
        frame_principal = ttk.Frame(ventana_detalles)
        frame_principal.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        tree_vehiculos = ttk.Treeview(frame_principal, columns=("Placa", "Tipo", "Ingreso", "Salida", "Espacio", "Conductor", "Total"), show="headings")
        tree_vehiculos.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(frame_principal, orient=tk.VERTICAL, command=tree_vehiculos.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree_vehiculos.configure(yscrollcommand=scrollbar.set)
        
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
        
        for vehiculo in vehiculos:
            tree_vehiculos.insert("", tk.END, values=(
                vehiculo[1], vehiculo[2], vehiculo[3], 
                vehiculo[4] if vehiculo[4] else "", 
                vehiculo[5] if vehiculo[5] else "", 
                vehiculo[6] if vehiculo[6] else "", 
                f"S/.{vehiculo[10]:.2f}" if vehiculo[10] else ""
            ))
    
    def exportar_pdf(self):
        if not self.tree_reporte.get_children():
            messagebox.showerror("Error", "No hay datos para exportar")
            return
        
        fecha_inicio = self.date_inicio.get()
        fecha_fin = self.date_fin.get()
        
        try:
            ruta_reportes = self.config.get_ruta_reportes()
            if not os.path.exists(ruta_reportes):
                os.makedirs(ruta_reportes)
            
            fecha_actual = datetime.now().strftime('%Y%m%d%H%M%S')
            archivo = os.path.join(ruta_reportes, f"reporte_{fecha_inicio}_{fecha_fin}_{fecha_actual}.pdf")
            
            pdf = FPDF()
            pdf.add_page()
            
            pdf.set_font('Arial', 'B', 16)
            pdf.cell(190, 10, 'REPORTE DE ESTACIONAMIENTO', 0, 1, 'C')
            pdf.set_font('Arial', '', 12)
            pdf.cell(190, 10, f"Período: {fecha_inicio} - {fecha_fin}", 0, 1, 'C')
            pdf.ln(10)
            
            pdf.set_font('Arial', 'B', 12)
            pdf.cell(47.5, 10, 'Fecha', 1, 0, 'C')
            pdf.cell(47.5, 10, 'Ingresos', 1, 0, 'C')
            pdf.cell(47.5, 10, 'Egresos', 1, 0, 'C')
            pdf.cell(47.5, 10, 'Total Cobrado', 1, 1, 'C')
            
            pdf.set_font('Arial', '', 12)
            for item in self.tree_reporte.get_children():
                valores = self.tree_reporte.item(item, 'values')
                pdf.cell(47.5, 10, str(valores[0]), 1, 0, 'C')
                pdf.cell(47.5, 10, str(valores[1]), 1, 0, 'C')
                pdf.cell(47.5, 10, str(valores[2]), 1, 0, 'C')
                pdf.cell(47.5, 10, str(valores[3]), 1, 1, 'C')
            
            pdf.ln(10)
            pdf.set_font('Arial', 'I', 10)
            pdf.cell(190, 10, f"Generado el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 0, 1, 'R')
            
            pdf.output(archivo)
            webbrowser.open(archivo)
            
            messagebox.showinfo("Éxito", f"Reporte exportado como {archivo}")
        except Exception as e:
            messagebox.showerror("Error", f"Error al exportar el reporte: {str(e)}")
    
    def exportar_csv(self):
        if not self.tree_reporte.get_children():
            messagebox.showerror("Error", "No hay datos para exportar")
            return
        
        fecha_inicio = self.date_inicio.get()
        fecha_fin = self.date_fin.get()
        
        try:
            ruta_reportes = self.config.get_ruta_reportes()
            if not os.path.exists(ruta_reportes):
                os.makedirs(ruta_reportes)
            
            fecha_actual = datetime.now().strftime('%Y%m%d%H%M%S')
            archivo = os.path.join(ruta_reportes, f"reporte_{fecha_inicio}_{fecha_fin}_{fecha_actual}.csv")
            
            with open(archivo, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['Fecha', 'Ingresos', 'Egresos', 'Total Cobrado'])
                
                for item in self.tree_reporte.get_children():
                    valores = self.tree_reporte.item(item, 'values')
                    total = valores[3].replace('s/.', '') if valores[3].startswith('s/.') else valores[3]
                    writer.writerow([valores[0], valores[1], valores[2], total])
            
            messagebox.showinfo("Éxito", f"Reporte exportado como {archivo}")
        except Exception as e:
            messagebox.showerror("Error", f"Error al exportar el reporte: {str(e)}")
    
    def guardar_config_general(self):
        try:
            self.config.update_config('SETTINGS', 'tesseract_path', self.entry_tesseract.get())
            self.config.update_config('SETTINGS', 'tiempo_apertura_puerta', self.entry_tiempo_puerta.get())
            
            max_espacios = int(self.entry_maxespacios.get())
            if max_espacios <= 0:
                raise ValueError("El número de espacios debe ser mayor a cero")
            
            self.config.update_config('SETTINGS', 'max_espacios', max_espacios)
            self.config.update_config('SETTINGS', 'ruta_reportes', self.entry_rutareportes.get())
            
            messagebox.showinfo("Éxito", "Configuración guardada correctamente")
            
            self.actualizar_espacios()
            self.actualizar_estado()
        except ValueError as e:
            messagebox.showerror("Error", f"Valor inválido: {str(e)}")
        except Exception as e:
            messagebox.showerror("Error", f"Error al guardar configuración: {str(e)}")
    
    def guardar_tarifas(self):
        try:
            tarifa_auto = float(self.entry_tarifa_auto.get())
            tarifa_moto = float(self.entry_tarifa_moto.get())
            tarifa_camioneta = float(self.entry_tarifa_camioneta.get())
            
            if tarifa_auto <= 0 or tarifa_moto <= 0 or tarifa_camioneta <= 0:
                raise ValueError("Las tarifas deben ser mayores a cero")
            
            self.config.update_config('SETTINGS', 'tarifa_auto', tarifa_auto)
            self.config.update_config('SETTINGS', 'tarifa_moto', tarifa_moto)
            self.config.update_config('SETTINGS', 'tarifa_camioneta', tarifa_camioneta)
            
            messagebox.showinfo("Éxito", "Tarifas actualizadas correctamente")
        except ValueError as e:
            messagebox.showerror("Error", f"Valor inválido: {str(e)}")
        except Exception as e:
            messagebox.showerror("Error", f"Error al guardar tarifas: {str(e)}")
    
    def cargar_usuarios(self):
        for item in self.tree_usuarios.get_children():
            self.tree_usuarios.delete(item)
        
        usuarios = self.db.get_usuarios()
        
        for usuario in usuarios:
            self.tree_usuarios.insert("", tk.END, values=(usuario[0], usuario[1], usuario[2]))
    
    def crear_nuevo_usuario(self):
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
            self.entry_nuevo_usuario.delete(0, tk.END)
            self.entry_nuevo_password.delete(0, tk.END)
            self.cargar_usuarios()
        else:
            messagebox.showerror("Error", mensaje)
    
    def cambiar_password_usuario(self):
        seleccion = self.tree_usuarios.selection()
        if not seleccion:
            messagebox.showerror("Error", "Debe seleccionar un usuario")
            return
        
        usuario_id = self.tree_usuarios.item(seleccion, 'values')[0]
        
        nueva_password = simpledialog.askstring("Cambiar contraseña", "Ingrese la nueva contraseña:", show='*')
        if not nueva_password:
            return
        
        if len(nueva_password) < 6:
            messagebox.showerror("Error", "La contraseña debe tener al menos 6 caracteres")
            return
        
        if messagebox.askyesno("Confirmar", "¿Está seguro de cambiar la contraseña de este usuario?"):
            if self.db.cambiar_password(usuario_id, nueva_password):
                messagebox.showinfo("Éxito", "Contraseña cambiada correctamente")
            else:
                messagebox.showerror("Error", "No se pudo cambiar la contraseña")
    
    def eliminar_usuario(self):
        seleccion = self.tree_usuarios.selection()
        if not seleccion:
            messagebox.showerror("Error", "Debe seleccionar un usuario")
            return
        
        usuario_id, username, rol = self.tree_usuarios.item(seleccion, 'values')
        
        if int(usuario_id) == self.usuario_actual:
            messagebox.showerror("Error", "No puede eliminarse a sí mismo")
            return
        
        if messagebox.askyesno("Confirmar", f"¿Está seguro de eliminar al usuario {username} ({rol})?"):
            if self.db.eliminar_usuario(usuario_id):
                messagebox.showinfo("Éxito", "Usuario eliminado correctamente")
                self.cargar_usuarios()
            else:
                messagebox.showerror("Error", "No se pudo eliminar el usuario")
    
    def crear_respaldo(self):
        try:
            archivo = filedialog.asksaveasfilename(
                defaultextension=".db",
                filetypes=[("Archivos de base de datos", "*.db"), ("Todos los archivos", "*.*")],
                title="Guardar respaldo como"
            )
            
            if not archivo:
                return
            
            if self.db.crear_respaldo(archivo):
                messagebox.showinfo("Éxito", f"Respaldo creado correctamente en {archivo}")
            else:
                messagebox.showerror("Error", "No se pudo crear el respaldo")
        except Exception as e:
            messagebox.showerror("Error", f"Error al crear respaldo: {str(e)}")
    
    def restaurar_respaldo(self):
        try:
            archivo = filedialog.askopenfilename(
                filetypes=[("Archivos de base de datos", "*.db"), ("Todos los archivos", "*.*")],
                title="Seleccionar archivo de respaldo"
            )
            
            if not archivo:
                return
            
            if messagebox.askyesno("Confirmar", "¿Está seguro de restaurar desde este respaldo? Todos los datos actuales serán reemplazados."):
                if self.db.restaurar_respaldo(archivo):
                    messagebox.showinfo("Éxito", "Respaldo restaurado correctamente. La aplicación se reiniciará.")
                    self.on_close()
                else:
                    messagebox.showerror("Error", "No se pudo restaurar el respaldo")
        except Exception as e:
            messagebox.showerror("Error", f"Error al restaurar respaldo: {str(e)}")
    
    def sincronizacion_periodica(self):
        while True:
            time.sleep(SYNC_INTERVAL)
            if self.db.sincronizar_datos():
                logging.info("Sincronización completada exitosamente")
            else:
                logging.warning("No se pudo completar la sincronización")
    
    def on_close(self):
        self.db.cerrar()
        self.destroy()

if __name__ == "__main__":
    app = ParkingApp()
    app.mainloop()