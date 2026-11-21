"""
Procesador de inventario de una ferretería.

Limpia un CSV de inventario, calcula el valor del inventario y el margen de
cada producto, detecta los productos que hay que reponer y genera un resumen
agrupado.

Toda la configuración se toma de variables de entorno (o argumentos), nunca de
rutas fijas en el código:

    INPUT_FILE   CSV de entrada              (defecto /data/input/inventario_ferreteria.csv)
    OUTPUT_DIR   Carpeta de salida           (defecto /data/output)
    GROUP_BY     categoria | proveedor | marca  (defecto categoria)
    LOG_LEVEL    DEBUG | INFO | WARNING      (defecto INFO)

Salidas en OUTPUT_DIR:
    inventario_limpio.csv   Productos válidos con valor, margen y estado
    resumen.csv             Totales por grupo
    reposicion.csv          Productos agotados o bajo el stock mínimo
    reporte.json            Métricas generales del proceso
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger("procesador-inventario")

COLUMNAS = ["codigo", "producto", "categoria", "marca", "unidad", "stock",
            "stock_minimo", "costo_unitario", "precio_venta", "proveedor",
            "fecha_ultimo_ingreso"]
NUMERICAS = ["stock", "stock_minimo", "costo_unitario", "precio_venta"]
GRUPOS_VALIDOS = ("categoria", "proveedor", "marca")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Limpia y analiza el inventario de una ferretería.")
    p.add_argument("--input", default=os.getenv("INPUT_FILE", "/data/input/inventario_ferreteria.csv"))
    p.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", "/data/output"))
    p.add_argument("--group-by", default=os.getenv("GROUP_BY", "categoria"), choices=GRUPOS_VALIDOS)
    return p.parse_args()


def limpiar(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Devuelve el inventario limpio y un conteo de filas descartadas por motivo."""
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    faltantes = set(COLUMNAS) - set(df.columns)
    if faltantes:
        raise KeyError(f"Faltan columnas en el CSV: {sorted(faltantes)}")
    df = df[COLUMNAS].astype("string")

    # Texto: quitar espacios y unificar mayúsculas
    for col in ("codigo", "producto", "categoria", "marca", "unidad", "proveedor"):
        df[col] = df[col].str.strip().replace("", pd.NA)
    df["codigo"] = df["codigo"].str.upper()
    df["categoria"] = df["categoria"].str.lower().str.capitalize()

    # Números: aceptar coma decimal ("1,05") y convertir
    for col in NUMERICAS:
        df[col] = pd.to_numeric(df[col].str.replace(",", ".", regex=False), errors="coerce")
    df["fecha_ultimo_ingreso"] = pd.to_datetime(df["fecha_ultimo_ingreso"],
                                                format="%Y-%m-%d", errors="coerce")

    descartes = {}

    def quitar(mascara: pd.Series, motivo: str) -> None:
        nonlocal df
        n = int(mascara.sum())
        if n:
            descartes[motivo] = n
            df = df[~mascara]

    quitar(df.duplicated(), "duplicado")
    quitar(df[["codigo", "producto", "categoria"] + NUMERICAS].isna().any(axis=1),
           "dato obligatorio vacío o no numérico")
    quitar(df["codigo"].duplicated(keep="first"), "código repetido")
    quitar((df["stock"] < 0) | (df["stock_minimo"] < 0), "stock negativo")
    quitar(df["precio_venta"] < df["costo_unitario"], "precio menor al costo")

    fechas_malas = int(df["fecha_ultimo_ingreso"].isna().sum())
    if fechas_malas:
        log.warning("%d producto(s) con fecha inválida: se conservan con fecha vacía", fechas_malas)

    df = df.copy()
    df["stock"] = df["stock"].astype(int)
    df["stock_minimo"] = df["stock_minimo"].astype(int)
    df["valor_inventario"] = (df["stock"] * df["costo_unitario"]).round(2)
    df["margen_pct"] = ((df["precio_venta"] - df["costo_unitario"]) / df["precio_venta"] * 100).round(1)
    df["estado"] = "OK"
    df.loc[df["stock"] < df["stock_minimo"], "estado"] = "REPONER"
    df.loc[df["stock"] == 0, "estado"] = "AGOTADO"
    df["fecha_ultimo_ingreso"] = df["fecha_ultimo_ingreso"].dt.strftime("%Y-%m-%d")
    return df.sort_values("codigo").reset_index(drop=True), descartes


def resumir(df: pd.DataFrame, group_by: str) -> pd.DataFrame:
    return (
        df.groupby(group_by)
        .agg(
            productos=("codigo", "count"),
            unidades=("stock", "sum"),
            valor_inventario=("valor_inventario", "sum"),
            margen_prom_pct=("margen_pct", "mean"),
            por_reponer=("estado", lambda s: int((s != "OK").sum())),
        )
        .round(2)
        .sort_values("valor_inventario", ascending=False)
        .reset_index()
    )


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    entrada, salida = Path(args.input), Path(args.output_dir)

    if not entrada.is_file():
        log.error("No se encontró el archivo de entrada: %s", entrada)
        return 1
    salida.mkdir(parents=True, exist_ok=True)

    log.info("Leyendo %s", entrada)
    crudo = pd.read_csv(entrada, dtype=str, keep_default_na=False)
    limpio, descartes = limpiar(crudo)
    if limpio.empty:
        log.error("No quedaron productos válidos.")
        return 2
    log.info("Filas leídas: %d | productos válidos: %d | descartadas: %d",
             len(crudo), len(limpio), len(crudo) - len(limpio))
    for motivo, n in descartes.items():
        log.info("  descartadas por %s: %d", motivo, n)

    resumen = resumir(limpio, args.group_by)
    reponer = limpio[limpio["estado"] != "OK"].copy()
    reponer["cantidad_sugerida"] = reponer["stock_minimo"] * 2 - reponer["stock"]
    reponer = reponer[["codigo", "producto", "categoria", "proveedor", "stock",
                       "stock_minimo", "cantidad_sugerida", "estado"]]

    limpio.to_csv(salida / "inventario_limpio.csv", index=False)
    resumen.to_csv(salida / "resumen.csv", index=False)
    reponer.to_csv(salida / "reposicion.csv", index=False)
    reporte = {
        "generado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archivo_entrada": str(entrada),
        "agrupado_por": args.group_by,
        "filas_leidas": int(len(crudo)),
        "productos_validos": int(len(limpio)),
        "filas_descartadas": descartes,
        "unidades_en_stock": int(limpio["stock"].sum()),
        "valor_total_inventario": round(float(limpio["valor_inventario"].sum()), 2),
        "margen_promedio_pct": round(float(limpio["margen_pct"].mean()), 1),
        "productos_agotados": int((limpio["estado"] == "AGOTADO").sum()),
        "productos_por_reponer": int((limpio["estado"] == "REPONER").sum()),
    }
    (salida / "reporte.json").write_text(json.dumps(reporte, indent=2, ensure_ascii=False),
                                         encoding="utf-8")

    log.info("Resumen por %s:\n%s", args.group_by, resumen.to_string(index=False))
    log.info("Valor total del inventario: $%.2f | agotados: %d | por reponer: %d",
             reporte["valor_total_inventario"], reporte["productos_agotados"],
             reporte["productos_por_reponer"])
    log.info("Archivos escritos en %s", salida)
    return 0


if __name__ == "__main__":
    sys.exit(main())
