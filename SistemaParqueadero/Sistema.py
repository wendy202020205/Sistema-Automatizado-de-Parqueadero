import mysql.connector
from datetime import datetime


def conectar():
    try:
        conexion = mysql.connector.connect(
            host="localhost",
            user="root",  
            password="",  
            database="parqueadero"
        )
        return conexion
    except mysql.connector.Error as err:
        print(f"Error: {err}")
        return None

def registrar_ingreso(placa, tipo, conductor=None):
    conexion = conectar()
    if conexion:
        cursor = conexion.cursor()
        
        cursor.execute("SELECT * FROM vehiculos WHERE placa = %s AND estado = 'activo'", (placa,))
        vehiculo = cursor.fetchone()
        
        if vehiculo:
            print(f"El vehículo con placa {placa} ya está ingresado.")
            conexion.close()
            return
        
       
        cursor.execute("SELECT * FROM espacios WHERE ocupado = FALSE LIMIT 1")
        espacio = cursor.fetchone()

        if not espacio:
            print("No hay espacios disponibles.")
            conexion.close()
            return
        
        espacio_id = espacio[0] 
        
        cursor.execute("UPDATE espacios SET ocupado = TRUE WHERE id = %s", (espacio_id,))
        # Registrar el vehículo
        hora_ingreso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("INSERT INTO vehiculos (placa, tipo, hora_ingreso, espacio_asignado, conductor) VALUES (%s, %s, %s, %s, %s)", 
                       (placa, tipo, hora_ingreso, espacio_id, conductor))
        conexion.commit()
        print(f"Vehículo {placa} registrado con éxito.")
        conexion.close()


def registrar_salida(placa):
    conexion = conectar()
    if conexion:
        cursor = conexion.cursor()
        cursor.execute("SELECT * FROM vehiculos WHERE placa = %s AND estado = 'activo'", (placa,))
        vehiculo = cursor.fetchone()
        
        if not vehiculo:
            print(f"El vehículo con placa {placa} no está registrado.")
            conexion.close()
            return
        
        hora_salida = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tiempo_estacionado = (datetime.now() - vehiculo[3]).total_seconds() / 60  # tiempo en minutos
        espacio_id = vehiculo[5]
        
        cursor.execute("UPDATE vehiculos SET hora_salida = %s, tiempo_estacionado = %s, estado = 'salido' WHERE placa = %s", 
                       (hora_salida, tiempo_estacionado, placa))
        cursor.execute("UPDATE espacios SET ocupado = FALSE WHERE id = %s", (espacio_id,))
        
        # Generar reporte de salida
        cursor.execute("INSERT INTO reportes (fecha, egresos) VALUES (CURDATE(), 1) ON DUPLICATE KEY UPDATE egresos = egresos + 1")
        
        conexion.commit()
        print(f"Vehículo {placa} ha salido. Tiempo de permanencia: {tiempo_estacionado:.2f} minutos.")
        conexion.close()
# Registrar ingreso de un vehículo
def registrar_ingreso(placa, tipo, conductor=None):
    conexion = conectar()
    if conexion:
        cursor = conexion.cursor()
        # Verificar si el vehículo ya está registrado
        cursor.execute("SELECT * FROM vehiculos WHERE placa = %s AND estado = 'activo'", (placa,))
        vehiculo = cursor.fetchone()
        
        if vehiculo:
            print(f"El vehículo con placa {placa} ya está ingresado.")
            conexion.close()
            return
        
        # Buscar espacio libre
        cursor.execute("SELECT * FROM espacios WHERE ocupado = FALSE LIMIT 1")
        espacio = cursor.fetchone()

        if not espacio:
            print("No hay espacios disponibles.")
            conexion.close()
            return
        
        espacio_id = espacio[0]  # ID del espacio
        # Asignar espacio al vehículo
        cursor.execute("UPDATE espacios SET ocupado = TRUE WHERE id = %s", (espacio_id,))
        # Registrar el vehículo
        hora_ingreso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("INSERT INTO vehiculos (placa, tipo, hora_ingreso, espacio_asignado, conductor) VALUES (%s, %s, %s, %s, %s)", 
                       (placa, tipo, hora_ingreso, espacio_id, conductor))
        conexion.commit()
        print(f"Vehículo {placa} registrado con éxito.")
        conexion.close()

