#!/usr/bin/env python3
"""把 skill 从插件目录同步到标准 skills/ 布局。

单一源固定在 plugins/easyapi/skills/easyapi/（插件规范要求 skills/<name>/SKILL.md
放在插件目录内）。但通用安装器（npx skills add / gh skill install）按仓库根的
skills/<name>/ 找 skill，所以这里生成一份副本，并用 --check 阻止两份内容漂移。

用法：
  python tools/sync_dist.py          # 重新生成 skills/easyapi/
  python tools/sync_dist.py --check   # 只比对，不一致时退出码 1（CI / 测试用）
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO_ROOT, "plugins", "easyapi", "skills", "easyapi")
TARGET = os.path.join(REPO_ROOT, "skills", "easyapi")

# 运行产物与缓存不进分发副本
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo")


def iter_files(root):
    """按相对路径遍历要分发的文件。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            yield os.path.relpath(full, root).replace(os.sep, "/"), full


def digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def snapshot(root):
    if not os.path.isdir(root):
        return {}
    return {rel: digest(full) for rel, full in iter_files(root)}


def compare():
    src = snapshot(SOURCE)
    dst = snapshot(TARGET)
    missing = sorted(set(src) - set(dst))
    extra = sorted(set(dst) - set(src))
    changed = sorted(name for name in set(src) & set(dst) if src[name] != dst[name])
    return src, missing, extra, changed


def sync():
    if not os.path.isdir(SOURCE):
        raise SystemExit("源目录不存在：%s" % SOURCE)
    if os.path.isdir(TARGET):
        shutil.rmtree(TARGET)
    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    shutil.copytree(
        SOURCE, TARGET,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    return snapshot(SOURCE)


def main(argv=None):
    parser = argparse.ArgumentParser(description="同步 skill 到标准 skills/ 布局")
    parser.add_argument("--check", action="store_true",
                        help="只比对不写入，不一致时退出码 1")
    args = parser.parse_args(argv)

    if not args.check:
        synced = sync()
        print("已生成 %s（%d 个文件）" % (TARGET, len(synced)))
        return 0

    _src, missing, extra, changed = compare()
    if not (missing or extra or changed):
        print("skills/easyapi 与插件目录一致（%d 个文件）" % len(snapshot(SOURCE)))
        return 0

    print("检测到分发副本与源不一致，请运行 `python tools/sync_dist.py` 重新生成：")
    for label, names in (("缺失", missing), ("多余", extra), ("内容不同", changed)):
        for name in names:
            print("  %s：%s" % (label, name))
    return 1


if __name__ == "__main__":
    sys.exit(main())
