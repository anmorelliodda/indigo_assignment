"""Offline checks for the assortment map. No SOAP, Azure or customer data."""

import csv
import re
import unittest
from pathlib import Path

from catalog import Catalog, category_key
from models import ProductFilters

ROOT = Path(__file__).resolve().parents[1]


class CatalogPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.goal = (ROOT / 'docs/agent-prompts/agent-goal.txt').read_text(encoding='utf-8')
        cls.guide = cls.goal.split('ASSORTIMENTO: MAPPA ORIENTATIVA PER LA RICERCA\n', 1)[1].split('RICERCA E RACCOMANDAZIONI\n', 1)[0]
        with (ROOT / 'data/gift-shop-catalog.csv').open(encoding='utf-8', newline='') as file:
            cls.rows = list(csv.DictReader(file))

    def test_map_covers_actual_categories_and_subcategories(self):
        actual = {}
        for row in self.rows:
            actual.setdefault(category_key(row['category']), set()).add(row['subcategory'])
        mapped = {}
        for line in self.guide.splitlines():
            if line.startswith('- '):
                name, body = line[2:].split(': ', 1)
                mapped[category_key(name)] = set(body.split('. ', 1)[0].split(', '))
        self.assertEqual(mapped, actual)
        self.assertEqual(len(self.rows), 152)

    def test_guide_is_orientation_not_fixed_product_or_system_facts(self):
        self.assertNotRegex(self.guide, r'\b[A-Z]{2}-\d{3}\b|\d+\s*(?:EUR|euro|€)')
        for internal in ('SOAP', 'Azure', 'ETag', 'BM25', 'CSV'):
            self.assertNotIn(internal, self.guide)
        self.assertIn('prevalgono i risultati dei tool', self.guide)
        self.assertIn('Non dedurre fasce di prezzo', self.guide)

    def test_family_search_includes_headphones_and_excludes_desk_accessories(self):
        catalog = Catalog(ROOT / 'data/gift-shop-catalog.csv')
        matches = catalog.search(ProductFilters(query='*', category=['Tech & Gadgets'], subcategory=['Audio']))
        self.assertEqual(set(matches), {row['product_id'] for row in self.rows if row['subcategory'] == 'Audio'})
        self.assertIn('TG-001', matches)
        self.assertNotIn('TG-015', matches)
        self.assertTrue(all(score is None for score in matches.values()))

    def test_return_read_and_replay_rules_are_explicit(self):
        self.assertIn('options e status inviano SOLO operation, order_number, email', self.goal)
        self.assertIn('mai draft_id, lines o reason', self.goal)
        self.assertIn('correggi il body e ripeti solo quella lettura', self.goal)
        self.assertIn('chiama nuovamente get_order_status', self.goal)
        self.assertIn('non e stato effettuato un nuovo invio', self.goal)

    def test_guide_preserves_requirements_and_non_exhaustive_results(self):
        for rule in ('Mantieni budget e disponibilita', 'next_offset', 'prima di un\'esclusione', 'non alzare il budget senza il cliente'):
            self.assertIn(rule, self.guide)
        self.assertIn('RESI REALI: PROPOSTA, CONSENSO E INVIO', self.goal)
        self.assertIn('Non convertire mai un CONFERMO di prova', self.goal)
