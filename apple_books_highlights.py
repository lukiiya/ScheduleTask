#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 macOS「图书」(Apple Books) 里的下划线 / 高亮 / 批注导出成 Markdown。

为什么需要它
-----------
你在 macOS「图书」App 里画的下划线和高亮**不会写进 `.epub` 文件**,
而是存在 Apple Books 自己的 SQLite 数据库里:

    ~/Library/Containers/com.apple.iBooksX/Data/Documents/
        AEAnnotation/AEAnnotation_*.sqlite   ← 标注内容(下划线/高亮/批注)
        BKLibrary/BKLibrary-*.sqlite         ← 书名 / 作者对照表

所以想导出划线,只能直接读这两个库,而不是解析 EPUB。

本工具把两个库(连同 `-wal` / `-shm` 预写日志)复制到临时目录后**只读**查询,
既能拿到「图书」尚未落盘、还在 WAL 里的最新标注,也不会锁住正在运行的 App。

输出是对 Obsidian 友好的 Markdown(callout 引用块),按章节分组、按阅读顺序排列。

用法
----
    # 列出所有有标注的书(书名 + 条数)
    apple-books-highlights --list

    # 导出某本书(按书名片段模糊匹配,不区分大小写)
    apple-books-highlights --book "活着"

    # 先预览不写文件(打印到屏幕)
    apple-books-highlights --book "活着" --stdout

    # 导出所有有标注的书,每本一篇
    apple-books-highlights --all

    # 指定输出目录和语言(en | zh)
    apple-books-highlights --all --out ~/notes --lang zh

依赖:macOS + Python 3.8+(`sqlite3` 为标准库自带)。无需 pip 安装任何依赖。

MIT License. https://github.com/VonHai/apple-books-highlights
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

HOME = os.path.expanduser("~")
BOOKS_ROOT = os.path.join(HOME, "Library/Containers/com.apple.iBooksX/Data/Documents")
ANNOT_GLOB = os.path.join(BOOKS_ROOT, "AEAnnotation", "AEAnnotation*.sqlite")
LIB_GLOB = os.path.join(BOOKS_ROOT, "BKLibrary", "BKLibrary*.sqlite")

# Apple Books 的时间戳用 Core Data / "Cocoa" 秒数,基准是 2001-01-01 UTC,
# 而不是 Unix 的 1970 基准。
COCOA_EPOCH = 978307200

# 存章节/小节标题的那一列是未文档化的,列名在不同 macOS 版本会变。
# 我们按顺序探测下列候选,取本机真实存在的第一个(见 `_pick_chapter_column`)。
CHAPTER_COLUMN_CANDIDATES = ("ZFUTUREPROOFING5", "ZPLLOCATIONRANGESTART")
# 「最近在读」用书库最后打开时间,不是标注时间。列名偶有版本差异,按序探测。
LAST_OPEN_COLUMN_CANDIDATES = ("ZLASTOPENDATE", "ZLASTENGAGEDDATE")
_DT_MIN_UTC = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


class BooksExportError(Exception):
    """所有面向用户的失败(找不到库、无匹配等)都抛这个异常。

    不在底层 helper 里直接 `sys.exit`,而是抛异常——这样核心逻辑可导入、可测试;
    只在 CLI 边界(`main`)把它转成退出码 1。
    """


@dataclass
class Book:
    asset_id: str
    title: str
    author: str = ""
    last_open: Optional[dt.datetime] = None


@dataclass
class Annotation:
    text: str
    note: str = ""
    is_underline: bool = False
    cfi: str = ""
    chapter: str = ""


@dataclass
class ExportResult:
    book: Book
    count: int
    path: Optional[str] = None  # 打印到 stdout 时为 None


# --------------------------------------------------------------------------- #
# 数据库读取
# --------------------------------------------------------------------------- #
def _find_one(pattern: str, what: str) -> str:
    """返回匹配 *pattern* 的、最近修改的那个文件。"""
    hits = sorted(glob.glob(pattern))
    if not hits:
        raise BooksExportError(
            f"Could not find {what}: {pattern}\n"
            "Make sure macOS Books is installed and has been opened at least once."
        )
    return max(hits, key=os.path.getmtime)


def _copy_db(src: str, tmpdir: str) -> str:
    """把 .sqlite 连同 -wal / -shm 兄弟文件一起复制过来,以读到最新数据。"""
    for suffix in ("", "-wal", "-shm"):
        p = src + suffix
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(tmpdir, os.path.basename(p)))
    return os.path.join(tmpdir, os.path.basename(src))


def _connect(src: str, tmpdir: str) -> sqlite3.Connection:
    con = sqlite3.connect(_copy_db(src, tmpdir))
    con.row_factory = sqlite3.Row
    return con


