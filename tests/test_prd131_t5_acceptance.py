import unittest
from replay_gate import replay_gate

class AcceptanceTests(unittest.TestCase):
    def test_full_chain(self):
        self.assertTrue(replay_gate({"status": "MATCH"})["allow"])
        self.assertFalse(replay_gate({"status": "MISMATCH"})["allow"])

if __name__ == "__main__":
    unittest.main()
