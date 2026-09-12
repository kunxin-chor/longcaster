import unittest

from longcaster.prompt_sections import (
    PROMPT_SECTION_NAMES,
    assemble_prompt,
    copy_sections,
    edit_sections,
    empty_prompt_sections,
    hash_prompt,
    inherited_prompt_sections,
    parse_legacy_prompt,
    prompt_fields,
    section_record,
    validate_prompt_sections,
)


class PromptSectionTests(unittest.TestCase):
    def test_assembly_is_ordered_deterministic_and_preserves_text(self):
        sections = empty_prompt_sections()
        values = {
            "subject_definitions": "  <Subject 1>\nLine two  ",
            "summary": "Walk forward.",
            "retention_analysis": "Keep the cap.",
            "detailed_description": "Camera tracks.\n",
            "overall_soundscape": "Pool ambience.",
            "non_diegetic_music": "",
        }
        sections = edit_sections(sections, values)
        assembled = assemble_prompt(sections)
        positions = [assembled.index(f"<{name}>") for name in PROMPT_SECTION_NAMES]
        self.assertEqual(positions, sorted(positions))
        parsed = parse_legacy_prompt(assembled)
        self.assertIsNotNone(parsed)
        self.assertEqual({name: parsed[name]["text"] for name in PROMPT_SECTION_NAMES}, values)
        self.assertEqual(assemble_prompt(sections), assembled)
        self.assertEqual(hash_prompt(assembled), hash_prompt(assemble_prompt(sections)))

    def test_parser_rejects_flat_missing_duplicate_and_out_of_order_prompts(self):
        self.assertIsNone(parse_legacy_prompt("flat prompt"))
        sections = empty_prompt_sections()
        assembled = assemble_prompt(sections)
        self.assertIsNone(parse_legacy_prompt(assembled.replace("<summary>", "<summary><summary>", 1)))
        blocks = [f"<{name}>\n\n</{name}>" for name in PROMPT_SECTION_NAMES]
        blocks[0], blocks[1] = blocks[1], blocks[0]
        self.assertIsNone(parse_legacy_prompt("\n\n".join(blocks)))

    def test_flat_prompt_fields_do_not_silently_convert(self):
        fields = prompt_fields("A legacy prompt\nwith exact spacing.  ")
        self.assertEqual(fields["prompt_format"], "legacy_flat")
        self.assertEqual(fields["assembled_prompt"], "A legacy prompt\nwith exact spacing.  ")
        self.assertTrue(all(not item["text"] for item in fields["prompt_sections"].values()))

    def test_copy_and_edit_retain_provenance(self):
        source = edit_sections(empty_prompt_sections(), {"summary": "Source summary"})
        target = empty_prompt_sections()
        copied = copy_sections(
            target,
            source,
            source_card_id="card-1",
            names=["summary"],
            previous=True,
        )
        provenance = copied["summary"]["provenance"]
        self.assertEqual(provenance["source_type"], "copied_previous")
        self.assertEqual(provenance["source_card_id"], "card-1")
        self.assertFalse(provenance["modified_after_copy"])
        edited = edit_sections(copied, {"summary": "Edited summary"})
        self.assertTrue(edited["summary"]["provenance"]["modified_after_copy"])
        self.assertEqual(edited["summary"]["provenance"]["source_card_id"], "card-1")

    def test_default_inheritance_copies_only_stable_sections(self):
        source = empty_prompt_sections()
        source = edit_sections(source, {name: name for name in PROMPT_SECTION_NAMES})
        inherited = inherited_prompt_sections(source, "card-1")
        for name in ("subject_definitions", "retention_analysis", "overall_soundscape", "non_diegetic_music"):
            self.assertEqual(inherited[name]["text"], name)
            self.assertEqual(inherited[name]["provenance"]["source_type"], "copied_previous")
        self.assertEqual(inherited["summary"]["text"], "")
        self.assertEqual(inherited["detailed_description"]["text"], "")

    def test_validation_rejects_invalid_provenance(self):
        sections = empty_prompt_sections()
        sections["summary"] = section_record("x")
        sections["summary"]["provenance"]["source_type"] = "mystery"
        with self.assertRaises(ValueError):
            validate_prompt_sections(sections)


if __name__ == "__main__":
    unittest.main()
