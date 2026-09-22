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

    def test_legacy_reference_without_contract_is_defaulted_read_only(self):
        descriptor = {
            "id": "res_legacy", "kind": "image", "role": "reference",
            "order": 0, "path": "resources/image.pt",
        }

        class Ref:
            def __init__(self, value):
                self.descriptor = value

        class Packet:
            manifest = {"id": "packet-id", "name": "legacy references"}

            def filter_resource_refs(self, *, role):
                return [Ref(descriptor)] if role == "reference" else []

            def get_by_id(self, resource_id):
                self.assert_id = resource_id
                return descriptor

        fake_api = types.SimpleNamespace(
            get_resource_payload=lambda _packet, _raw: "image-payload",
            resolve_reference_set=lambda *_args, **_kwargs: self.fail(
                "modern resolver must not receive a contractless legacy descriptor"
            ),
        )
        packet = Packet()
        with patch.object(adapter, "mmh3_api", return_value=fake_api):
            snapshot = adapter.reference_snapshot(packet)
            materialized = adapter.materialize_native_references(
                packet, width=576, height=576, target_frames=73,
            )

        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["resources"][0]["id"], "res_legacy")
        self.assertEqual(
            snapshot["diagnostics"][0]["code"],
            "legacy_reference_contract_defaulted",
        )
        self.assertEqual(materialized.images, {"ref_image_0": "image-payload"})
        self.assertNotIn("extensions", descriptor)


if __name__ == "__main__":
    unittest.main()
