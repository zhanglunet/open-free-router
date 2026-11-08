import sys, unittest
from pathlib import Path
from open_free_router.catalog_search import search_catalog

class CatalogTests(unittest.TestCase):
    def test_search(self):
        registry = [{"name": "openai", "description": "OpenAI API"}, {"name": "anthropic", "description": "Claude API"}]
        self.assertEqual(len(search_catalog(registry, "openai")), 1)
        self.assertEqual(len(search_catalog(registry, "api")), 2)

if __name__ == "__main__":
    unittest.main()