def _table_columns(con: sqlite3.Connection, table: str) -> set:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}


def _pick_chapter_column(columns: set) -> Optional[str]:
    """章节标题所在的列因版本而异,挑一个本机真实存在的候选列。"""
    for candidate in CHAPTER_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def _pick_last_open_column(columns: set) -> Optional[str]:
    for candidate in LAST_OPEN_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def _cocoa_to_datetime(value) -> Optional[dt.datetime]:
    """把 Apple / Core Data 秒数换成 UTC datetime;无效值返回 None。"""
    if value is None:
        return None
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(secs + COCOA_EPOCH, tz=dt.timezone.utc)


def _format_last_open(when: Optional[dt.datetime]) -> str:
    if when is None:
        return "-"
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


def sort_asset_ids_by_recently_read(
    asset_ids: Sequence[str], library: Dict[str, Book]
) -> List[str]:
    """最近打开的书在前;没有打开时间的排到最后。"""
    return sorted(
        asset_ids,
        key=lambda aid: library[aid].last_open or _DT_MIN_UTC,
        reverse=True,
    )


def load_library(tmpdir: str) -> Dict[str, Book]:
    """返回书库里每本书的 ``{asset_id: Book}``。"""
    con = _connect(_find_one(LIB_GLOB, "the BKLibrary database"), tmpdir)
    try:
        books: Dict[str, Book] = {}
        last_open_col = _pick_last_open_column(_table_columns(con, "ZBKLIBRARYASSET"))
        last_open_select = f"{last_open_col} AS last_open" if last_open_col else "NULL AS last_open"
        for r in con.execute(
            f"SELECT ZASSETID, ZTITLE, ZAUTHOR, {last_open_select} "
            "FROM ZBKLIBRARYASSET WHERE ZASSETID IS NOT NULL"
        ):
            books[r["ZASSETID"]] = Book(
                asset_id=r["ZASSETID"],
                title=(r["ZTITLE"] or "Untitled"),
                author=(r["ZAUTHOR"] or ""),
                last_open=_cocoa_to_datetime(r["last_open"]),
            )
        return books
    finally:
        con.close()


def _cfi_sort_key(cfi: str):
    """把 ``epubcfi`` 位置串解析成数值元组,使标注按阅读顺序排列,
    而不是按字符串字典序(那会把 /6/14 排到 /6/2 前面)。"""
    if not cfi:
        return (10 ** 9,)
    return tuple(int(n) for n in re.findall(r"\d+", cfi)) or (10 ** 9,)


