# Imagen base slim con versión fija (no "latest")
FROM python:3.12.8-slim-bookworm

# Evita archivos .pyc y fuerza logs sin buffer; valores por defecto configurables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INPUT_FILE=/data/input/inventario_ferreteria.csv \
    OUTPUT_DIR=/data/output \
    GROUP_BY=categoria \
    LOG_LEVEL=INFO

WORKDIR /app

# 1) Dependencias primero: esta capa queda en caché mientras requirements.txt no cambie
COPY requirements.txt .
RUN pip install -r requirements.txt

# 2) Usuario sin privilegios y carpetas de datos que serán montadas como volúmenes
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app \
 && mkdir -p /data/input /data/output \
 && chown -R app:app /data

# 3) Código al final: cambiar el script no invalida la capa de dependencias
COPY --chown=app:app procesar.py .

USER app

VOLUME ["/data/input", "/data/output"]

# CMD en forma exec (JSON): python es el PID 1 y recibe las señales correctamente
CMD ["python", "procesar.py"]
