from __future__ import annotations

import unittest

from app.reply_contract import extract_final_reply, final_reply_markers


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


if __name__ == "__main__":
    unittest.main()
