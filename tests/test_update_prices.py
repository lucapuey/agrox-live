import importlib.util
import unittest
from datetime import timedelta
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_prices.py"
spec = importlib.util.spec_from_file_location("agrox_update_prices", MODULE_PATH)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class UpdatePricesTests(unittest.TestCase):
    def test_spanish_number_formats(self):
        self.assertAlmostEqual(updater.num("1.020,50"), 1020.50)
        self.assertAlmostEqual(updater.num("5,38"), 5.38)
        self.assertAlmostEqual(updater.num("2244"), 2244.0)
        self.assertIsNone(updater.num("—"))

    def test_merge_observation_upserts_by_date(self):
        item = {"name": "Soja", "observations": []}
        self.assertTrue(updater.merge_obs(item, {"date":"2026-10-01","price":300,"source":"test"}))
        self.assertFalse(updater.merge_obs(item, {"date":"2026-10-01","price":300,"source":"test"}))
        self.assertEqual(len(item["observations"]), 1)
        self.assertTrue(updater.merge_obs(item, {"date":"2026-10-01","price":301,"source":"test"}))
        self.assertEqual(item["observations"][0]["price"], 301)

    def test_real_variation_compound_formula(self):
        annual, deval = 32.0, 10.0
        real = ((1 + annual/100) / (1 + deval/100) - 1) * 100
        self.assertAlmostEqual(real, 20.0, places=8)


    def test_campo_argento_parser_preserves_supplier_observation_date(self):
        data = updater.load_data()
        status = {}
        html = """<html><body>
        <div>UREA Granel | 1 tn</div><div>US$ 615,00/t</div><div>Campo Simple · Observado 12 de oct de 2026</div>
        <div>MAP Granel | Fosfato Monoamónico | 1 tn</div><div>US$ 1.020,00/t</div><div>Campo Simple · Observado 12 de oct de 2026</div>
        </body></html>"""
        original = updater.get_text
        try:
            updater.get_text = lambda _url: html
            count = updater.update_campo_argento(data, status)
        finally:
            updater.get_text = original
        self.assertGreaterEqual(count, 2)
        urea = updater.item_by_name(data["items"], "Urea")
        self.assertAlmostEqual(urea["price"], 615.0)
        self.assertEqual(urea["date"], "2026-10-12")

    def test_surtidores_monthly_table_keeps_month_and_fx(self):
        data = updater.load_data()
        status = {}
        months = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
        rows = []
        for year, val in [(2026, 2244), (2025, 1408)]:
            rows.append('<tr>' + ''.join(f'<td>{x}</td>' for x in [year] + months) + '</tr>')
            rows.append('<tr>' + ''.join(f'<td>{x}</td>' for x in ["Gasoil"] + [val]*10 + ["",""]) + '</tr>')
        html = '<table>' + ''.join(rows) + '</table>'
        original = updater.get_text
        try:
            updater.get_text = lambda _url: html
            fx = [{"date":"2025-10-01","value":1400},{"date":"2026-10-01","value":1500}]
            count = updater.update_surtidores_gasoil(data, status, fx, 1500)
        finally:
            updater.get_text = original
        self.assertGreaterEqual(count, 2)
        gasoil = updater.item_by_name(data["items"], "Gas Oil")
        self.assertEqual(gasoil["ars"], 2244)
        self.assertEqual(gasoil["date"], "2026-10-01")
        self.assertAlmostEqual(gasoil["price"], 2244/1500, places=5)


    def test_public_livestock_pages_update_only_dated_prices(self):
        data = updater.load_data()
        status = {}
        pages = {
            "https://agromove.com.ar/cotizaciones/novillo": "El novillo cotiza $4.214 por kg vivo al 16/10/2026, según MAG Cañuelas.",
            "https://agromove.com.ar/cotizaciones/vaca": "La vaca cotiza $2.916 por kg vivo al 23/10/2026, según MAG Cañuelas.",
            "https://pampaia.ar/agrodata/invernada": "4,19 USD por kilo: terneros 180 a 200 kg $ 6.337 · 12.298 cabezas. Del 01/09/2026 al 30/09/2026. Dólar oficial A3500 promedio del período: $ 1.513,89.",
        }
        original = updater.get_text
        try:
            updater.get_text = lambda url: pages[url]
            fx = [
                {"date":"2026-10-16","value":1500},
                {"date":"2026-10-23","value":1520},
            ]
            count = updater.update_agromove_livestock(data, status, fx)
        finally:
            updater.get_text = original
        self.assertEqual(count, 3)
        novillo = updater.item_by_name(data["items"], "Novillo")
        ternero = updater.item_by_name(data["items"], "Ternero")
        vaca_proxy = updater.item_by_name(data["items"], "Carne vacuna")
        self.assertEqual(novillo["date"], "2026-10-16")
        self.assertAlmostEqual(novillo["price"], 4214/1500, places=5)
        self.assertEqual(ternero["date"], "2026-09-30")
        self.assertAlmostEqual(ternero["price"], 4.19, places=4)
        self.assertIn("Proxy", vaca_proxy["quality"])

    def test_maquinac_scraper_keeps_period_caveat_and_updates_benchmark(self):
        data = updater.load_data()
        status = {}
        page = "En el primer semestre de 2026 el valor promedio fue de U$S 117.792 por equipo, un 3,7% menos que la media de 2025 (U$S 122.330)."
        original = updater.get_text
        try:
            updater.get_text = lambda _url: page
            fx = [
                {"date":"2025-06-30","value":1000},
                {"date":"2026-06-30","value":1400},
            ]
            count = updater.update_maquinac(data, status, fx, 1450)
        finally:
            updater.get_text = original
        self.assertEqual(count, 2)
        item = updater.item_by_name(data["items"], "Maquinaria agrícola")
        self.assertEqual(item["price"], 117792)
        self.assertEqual(item["annual"], 34.81)
        self.assertAlmostEqual(item["real"], -3.71, places=2)
        self.assertIn("enero-junio", item["variationPeriod"])


    def test_freight_reference_requires_tariff_period_and_uses_current_fx(self):
        data = updater.load_data()
        status = {}
        page = """<html><body><div>Flete zona núcleo → Rosario ~$37.500/tn</div>
        <div>Cuadro tarifario — Abril 2026 (último publicado por CATAC)</div></body></html>"""
        original = updater.get_text
        try:
            updater.get_text = lambda _url: page
            count = updater.update_freight_granos_page(data, status, 1500)
        finally:
            updater.get_text = original
        self.assertEqual(count, 1)
        item = updater.item_by_name(data["items"], "Flete agrícola")
        self.assertAlmostEqual(item["price"], 25.0, places=4)
        self.assertEqual(item["ars"], 37500)
        self.assertEqual(item["tariff_reference_date"], "2026-04-01")
        self.assertIn("Proxy estimativo", item["quality"])


    def test_milk_page_fallback_uses_source_month_and_historical_fx(self):
        data = updater.load_data()
        status = {}
        html = """<html><body><h2>Precio Nacional de la Leche · MAGYP</h2>
        <div>Precio por litro $543,01</div><div>Corresponde a Septiembre 2026</div></body></html>"""
        original_text, original_json = updater.get_text, updater.get_json
        try:
            updater.get_text = lambda _url: html
            updater.get_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("API offline"))
            fx_hist = [{"date":"2026-09-30","value":1510}]
            # Use FX history attached to the same dataset, as the production adapter does.
            data["macro"]["fx_history"] = fx_hist
            count = updater.update_milk_from_granos_indicators(data, status, 1550)
        finally:
            updater.get_text, updater.get_json = original_text, original_json
        self.assertEqual(count, 1)
        item = updater.item_by_name(data["items"], "Leche")
        self.assertEqual(item["date"], "2026-09-30")
        self.assertAlmostEqual(item["ars"], 543.01, places=2)
        self.assertAlmostEqual(item["price"], 543.01/1510, places=5)

    def test_update_metrics_uses_matched_one_year_observations(self):
        today = updater.TODAY
        prev_date = (today - timedelta(days=365)).isoformat()
        now_date = today.isoformat()
        item = {
            "id":1,"name":"Soja","unit":"USD/t","price":120,
            "date":now_date,"observations":[
                {"date":prev_date,"price":100,"fx":1000,"source":"test"},
                {"date":now_date,"price":120,"fx":1100,"source":"test"},
            ]
        }
        data = {"items":[item]}
        fx = [
            {"date":prev_date,"value":1000,"source":"test"},
            {"date":now_date,"value":1100,"source":"test"},
        ]
        updater.update_metrics(data, fx)
        self.assertAlmostEqual(item["annual"], 32.0, places=2)
        self.assertAlmostEqual(item["deval"], 10.0, places=2)
        self.assertAlmostEqual(item["real"], 20.0, places=2)
        self.assertEqual(item["variationQuality"], "observed")

    def test_missing_year_comparable_preserves_existing_values(self):
        today = updater.TODAY
        item = {"name":"Urea","unit":"USD/t","price":615,"date":today.isoformat(),
                "annual":9.23,"deval":5.72,"real":3.32,
                "observations":[{"date":today.isoformat(),"price":615,"fx":1000}]}
        updater.update_metrics({"items":[item]}, [{"date":today.isoformat(),"value":1000}])
        self.assertEqual(item["annual"], 9.23)
        self.assertEqual(item["deval"], 5.72)
        self.assertEqual(item["real"], 3.32)


if __name__ == "__main__":
    unittest.main()
