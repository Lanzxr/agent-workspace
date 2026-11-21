from __future__ import annotations

import unittest

from app.reply_contract import (
    extract_final_reply,
    final_reply_markers,
    has_malformed_model_markup,
    strip_thinking_blocks,
)


class FinalReplyContractTests(unittest.TestCase):
    def test_extracts_only_the_execution_bound_reply_block(self) -> None:
        begin, end = final_reply_markers("execution-1")
        artifact = f"internal notes\n{begin}\nAnswer for the user.\n{end}\nmore notes"

        self.assertEqual(extract_final_reply(artifact, "execution-1"), "Answer for the user.")
        self.assertIsNone(extract_final_reply(artifact, "another-execution"))

    def test_rejects_duplicate_or_overlong_reply_blocks(self) -> None:
        begin, end = final_reply_markers("execution-1")
        duplicate = f"{begin}\nfirst\n{end}\n{begin}\nsecond\n{end}"
        overlong = f"{begin}\n{'x' * 1501}\n{end}"

        self.assertIsNone(extract_final_reply(duplicate, "execution-1"))
        self.assertIsNone(extract_final_reply(overlong, "execution-1"))

    def test_detects_and_strips_dsml_and_unclosed_thinking_leaks(self) -> None:
        leaked = "Let me rewrite build script notes part.</｜｜DSML｜｜ parameter>\n"
        self.assertTrue(has_malformed_model_markup(leaked))
        self.assertEqual(strip_thinking_blocks(leaked), "Let me rewrite build script notes part.")

        block_leak = "Done.<｜DSML｜function_calls><｜DSML｜invoke name=\"bash\"></｜DSML｜function_calls>"
        self.assertTrue(has_malformed_model_markup(block_leak))
        self.assertEqual(strip_thinking_blocks(block_leak), "Done.")

        self.assertTrue(has_malformed_model_markup("Summary\n<thinking>unclosed chain"))
        self.assertFalse(has_malformed_model_markup("<thinking>internal</thinking>\nClean final text."))
        self.assertFalse(has_malformed_model_markup("Normal markdown `a | b` without leaks."))


if __name__ == "__main__":
    unittest.main()
