#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现「一书多档」：同书名、不同 asset id 或旧文件名再次导出时，目录里应只剩一份。"""

from __future__ import annotations

import os
import tempfile
import unittest

from apple_books_highlights import (
    Annotation,
    Book,
    dedupe_asset_ids_by_title,
    export_book,
)


def _book(title: str, asset_id: str) -> Book:
    return Book(asset_id=asset_id, title=title, author="芒格")


def _ann(text: str = "划线") -> list:
    return [Annotation(text=text, cfi="epubcfi(/6/2)")]


class ExportDedupTests(unittest.TestCase):
    def test_reexport_same_title_different_asset_id_keeps_one_file(self):
        """同一书名换了 asset id 再导出，不得留下多份（穷查理宝典那种重复）。"""
        with tempfile.TemporaryDirectory() as out:
            first = export_book(
                _book("穷查理宝典：查理·芒格智慧箴言录", "ASSET-AAA"),
                _ann("旧划线"),
                out,
                to_stdout=False,
                lang="zh",
                rank=20,
            )
            second = export_book(
                _book("穷查理宝典：查理·芒格智慧箴言录", "ASSET-BBB"),
                _ann("新划线"),
                out,
                to_stdout=False,
                lang="zh",
                rank=20,
            )
            names = sorted(os.listdir(out))
            self.assertEqual(names, [os.path.basename(second.path)])
            self.assertFalse(os.path.exists(first.path) and first.path != second.path)
            with open(second.path, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("新划线", body)
            self.assertNotIn("旧划线", body)

    def test_reexport_purges_old_unnumbered_and_ranked_aliases(self):
        """旧版无序号、或序号已变的同名文件都应清掉。"""
        title = "穷查理宝典：查理·芒格智慧箴言录"
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, f"{title}.md"), "w") as f:
                f.write("legacy")
            with open(os.path.join(out, f"01 {title} (OLDID).md"), "w") as f:
                f.write("old rank")
            result = export_book(
                _book(title, "NEW-ID"),
                _ann(),
                out,
                to_stdout=False,
                lang="zh",
                rank=20,
            )
            self.assertEqual(os.listdir(out), [os.path.basename(result.path)])

    def test_export_does_not_delete_other_book_titles(self):
        with tempfile.TemporaryDirectory() as out:
            other = "21 明亮的夜晚.md"
            with open(os.path.join(out, other), "w") as f:
                f.write("keep")
            export_book(
                _book("穷查理宝典：查理·芒格智慧箴言录", "X"),
                _ann(),
                out,
                to_stdout=False,
                lang="zh",
                rank=20,
            )
            self.assertIn(other, os.listdir(out))
            self.assertEqual(len(os.listdir(out)), 2)


class DedupeByTitleTests(unittest.TestCase):
    def test_same_title_keeps_first_and_merges_annotations(self):
        """--all 遇到一书多 asset 时，保留最近在读的那本，并合并划线。"""
        a = Book(asset_id="A", title="悉达多", author="黑塞")
        b = Book(asset_id="B", title="悉达多", author="黑塞")
        library = {"A": a, "B": b}
        annots = {
            "A": [Annotation(text="甲", cfi="epubcfi(/6/4)")],
            "B": [Annotation(text="乙", cfi="epubcfi(/6/2)")],
        }
        kept = dedupe_asset_ids_by_title(["A", "B"], library, annots)
        self.assertEqual(kept, ["A"])
        texts = [x.text for x in annots["A"]]
        self.assertEqual(texts, ["乙", "甲"])


if __name__ == "__main__":
    unittest.main()
