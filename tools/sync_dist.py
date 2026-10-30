#!/usr/bin/env python3
"""把 skill 从插件目录同步到标准 skills/ 布局，并保证共享脚本只有一份实现。

插件的单一源在 plugins/easyapi/skills/<name>/（插件规范要求 skills/<name>/SKILL.md
放在插件目录内）；仓库根的 skills/<name>/ 是给通用安装器（npx skills add / gh skill
install）用的生成副本。

两个技能共用同一份运行时代码：
  - easyapi-studio/scripts/easyapi.py 是源
  - easyapi-auth/scripts/easyapi.py 是它的字节副本（由本脚本复制，--check 做哈希校验）
这样「配置 key」和「创作」是两份文档，但只有一份实现，改一处不会漂移。

用法：
  python tools/sync_dist.py           # 同步共享脚本 + 重新生成 skills/
  python tools/sync_dist.py --check    # 只比对，不一致时退出码 1（CI / 测试用）
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_SKILLS = os.path.join(REPO_ROOT, "plugins", "easyapi", "skills")
DIST_ROOT = os.path.join(REPO_ROOT, "skills")

# 源技能排在最前：它的 scripts/easyapi.py 是共享实现
SKILLS = ("easyapi-studio", "easyapi-auth")
PRIMARY = "easyapi-studio"
SHARED_SCRIPT = os.path.join("scripts", "easyapi.py")
LEGACY_DIST = "easyapi"

# 运行产物与缓存不进分发副本
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo")


def source_dir(name):
    return os.path.join(PLUGIN_SKILLS, name)


def target_dir(name):
    return os.path.join(DIST_ROOT, name)


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


def shared_script_paths():
    return (os.path.join(source_dir(PRIMARY), SHARED_SCRIPT),
            os.path.join(source_dir("easyapi-auth"), SHARED_SCRIPT))


def sync_shared_script():
    """把 studio 的脚本复制到 auth；返回 True 表示发生了写入。"""
    primary, secondary = shared_script_paths()
    if not os.path.isfile(primary):
        raise SystemExit("共享脚本不存在：%s" % primary)
    if os.path.isfile(secondary) and digest(primary) == digest(secondary):
        return False
    os.makedirs(os.path.dirname(secondary), exist_ok=True)
    shutil.copy2(primary, secondary)
    return True


def sync():
    wrote_shared = sync_shared_script()
    os.makedirs(DIST_ROOT, exist_ok=True)
    legacy = os.path.join(DIST_ROOT, LEGACY_DIST)
    if os.path.isdir(legacy):
        # 技能已改名为 easyapi-studio，旧目录必须清掉，否则安装器还能装到旧名字
        shutil.rmtree(legacy)
    counts = {}
    for name in SKILLS:
        src = source_dir(name)
        if not os.path.isdir(src):
            raise SystemExit("源目录不存在：%s" % src)
        dst = target_dir(name)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(
            src, dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        counts[name] = len(snapshot(src))
    return counts, wrote_shared


def compare_shared_script():
    primary, secondary = shared_script_paths()
    if not os.path.isfile(secondary):
        return "缺失"
    if digest(primary) != digest(secondary):
        return "内容不同"
    return None


def compare_skill(name):
    src = snapshot(source_dir(name))
    dst = snapshot(target_dir(name))
    return {
        "missing": sorted(set(src) - set(dst)),
        "extra": sorted(set(dst) - set(src)),
        "changed": sorted(n for n in set(src) & set(dst) if src[n] != dst[n]),
        "count": len(src),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="同步 skill 到标准 skills/ 布局")
    parser.add_argument("--check", action="store_true",
                        help="只比对不写入，不一致时退出码 1")
    args = parser.parse_args(argv)

    if not args.check:
        counts, wrote_shared = sync()
        if wrote_shared:
            print("已同步共享脚本 -> easyapi-auth/scripts/easyapi.py")
        for name, count in counts.items():
            print("已生成 skills/%s（%d 个文件）" % (name, count))
        return 0

    problems = []
    shared_state = compare_shared_script()
    if shared_state:
        problems.append("easyapi-auth 的共享脚本与 studio %s" % shared_state)

    for name in SKILLS:
        diff = compare_skill(name)
        if diff["missing"] or diff["extra"] or diff["changed"]:
            for label, key in (("缺失", "missing"), ("多余", "extra"), ("内容不同", "changed")):
                for item in diff[key]:
                    problems.append("%s %s：%s" % (name, label, item))

    if not problems:
        total = sum(compare_skill(name)["count"] for name in SKILLS)
        print("skills/ 与插件目录一致（%d 个文件，%d 个技能）" % (total, len(SKILLS)))
        return 0

    print("检测到分发副本与源不一致，请运行 `python tools/sync_dist.py` 重新生成：")
    for item in problems:
        print("  %s" % item)
    return 1


if __name__ == "__main__":
    sys.exit(main())