# Registrar salida de un vehículo
def registrar_salida(placa):
    conexion = conectar()
    if conexion:
        cursor = conexion.cursor()
        cursor.execute("SELECT * FROM vehiculos WHERE placa = %s AND estado = 'activo'", (placa,))
        vehiculo = cursor.fetchone()
        
        if not vehiculo:
            print(f"El vehículo con placa {placa} no está registrado.")
            conexion.close()
            return
        
        hora_salida = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tiempo_estacionado = (datetime.now() - vehiculo[3]).total_seconds() / 60  # tiempo en minutos
        espacio_id = vehiculo[5]
        
        cursor.execute("UPDATE vehiculos SET hora_salida = %s, tiempo_estacionado = %s, estado = 'salido' WHERE placa = %s", 
                       (hora_salida, tiempo_estacionado, placa))
        cursor.execute("UPDATE espacios SET ocupado = FALSE WHERE id = %s", (espacio_id,))
        
        # Generar reporte de salida
        cursor.execute("INSERT INTO reportes (fecha, egresos) VALUES (CURDATE(), 1) ON DUPLICATE KEY UPDATE egresos = egresos + 1")
        
        conexion.commit()
        print(f"Vehículo {placa} ha salido. Tiempo de permanencia: {tiempo_estacionado:.2f} minutos.")
        conexion.close()
# Generar reporte diario
def generar_reporte_diario():
    conexion = conectar()
    if conexion:
        cursor = conexion.cursor()
        fecha = datetime.now().strftime('%Y-%m-%d')
        cursor.execute("SELECT * FROM reportes WHERE fecha = %s", (fecha,))
        reporte = cursor.fetchone()
        
        if reporte:
            print(f"Reporte del {fecha}:")
            print(f"Ingresos: {reporte[2]}")
            print(f"Egresos: {reporte[3]}")
        else:
            print("No hay reportes para hoy.")
        conexion.close()
        
import tkinter as tk
from tkinter import messagebox

# Crear ventana principal
import tkinter as tk
from tkinter import ttk

# Suponiendo que estas funciones ya existen
def registrar_ingreso(placa, tipo, conductor):
    print(f"[REGISTRO INGRESO] Placa: {placa}, Tipo: {tipo}, Conductor: {conductor}")

def registrar_salida(placa):
    print(f"[REGISTRO SALIDA] Placa: {placa}")

def ventana_principal():
    ventana = tk.Tk()
    ventana.title("Sistema de Parqueadero")

    # --- Funciones internas ---
    def ingreso():
        placa = entrada_placa.get()
        tipo = combo_tipo.get()
        conductor = entrada_conductor.get() or None
        registrar_ingreso(placa, tipo, conductor)

    def salida():
        placa = entrada_placa.get()
        registrar_salida(placa)

    # --- Campos ---
    tk.Label(ventana, text="Placa:").grid(row=0, column=0, padx=5, pady=5)
    entrada_placa = tk.Entry(ventana)
    entrada_placa.grid(row=0, column=1, padx=5, pady=5)

    tk.Label(ventana, text="Tipo de vehículo:").grid(row=1, column=0, padx=5, pady=5)
    tipos_vehiculo = ["Auto", "Moto", "Camioneta", "Furgoneta", "Microbus", "Vehículo eléctrico", "Taxi"]
    combo_tipo = ttk.Combobox(ventana, values=tipos_vehiculo, state="readonly")
    combo_tipo.set("Seleccione tipo")
    combo_tipo.grid(row=1, column=1, padx=5, pady=5)

    tk.Label(ventana, text="Conductor (opcional):").grid(row=2, column=0, padx=5, pady=5)
    entrada_conductor = tk.Entry(ventana)
    entrada_conductor.grid(row=2, column=1, padx=5, pady=5)

    # --- Botones ---
    tk.Button(ventana, text="Registrar Ingreso", command=ingreso).grid(row=3, column=0, padx=5, pady=10)
    tk.Button(ventana, text="Registrar Salida", command=salida).grid(row=3, column=1, padx=5, pady=10)

    ventana.mainloop()

# Iniciar la aplicación
ventana_principal()
