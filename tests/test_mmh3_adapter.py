import sys
import types
import unittest
from unittest.mock import patch

import longcaster.mmh3_adapter as adapter


class MMH3DiscoveryTests(unittest.TestCase):
    def tearDown(self):
        adapter._MMH3_API = None

    def test_discovers_already_loaded_synthetic_custom_node_package(self):
        fake = types.ModuleType("custom_nodes.synthetic.mmh3_media")
        for name in (
            "MMH3Media",
            "build_h3_continuation_handover",
            "get_resource_payload",
            "load_archive",
            "pack_h3_result",
            "resolve_reference_set",
            "save_archive",
        ):
            setattr(fake, name, object())
        with patch.dict(sys.modules, {fake.__name__: fake}):
            adapter._MMH3_API = None
            self.assertIs(adapter.mmh3_api(), fake)


if __name__ == "__main__":
    unittest.main()
