import unittest

from longcaster.prompt_sections import (
    PROMPT_SECTION_NAMES,
    assemble_prompt,
    copy_sections,
    edit_sections,
    empty_prompt_sections,
    hash_prompt,
    inherited_prompt_sections,
    imported_prompt_sections,
    inject_lora_activation_words,
    parse_labeled_prompt,
    parse_legacy_prompt,
    prompt_fields,
    section_record,
    validate_prompt_sections,
)


class PromptSectionTests(unittest.TestCase):
    def test_project_lora_words_are_injected_without_changing_saved_prompt(self):
        prompt = assemble_prompt(empty_prompt_sections())
        injected = inject_lora_activation_words(prompt, "  ohwxPerson, filmStyle  ")
        self.assertTrue(injected.startswith("subject_definitions:\nohwxPerson, filmStyle\n"))
        self.assertEqual(inject_lora_activation_words(prompt, "   "), prompt)
        self.assertEqual(
            inject_lora_activation_words("legacy prompt", "triggerWord"),
            "triggerWord\n\nlegacy prompt",
        )

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
        positions = [assembled.index(f"{name}:") for name in PROMPT_SECTION_NAMES]
        self.assertEqual(positions, sorted(positions))
        parsed = parse_labeled_prompt(assembled)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["subject_definitions"]["text"], values["subject_definitions"])
        self.assertEqual(parsed["summary"]["text"], values["summary"])
        self.assertNotIn("<subject_definitions>", assembled)
        self.assertEqual(assemble_prompt(sections), assembled)
        self.assertEqual(hash_prompt(assembled), hash_prompt(assemble_prompt(sections)))

    def test_parser_rejects_flat_missing_duplicate_and_out_of_order_prompts(self):
        self.assertIsNone(parse_legacy_prompt("flat prompt"))
        sections = empty_prompt_sections()
        assembled = assemble_prompt(sections)
        self.assertIsNone(parse_labeled_prompt(assembled.replace("summary:", "summary:\nsummary:", 1)))
        blocks = [f"<{name}>\n\n</{name}>" for name in PROMPT_SECTION_NAMES]
        blocks[0], blocks[1] = blocks[1], blocks[0]
        self.assertIsNone(parse_legacy_prompt("\n\n".join(blocks)))

    def test_flat_prompt_fields_do_not_silently_convert(self):
        fields = prompt_fields("A legacy prompt\nwith exact spacing.  ")
        self.assertEqual(fields["prompt_format"], "legacy_flat")
        self.assertEqual(fields["assembled_prompt"], "A legacy prompt\nwith exact spacing.  ")
        self.assertTrue(all(not item["text"] for item in fields["prompt_sections"].values()))

    def test_labeled_flat_prompt_can_be_safely_split_on_explicit_conversion(self):
        prompt = "\n\n".join(
            f"{name}:\n{name} text\nline two" for name in PROMPT_SECTION_NAMES
        )
        parsed = parse_labeled_prompt(prompt)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["summary"]["text"], "summary text\nline two")
        self.assertEqual(parsed["detailed_description"]["text"], "detailed_description text\nline two")
        self.assertTrue(all(record["provenance"]["source_type"] == "generated_internal" for record in parsed.values()))

    def test_labeled_flat_prompt_accepts_readable_heading_separators(self):
        prompt = "\n\n".join(
            f"## {name.replace('_', ' ').title()}:\n{name} text"
            for name in PROMPT_SECTION_NAMES
        )
        parsed = parse_labeled_prompt(prompt)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["overall_soundscape"]["text"], "overall_soundscape text")

    def test_labeled_parser_rejects_missing_or_ambiguous_headings(self):
        self.assertIsNone(parse_labeled_prompt("summary:\nOnly one section"))
        prompt = "\n\n".join(f"{name}:\ntext" for name in PROMPT_SECTION_NAMES)
        self.assertIsNone(parse_labeled_prompt(f"{prompt}\n\nsummary:\nduplicate"))

    def test_pasted_partial_prompt_assigns_known_sections_and_preserves_preamble(self):
        prompt = "Shared setup\n\nSummary:\nA short summary.\n\nDetailed Description:\nThe shot."
        sections = imported_prompt_sections(prompt)
        self.assertEqual(sections["summary"]["text"], "A short summary.")
        self.assertEqual(sections["detailed_description"]["text"], "Shared setup\n\nThe shot.")
        self.assertEqual(sections["retention_analysis"]["text"], "")

    def test_unlabeled_pasted_prompt_falls_back_to_detailed_description(self):
        sections = imported_prompt_sections("One unlabelled external prompt.")
        self.assertEqual(
            sections["detailed_description"]["text"], "One unlabelled external prompt."
        )
        self.assertTrue(all(
            not record["text"] for name, record in sections.items()
            if name != "detailed_description"
        ))

    def test_pasted_prompt_accepts_inline_section_text(self):
        sections = imported_prompt_sections(
            "Summary: The swimmer rests.\n\nOverall Soundscape: Quiet pool ambience."
        )
        self.assertEqual(sections["summary"]["text"], "The swimmer rests.")
        self.assertEqual(sections["overall_soundscape"]["text"], "Quiet pool ambience.")

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
