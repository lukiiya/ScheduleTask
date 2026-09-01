# ScheduleTask

存放定期会执行的个人脚本。目前收录一份工具：把 macOS「图书」（Apple Books）里的下划线、高亮与批注导出为 Markdown，并写入指定的 Obsidian 目录。

后续若再加入定时任务，可按同样约定放在本目录，并在下方「脚本一览」补一行说明。

## 环境

- 系统：仅 macOS（脚本读取本机 Apple Books 的 SQLite 数据库）
- 运行时：Python 3.8+
- 依赖：标准库即可（`sqlite3`、`argparse` 等），无需 `pip install`

请先至少打开过一次「图书」App，确保本机已生成书库与标注数据库。

## 脚本一览

| 脚本 | 用途 |
| --- | --- |
| [`apple_books_highlights.py`](apple_books_highlights.py) | 读取 Apple Books 标注库，按书导出对 Obsidian 友好的 Markdown |

---

## apple_books_highlights.py

### 它解决什么问题

在 macOS「图书」里画的下划线、高亮和批注**不会写进 `.epub` 文件**，而是存在 Apple Books 自己的 SQLite 里：

```
~/Library/Containers/com.apple.iBooksX/Data/Documents/
    AEAnnotation/AEAnnotation_*.sqlite   ← 标注（下划线 / 高亮 / 批注）
    BKLibrary/BKLibrary-*.sqlite         ← 书名、作者、最近打开时间
```

因此导出划线只能直接读这两个库，而不是解析 EPUB。

脚本会把 `.sqlite` 连同 `-wal` / `-shm` 预写日志复制到临时目录后**只读**查询：既能读到「图书」尚未落盘、仍在 WAL 里的最新标注，也不会锁住正在运行的 App。

### 常用命令

在本目录下执行：

```bash
# 列出所有有标注的书（最近打开时间、条数、书名、作者）
python3 apple_books_highlights.py --list

# 按书名片段模糊匹配导出一本（不区分大小写）
python3 apple_books_highlights.py --book "活着" --lang zh --out ~/path/to/Obsidian/vault

# 先预览，不写文件
python3 apple_books_highlights.py --book "活着" --stdout --lang zh

# 按精确 asset id 导出一本
python3 apple_books_highlights.py --asset "<ASSET_ID>" --lang zh --out ~/path/to/Obsidian/vault

# 导出所有有标注的书，每本一篇
python3 apple_books_highlights.py --all --lang zh --out ~/path/to/Obsidian/vault
```

同步到 Obsidian 时，把 `--out` 换成库中实际存放读书笔记的文件夹。未指定时，默认写到当前工作目录下的 `AppleBooksHighlights/`。

### 参数

`--list`、`--book`、`--asset`、`--all` 四选一，必须指定其一。

| 参数 | 说明 |
| --- | --- |
| `--list` | 列出所有已有标注的书，不写文件 |
| `--book TITLE` | 按书名模糊匹配一本并导出；匹配到 0 本或超过 1 本会报错 |
| `--asset ID` | 按图书 asset id 精确导出一本 |
| `--all` | 导出库中每一本有标注的书 |
| `--out DIR` | 输出目录，默认 `./AppleBooksHighlights` |
| `--lang {en,zh}` | 正文标签语言，默认 `en`；写入 Obsidian 时建议 `zh` |
| `--stdout` | 打印到终端，不写文件 |

### 输出约定

- 一书一档，文件名形如：`序号 书名.md`  
  例如 `01 活着.md`。序号按「最近打开」排序，小序号在按文件名排序时置顶。
- 再次导出时按**书名**覆盖：同一本书换了 Apple Books 内部 id、旧档带 `(asset_id)`、或序号已变，都会删掉旧文件，目录里只留一份。
- `--all` 若遇到同一书名的多个副本（不同 asset），只导出最近在读的那本，并把各副本的划线合并进去。
- 导出文件的修改时间会尽量对齐该书在「图书」里的最后打开时间，便于在访达 / Obsidian 里按时间浏览。
- Markdown 带 YAML front matter，并用 Obsidian callout（`[!info]`、`[!quote]`）呈现导出信息与每条划线。
- 标注按 EPUB CFI 解析后的阅读顺序排列，并按章节分组。

中文导出时，front matter 大致如下：

```yaml
---
title: 书名
author: 作者
source: Apple Books (macOS 图书)
exported: 2026-09-01
highlights: 12
tags: [读书笔记, Apple图书, 划线摘录]
---
```

正文会统计下划线、高亮、含批注的条数；每条标注标明类型，若有批注则附在该条下方。

### 行为细节

- 只导出未删除、且选中文本非空的标注。
- 章节列、最近打开时间列在不同 macOS / 图书版本上可能改名；脚本会按候选列探测本机真实存在的字段。
- 非 macOS 上运行会直接失败并提示仅支持本机「图书」数据库。
- 若本机图书 schema 与预期不符，会提示读取标注库失败。

### 许可与来源

MIT License。实现参考 [VonHai/apple-books-highlights](https://github.com/VonHai/apple-books-highlights)。
