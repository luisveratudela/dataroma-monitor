#!/usr/bin/env python3
"""
Dataroma Monitor — Pipeline Runner
Uso: python pipeline/run.py [--only-edgar] [--only-dataroma] [--force]

Ejecutar manualmente o via cron trimestral (GitHub Actions).
"""
import argparse, sys, os, time

# Asegurar que el directorio raíz esté en el path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sec_edgar_parser import fetch_all_funds
from dataroma_scraper  import scrape_all
from data_processor    import process

def main():
    parser = argparse.ArgumentParser(description="Actualiza data.json con datos reales de 13F")
    parser.add_argument("--only-edgar",    action="store_true", help="Solo SEC EDGAR, skip Dataroma")
    parser.add_argument("--only-dataroma", action="store_true", help="Solo Dataroma, skip EDGAR")
    parser.add_argument("--force",         action="store_true", help="Forzar re-descarga aunque existan datos")
    args = parser.parse_args()

    start = time.time()
    print("=" * 50)
    print("  Dataroma Monitor — Pipeline")
    print("=" * 50)

    # SEC EDGAR
    if not args.only_dataroma:
        print("\n[1/3] SEC EDGAR 13F Filings...")
        try:
            funds = fetch_all_funds(output_dir="raw/funds")
            print(f"  → {len(funds)} fondos procesados")
        except Exception as e:
            print(f"  ✗ Error en SEC EDGAR: {e}")
            if not args.only_edgar:
                print("  Continuando con Dataroma...")

    # Dataroma
    if not args.only_edgar:
        print("\n[2/3] Dataroma Grand Portfolio...")
        try:
            scrape_all(output_dir="raw")
        except Exception as e:
            print(f"  ✗ Error en Dataroma: {e}")
            print("  Continuando con datos anteriores si existen...")

    # Procesar
    print("\n[3/3] Generando data.json...")
    try:
        data = process(
            funds_dir="raw/funds",
            dataroma_path="raw/grand_portfolio.json",
            output_path="output/data.json",
        )
        elapsed = time.time() - start
        print(f"\n✓ Listo en {elapsed:.0f}s → output/data.json")
        print(f"  Periodo: {data['quarter']} · {data['totalInvestors']} inversores")
    except Exception as e:
        print(f"  ✗ Error en procesamiento: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
