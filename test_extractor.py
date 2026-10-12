import tempfile
import unittest
from pathlib import Path

import extractor
from cloud_extract import prepare_cloud_config


class ExtractorUnitTests(unittest.TestCase):
    def test_parse_years_supports_ranges_and_commas(self):
        self.assertEqual(extractor.parse_years("2020-2022,2024,2022"), [2020, 2021, 2022, 2024])

    def test_parse_years_rejects_pre_supreme_court_year(self):
        with self.assertRaises(ValueError):
            extractor.parse_years("1949")

    def test_pdf_url_uses_real_english_filename_suffix(self):
        self.assertEqual(
            extractor.pdf_url(2023, "2023_16_872_887"),
            "https://indian-supreme-court-judgments.s3.amazonaws.com/data/pdf/year=2023/english/2023_16_872_887_EN.pdf",
        )

    def test_enrichment_never_invents_analysis(self):
        row = extractor.enrich_row(
            {"year": "2023", "path": "sample", "title": "A v. B", "description": "Published source note", "judge": "A J, B J"},
            include_pdf_text=False,
            pdf_cache=Path(tempfile.gettempdir()),
            progress=None,
            cancel=None,
        )
        self.assertEqual(row["source_headnote"], "Published source note")
        self.assertEqual(row["ai_headnote"], "")
        self.assertEqual(row["ratio"], "")
        self.assertEqual(row["citation_treatment"], "")
        self.assertEqual(row["analysis_status"], "not_run")

    def test_coram_and_bench_strength_prefer_published_html(self):
        row = extractor.enrich_row(
            {
                "year": "2023",
                "path": "sample",
                "judge": "AUTHOR JUDGE",
                "raw_html": "<strong>Coram : AUTHOR JUDGE<sup>*</sup>, SECOND JUDGE</strong> <span> Bench :</span><font> 2 Judges</font>",
            },
            include_pdf_text=False,
            pdf_cache=Path(tempfile.gettempdir()),
            progress=None,
            cancel=None,
        )
        self.assertEqual(row["judges_normalized"], "AUTHOR JUDGE | SECOND JUDGE")
        self.assertEqual(row["bench_strength"], "2")

    def test_schema_has_every_unique_column(self):
        self.assertEqual(len(extractor.ALL_COLUMNS), len(set(extractor.ALL_COLUMNS)))
        self.assertTrue(set(extractor.DEFAULT_COLUMNS).issubset(extractor.ALL_COLUMNS))

    def test_cloud_config_enforces_bounded_exports(self):
        prepared = prepare_cloud_config({"years": "2020-2024", "limit": 1000, "format": "csv"})
        self.assertEqual(prepared["years"], [2020, 2021, 2022, 2023, 2024])
        with self.assertRaises(ValueError):
            prepare_cloud_config({"years": "2019-2024", "limit": 10, "format": "csv"})
        with self.assertRaises(ValueError):
            prepare_cloud_config({"years": "2023", "limit": 1001, "format": "csv"})

    def test_cloud_config_rejects_unavailable_serverless_features(self):
        with self.assertRaises(ValueError):
            prepare_cloud_config({"years": "2023", "limit": 10, "format": "sqlite"})
        with self.assertRaises(ValueError):
            prepare_cloud_config({"years": "2023", "limit": 10, "format": "csv", "include_pdf_text": True})


if __name__ == "__main__":
    unittest.main()