def load_annotations(asset_ids: Sequence[str], tmpdir: str) -> Dict[str, List[Annotation]]:
    """返回给定这些书的 ``{asset_id: [Annotation, ...]}``,每本按阅读顺序排好。
    没有任何标注的书不会出现在结果里。"""
    if not asset_ids:
        return {}
    con = _connect(_find_one(ANNOT_GLOB, "the AEAnnotation database"), tmpdir)
    try:
        chapter_col = _pick_chapter_column(_table_columns(con, "ZAEANNOTATION"))
        chapter_select = f"{chapter_col} AS chapter" if chapter_col else "'' AS chapter"
        placeholders = ",".join("?" * len(asset_ids))
        rows = con.execute(
            f"""
            SELECT ZANNOTATIONASSETID       AS asset,
                   ZANNOTATIONSELECTEDTEXT  AS text,
                   ZANNOTATIONNOTE          AS note,
                   ZANNOTATIONISUNDERLINE   AS is_underline,
                   ZANNOTATIONLOCATION      AS cfi,
                   {chapter_select}
            FROM ZAEANNOTATION
            WHERE ZANNOTATIONASSETID IN ({placeholders})
              AND ZANNOTATIONDELETED = 0
              AND ZANNOTATIONSELECTEDTEXT IS NOT NULL
              AND TRIM(ZANNOTATIONSELECTEDTEXT) <> ''
            """,
            list(asset_ids),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise BooksExportError(
            "Failed to read the annotation database; the Apple Books schema on "
            f"this macOS version may be unsupported.\n  sqlite3: {exc}"
        ) from exc
    finally:
        con.close()

    by_book: Dict[str, List[Annotation]] = {}
    for r in rows:
        by_book.setdefault(r["asset"], []).append(
            Annotation(
                text=r["text"],
                note=(r["note"] or ""),
                is_underline=bool(r["is_underline"]),
                cfi=(r["cfi"] or ""),
                chapter=(r["chapter"] or "").strip(),
            )
        )
    for anns in by_book.values():
        anns.sort(key=lambda a: _cfi_sort_key(a.cfi))
    return by_book


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
LABELS = {
    "en": {
        "source": "Apple Books (macOS)",
        "tags": "[reading-notes, apple-books, highlights]",
        "heading_suffix": "Highlights",
        "info": "Export info",
        "author": "Author",
        "source_line": "Source: macOS Books (Apple Books) annotation database",
        "exported": "Exported",
        "summary": "{n} annotations — {u} underlines / {h} highlights / {c} with notes",
        "underline": "Underline",
        "highlight": "Highlight",
        "note": "Note",
    },
    "zh": {
        "source": "Apple Books (macOS 图书)",
        "tags": "[读书笔记, Apple图书, 划线摘录]",
        "heading_suffix": "划线摘录",
        "info": "导出信息",
        "author": "作者",
        "source_line": "来源:macOS「图书」(Apple Books) 标注数据库",
        "exported": "导出时间",
        "summary": "共 {n} 条 — 下划线 {u} / 高亮 {h} / 含批注 {c}",
        "underline": "下划线",
        "highlight": "高亮",
        "note": "批注",
    },
}


def render_markdown(book: Book, annotations: List[Annotation], lang: str = "en") -> str:
    """把一本书的标注渲染成对 Obsidian 友好的 Markdown。"""
    t = LABELS.get(lang, LABELS["en"])
    n_under = sum(1 for a in annotations if a.is_underline)
    n_high = len(annotations) - n_under
    n_note = sum(1 for a in annotations if a.note.strip())
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    out: List[str] = ["---", f"title: {book.title}"]
    if book.author:
        out.append(f"author: {book.author}")
    out += [
        f"source: {t['source']}",
        f"exported: {today}",
        f"highlights: {len(annotations)}",
        f"tags: {t['tags']}",
        "---",
        "",
        f"# {book.title} — {t['heading_suffix']}",
        "",
        f"> [!info] {t['info']}",
    ]
    if book.author:
        out.append(f"> {t['author']}: {book.author}")
    out += [
        f"> {t['source_line']}",
        f"> {t['exported']}: {now}",
        "> " + t["summary"].format(n=len(annotations), u=n_under, h=n_high, c=n_note),
        "",
        "---",
        "",
    ]

    last_chapter = None
    for i, a in enumerate(annotations, 1):
        if a.chapter and a.chapter != last_chapter:
            out += [f"## {a.chapter}", ""]
            last_chapter = a.chapter

        # 把多行划线里的空行压成段内换行,让引用块保持成一个整洁的 callout。
        text = re.sub(r"\n{2,}", "\n> \n> ", a.text.strip()).replace("\n", "\n> ")
        kind = t["underline"] if a.is_underline else t["highlight"]
        out += [f"> [!quote] {i}. {kind}", f"> {text}"]
        if a.note.strip():
            out += ["> ", f"> \U0001f4ac **{t['note']}**: {a.note.strip().replace(chr(10), chr(10) + '> ')}"]
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# 写出
# --------------------------------------------------------------------------- #
def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name[:120] or "Untitled"


def _unique_path(out_dir: str, book: Book, rank: int) -> str:
    """一书一档: ``序号 书名.md``。序号小的在按文件名排序时置顶。"""
    title = _safe_filename(book.title)
    return os.path.join(out_dir, f"{rank:02d} {title}.md")


def _is_export_for_book(name: str, book: Book) -> bool:
    """识别同一书名的导出档(含旧版「序号 书名 (asset_id).md」和无序号文件名)。"""
    title = _safe_filename(book.title)
    return re.match(
        rf"^(?:\d{{2}} )?{re.escape(title)}(?: \([^)]+\))?\.md$", name
    ) is not None


def _purge_stale_exports(out_dir: str, book: Book, keep_path: str) -> None:
    """删掉同一书名的旧文件(asset 变了、无序号、或序号已变),保证目录里只有一份。"""
    keep = os.path.abspath(keep_path)
    try:
        names = os.listdir(out_dir)
    except FileNotFoundError:
        return
    for name in names:
        if not _is_export_for_book(name, book):
            continue
        path = os.path.abspath(os.path.join(out_dir, name))
        if path != keep:
            os.remove(path)


def _merge_annotations(*groups: Sequence[Annotation]) -> List[Annotation]:
    """合并多份标注,按 (正文, 批注, 位置) 去重,再按阅读顺序排好。"""
    seen = set()
    out: List[Annotation] = []
    for anns in groups:
        for a in anns:
            key = (a.text, a.note, a.cfi)
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    out.sort(key=lambda a: _cfi_sort_key(a.cfi))
    return out


def dedupe_asset_ids_by_title(
    asset_ids: Sequence[str],
    library: Dict[str, Book],
    annotations: Dict[str, List[Annotation]],
) -> List[str]:
    """一书多名(多个 asset)时只留一份。调用方应按「最近在读」排好,先出现的优先;
    其余副本的划线合并进保留的那本。"""
    kept: List[str] = []
    title_to_aid: Dict[str, str] = {}
    for aid in asset_ids:
        key = library[aid].title.strip()
        if key not in title_to_aid:
            title_to_aid[key] = aid
            kept.append(aid)
            continue
        primary = title_to_aid[key]
        annotations[primary] = _merge_annotations(
            annotations.get(primary, []),
            annotations.get(aid, []),
        )
    return kept


def export_book(
    book: Book,
    annotations: List[Annotation],
    out_dir: str,
    to_stdout: bool,
    lang: str,
    rank: int = 1,
) -> ExportResult:
    md = render_markdown(book, annotations, lang)
    if to_stdout:
        print(md)
        return ExportResult(book=book, count=len(annotations), path=None)
    os.makedirs(out_dir, exist_ok=True)
    path = _unique_path(out_dir, book, rank)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    _purge_stale_exports(out_dir, book, path)
    if book.last_open is not None:
        ts = book.last_open.timestamp()
        os.utime(path, (ts, ts))
    return ExportResult(book=book, count=len(annotations), path=path)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def _resolve_targets(args, library: Dict[str, Book], tmpdir: str) -> List[str]:
    """把命令行选择器(--book / --asset / --all)解析成一批「确实有标注」的
    asset id;只加载需要的部分,不做无谓的全库扫描。"""
    if args.asset:
        candidates = [args.asset] if args.asset in library else []
    elif args.book:
        kw = args.book.lower()
        candidates = [aid for aid, b in library.items() if kw in b.title.lower()]
    else:  # --all
        candidates = list(library.keys())

    annotated = set(load_annotations(candidates, tmpdir))

    if args.asset and args.asset not in annotated:
        raise BooksExportError(f"asset id {args.asset} has no annotations or does not exist.")
    if args.book:
        matches = [aid for aid in candidates if aid in annotated]
        if not matches:
            raise BooksExportError(
                f'No annotated book matches "{args.book}". Try --list to see what is available.'
            )
        if len(matches) > 1:
            names = "\n".join("  - " + library[aid].title for aid in matches)
            raise BooksExportError(
                f'"{args.book}" matched {len(matches)} books, please be more specific:\n{names}'
            )
        return matches
    return [aid for aid in candidates if aid in annotated]


def cmd_list(library: Dict[str, Book], tmpdir: str) -> None:
    annots = load_annotations(list(library.keys()), tmpdir)
    aids = sort_asset_ids_by_recently_read(annots.keys(), library)
    if not aids:
        print("No underlines / highlights found in Apple Books yet.")
        return
    print(f"{len(aids)} book(s) with annotations:\n")
    print(f"{'opened':<16}  {'count':>5}  title")
    print("-" * 60)
    for aid in aids:
        book = library[aid]
        author = f"  — {book.author}" if book.author else ""
        print(
            f"{_format_last_open(book.last_open):<16}  {len(annots[aid]):>5}  "
            f"{book.title}{author}"
        )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="apple-books-highlights",
        description="Export macOS Apple Books underlines / highlights / notes to Markdown.",
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list every annotated book")
    g.add_argument("--book", metavar="TITLE", help="fuzzy-match one book by title and export it")
    g.add_argument("--asset", metavar="ID", help="export one book by exact asset id")
    g.add_argument("--all", action="store_true", help="export every annotated book")
    ap.add_argument(
        "--out",
        default=os.path.join(os.getcwd(), "AppleBooksHighlights"),
        help="output directory (default: ./AppleBooksHighlights)",
    )
    ap.add_argument("--lang", choices=("en", "zh"), default="en", help="output language (default: en)")
    ap.add_argument("--stdout", action="store_true", help="print to screen instead of writing a file")
    return ap


def run(args) -> int:
    if sys.platform != "darwin":
        raise BooksExportError("This tool only runs on macOS (it reads Apple Books' local databases).")

    with tempfile.TemporaryDirectory(prefix="abh_") as tmpdir:
        library = load_library(tmpdir)

        if args.list:
            cmd_list(library, tmpdir)
            return 0

        targets = sort_asset_ids_by_recently_read(
            _resolve_targets(args, library, tmpdir), library
        )
        if not targets:
            print("Nothing to export.")
            return 0

        annots = load_annotations(targets, tmpdir)
        targets = dedupe_asset_ids_by_title(targets, library, annots)
        written = 0
        for rank, aid in enumerate(targets, 1):
            result = export_book(
                library[aid], annots[aid], args.out, args.stdout, args.lang, rank=rank
            )
            if result.path:
                written += 1
                print(f"✓ {result.book.title}: {result.count} → {result.path}")
        if written and not args.stdout:
            print(f"\nDone. Exported {written} book(s).")
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except BooksExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
