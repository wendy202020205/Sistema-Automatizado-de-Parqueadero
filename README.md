🚗 Sistema Automatizado de Parqueadero con Reconocimiento Facial y Captura de Placas
Este proyecto es un sistema inteligente para la gestión de parqueaderos, que permite el inicio de sesión mediante reconocimiento facial y la lectura automática de placas vehiculares usando visión por computadora. Fue desarrollado en Python con una interfaz gráfica en Tkinter y almacenamiento de datos en SQLite3.

🧠 Funcionalidades
🔐 Inicio de sesión por reconocimiento facial usando OpenCV + Dlib + LBPH

🎥 Captura automática de placas mediante procesamiento de imagen

🕗 Registro de entrada y salida de vehículos con fecha y hora

📂 Base de datos local en SQLite3 para almacenar usuarios, registros e imágenes

🖥️ Interfaz gráfica amigable creada con Tkinter

📸 Almacenamiento de imágenes de rostro y placas como respaldo

📊 Panel con historial de accesos y búsqueda por fecha o placa

🛠️ Tecnologías y Librerías
Lenguaje principal: Python 3.x

Interfaz gráfica: Tkinter

Reconocimiento facial: OpenCV, Dlib, LBPH (Local Binary Patterns Histograms)

Lectura de placas: OpenCV + técnicas básicas de OCR

Base de datos: SQLite3

Otros: PIL (para manejo de imágenes), datetime, os
![image](https://github.com/user-attachments/assets/7a55a178-8c85-4f2d-82c3-f0310444ed65)
![image](https://github.com/user-attachments/assets/bc5f61b1-3cc3-4652-b8a7-8971a0a9e811)

⚙️ Instalación
🔧 Requisitos Previos
Python 3.x

pip

📦 Instalación de Dependencias
pip install opencv-python
pip install dlib
pip install pillow

🚀 Ejecución
pythonParking.py
Asegúrate de tener una cámara conectada. La base de datos se crea automáticamente si no existe.

🗃️ Estructura del Proyecto
bash
Copiar código
/rostros              -> Imágenes de rostros
/modelos               -> Modelos
/database.db          -> Base de datos SQLite3
/sistemaParking.py              -> Archivo principal con la interfaz
/facial_recognition/  -> Módulos de entrenamiento y detección facial
/plate_detection/     -> Captura y procesamiento de placas
🎯 Cómo Funciona
El usuario se registra capturando su rostro.

El sistema usa LBPH + Dlib para reconocer al usuario al ingresar.

Se activa la cámara para capturar la placa del vehículo.

Se registran en la base de datos: usuario, hora, fecha y número de placa.

Desde el panel se puede ver el historial de entradas/salidas.




