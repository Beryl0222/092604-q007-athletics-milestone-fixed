import json
import unittest

from athletics_milestone.api import handle
from athletics_milestone.service import Service
from athletics_milestone.store import Store


class 基础行为测试(unittest.TestCase):
    def test_health(self):
        result = json.loads(handle('{"action":"health"}', Service(Store())))
        self.assertEqual(result["status"], "ok")

    def test_register_and_find(self):
        service = Service(Store())
        created = service.register("r-1", "owner-1")
        self.assertEqual(created["state"], "draft")
        self.assertEqual(service.find("r-1")["owner_id"], "owner-1")


if __name__ == "__main__":
    unittest.main()
