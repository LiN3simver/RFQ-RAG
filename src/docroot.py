"""附件路徑鍵：資料庫只存相對 DOC_ROOT 的鍵，執行期才組回磁碟路徑。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parents[1] / "data"

_warned: set[str] = set()


def root() -> Path:
    return Path(os.environ.get("DOC_ROOT") or _DEFAULT).expanduser().resolve()


def to_key(path: str | Path) -> str:
    p = Path(path).expanduser()
    p = p.resolve()
    try:
        return p.relative_to(root()).as_posix()
    except ValueError:
        key = p.as_posix()
        if key not in _warned:
            _warned.add(key)
            print(f"[docroot] {p} 不在 DOC_ROOT（{root()}）底下，只能存絕對路徑；"
                  f"這一份換機器就打不開了。要可攜請把檔案放進 DOC_ROOT，"
                  f"或把 DOC_ROOT 設成那個目錄。", file=sys.stderr)
        return key


def resolve(key: str) -> Path:
    p = Path(key)
    return p if p.is_absolute() else root() / p


def exists(key: str) -> bool:
    return resolve(key).is_file()
