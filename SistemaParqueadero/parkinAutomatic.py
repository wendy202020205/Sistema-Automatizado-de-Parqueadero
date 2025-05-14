import cv2
import pytesseract
import sqlite3
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk
from PIL import Image, ImageTk
import os
import logging
import configparser
import threading
import numpy as np

# ******************** CONFIGURACIÓN INICIAL ********************
# 📝 Configuración de logs
logging.basicConfig(
    filename='estacionamiento.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# ⚙️ Cargar configuración
config = configparser.ConfigParser()
config.read('config.ini')
TESSERACT_PATH = config.get('Settings', 'tesseract_path', 
                          fallback=r'C:\Program Files\Tesseract-OCR\tesseract.exe')
TARIFA_POR_HORA = config.getfloat('Settings', 'tarifa_por_hora', fallback=2.0)
TIEMPO_APERTURA_PUERTA = config.getint('Settings', 'tiempo_apertura_puerta', fallback=3000)

pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# ******************** BASE DE DATOS ********************
class DatabaseManager:
    def __init__(self):
        self.conn = sqlite3.connect("estacionamiento.db", check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._initialize_db()
        
    def _initialize_db(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS movimientos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                placa TEXT,
                hora_entrada TEXT,
                hora_salida TEXT,
                tarifa REAL
            )
        ''')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_placa ON movimientos(placa)')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_hora_salida ON movimientos(hora_salida)')
        self.conn.commit()
        
    def execute_query(self, query, params=()):
        try:
            self.cursor.execute(query, params)
            self.conn.commit()
            return self.cursor
        except sqlite3.Error as e:
            logging.error(f"Error en base de datos: {e}")
            raise

db = DatabaseManager()

# ******************** PROCESAMIENTO DE IMÁGENES ********************
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
            
            # Procesamiento final para OCR
            _, thresh = cv2.threshold(plate_cropped, 150, 255, cv2.THRESH_BINARY)
            return pytesseract.image_to_string(
                thresh, 
                config='--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
            ).strip().replace(" ", "").replace("\n", "")
        except Exception as e:
            logging.error(f"Error en OCR: {e}")
            return ""

# ******************** LÓGICA PRINCIPAL ********************
class ParkingSystem:
    @staticmethod
    def validar_placa(placa):
        return 6 <= len(placa) <= 7 and placa.isalnum()
    
    @staticmethod
    def calcular_tarifa(duracion_min):
        return round((duracion_min / 60) * TARIFA_POR_HORA, 2)
    
    @staticmethod
    def capturar_placa():
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            logging.error("No se pudo acceder a la cámara")
            return None
            
        filename = "placa_temp.jpg"
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                cv2.imshow("Captura de Placa", frame)
                key = cv2.waitKey(1)
                if key % 256 == 32:  # Tecla ESPACIO
                    cv2.imwrite(filename, frame)
                    break
        finally:
            cap.release()
            cv2.destroyAllWindows()
        
        placa = PlateRecognizer.read_plate(filename)
        os.remove(filename)
        
        if ParkingSystem.validar_placa(placa):
            return placa
        return None

# ******************** INTERFAZ GRÁFICA ********************
class ParkingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sistema de Estacionamiento Automatizado")
        self.geometry("400x450")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self._create_widgets()
    
    def _create_widgets(self):
        frame = tk.Frame(self, padx=20, pady=20)
        frame.pack()
        
        self.lbl_estado = tk.Label(frame, text="Estado: Listo", fg="green")
        self.lbl_estado.pack(pady=5)
        
        self.lbl_puerta = tk.Label(frame, text="Puerta: Cerrada", fg="red")
        self.lbl_puerta.pack(pady=5)
        
        btn_style = {'width': 25, 'height': 2}
        tk.Button(frame, text="Registrar Entrada", command=self.registrar_entrada, **btn_style).pack(pady=5)
        tk.Button(frame, text="Registrar Salida", command=self.registrar_salida, **btn_style).pack(pady=5)
        tk.Button(frame, text="Ver Historial", command=self.ver_historial, **btn_style).pack(pady=5)
        tk.Button(frame, text="Generar Reporte", command=self.generar_reporte, **btn_style).pack(pady=5)
        tk.Button(frame, text="Salir", command=self.on_close, **btn_style).pack(pady=10)
    
    def actualizar_estado(self, mensaje, color="black"):
        self.lbl_estado.config(text=mensaje, fg=color)
    
    def abrir_puerta(self):
        self.lbl_puerta.config(text="Puerta: Abierta", fg="green")
        self.after(TIEMPO_APERTURA_PUERTA, lambda: self.lbl_puerta.config(text="Puerta: Cerrada", fg="red"))
    
    def registrar_entrada(self):
        self.actualizar_estado("Procesando entrada...", "blue")
        threading.Thread(target=self._procesar_entrada, daemon=True).start()
    
    def _procesar_entrada(self):
        try:
            placa = ParkingSystem.capturar_placa()
            if placa:
                res = db.execute_query(
                    "SELECT * FROM movimientos WHERE placa=? AND hora_salida IS NULL",
                    (placa,)
                ).fetchone()
                
                if res:
                    self.after(0, lambda: messagebox.showwarning("Duplicado", f"Placa {placa} ya registrada"))
                else:
                    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    db.execute_query(
                        "INSERT INTO movimientos (placa, hora_entrada) VALUES (?, ?)",
                        (placa, ahora)
                    )
                    self.after(0, lambda: (
                        self.abrir_puerta(),
                        messagebox.showinfo("Entrada Registrada", f"Placa: {placa}\nHora: {ahora}")
                    ))
        except Exception as e:
            logging.error(f"Error en entrada: {e}")
            self.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.after(0, lambda: self.actualizar_estado("Listo", "green"))
    
    def registrar_salida(self):
        self.actualizar_estado("Procesando salida...", "blue")
        threading.Thread(target=self._procesar_salida, daemon=True).start()
    
    def _procesar_salida(self):
        try:
            placa = ParkingSystem.capturar_placa()
            if placa:
                res = db.execute_query(
                    "SELECT id, hora_entrada FROM movimientos WHERE placa=? AND hora_salida IS NULL",
                    (placa,)
                ).fetchone()
                
                if res:
                    id_mov, hora_entrada = res
                    entrada_dt = datetime.strptime(hora_entrada, "%Y-%m-%d %H:%M:%S")
                    salida_dt = datetime.now()
                    duracion = (salida_dt - entrada_dt).total_seconds() / 60
                    tarifa = ParkingSystem.calcular_tarifa(duracion)
                    
                    db.execute_query(
                        "UPDATE movimientos SET hora_salida=?, tarifa=? WHERE id=?",
                        (salida_dt.strftime("%Y-%m-%d %H:%M:%S"), tarifa, id_mov)
                    )
                    self.after(0, lambda: (
                        self.abrir_puerta(),
                        messagebox.showinfo("Salida Registrada",
                            f"Placa: {placa}\nTiempo: {int(duracion)} min\nTarifa: S/. {tarifa}")
                    ))
                else:
                    self.after(0, lambda: messagebox.showwarning("Error", "Placa no encontrada"))
        except Exception as e:
            logging.error(f"Error en salida: {e}")
            self.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.after(0, lambda: self.actualizar_estado("Listo", "green"))
    
    def ver_historial(self):
        try:
            res = db.execute_query(
                "SELECT placa, hora_entrada, hora_salida, tarifa FROM movimientos ORDER BY id DESC"
            ).fetchall()
            
            historial = tk.Toplevel(self)
            historial.title("Historial")
            tree = ttk.Treeview(historial, columns=("Placa", "Entrada", "Salida", "Tarifa"), show="headings")
            tree.heading("Placa", text="Placa")
            tree.heading("Entrada", text="Entrada")
            tree.heading("Salida", text="Salida")
            tree.heading("Tarifa", text="Tarifa (S/.)")
            
            for row in res:
                tree.insert("", tk.END, values=row)
            
            tree.pack(fill=tk.BOTH, expand=True)
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def generar_reporte(self):
        try:
            res = db.execute_query(
                "SELECT DATE(hora_entrada), COUNT(*), SUM(tarifa) "
                "FROM movimientos WHERE hora_salida IS NOT NULL "
                "GROUP BY DATE(hora_entrada)"
            ).fetchall()
            
            reporte = tk.Toplevel(self)
            reporte.title("Reporte Diario")
            text = tk.Text(reporte)
            text.pack()
            
            text.insert(tk.END, "Fecha\tVehiculos\tIngresos\n")
            for fecha, vehiculos, ingresos in res:
                text.insert(tk.END, f"{fecha}\t{vehiculos}\tS/. {ingresos:.2f}\n")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def on_close(self):
        db.conn.close()
        self.destroy()

if __name__ == "__main__":
    app = ParkingApp()
    app.mainloop()