#!/usr/bin/env python3
"""生成 skill 内置参数快照。

两种来源：
  1. --from-api BASE ：GET {BASE}/pricing/schema?all=1（推荐，参数与端点都是线上真值，
     新模型上线后重跑一次即可，不用等 skill 发版）
  2. --docs DIR      ：读 easyapi-ai-api/docs 的管理端导出（离线兜底）

为什么要内置快照：线上 params 覆盖率长期不足（2026-10-23 复测 69 个计价模型只有 15 个
带 params），所以 skill 不能只靠线上 schema 做传参校验；快照同时承担完全离线场景。

剥字段的口径与后端 model.SanitizeParamsForClient 一致：
upstream_name / source / confidence 属于服务端路由信息，不下发给客户端。

用法：
    python tools/build_snapshot.py --from-api https://token.easyapi.com --out ../easyapi/references/model-snapshot.json
    python tools/build_snapshot.py --docs ../../easyapi-ai-api/docs --out ../easyapi/references/model-snapshot.json
"""

import argparse
import datetime
import json
import os
import sys
import urllib.request

# 服务端专有字段：客户端不需要（upstream_name 由网关做映射），与后端剥离口径一致
SERVER_ONLY_KEYS = ("upstream_name", "source", "confidence")

DEFAULT_SOURCES = ("image_models.json", "video_models.json", "all_models.json")

# OpenAPI 3.1 命名 -> 快照沿用的 model.params 命名
OPENAPI_RENAMES = {
    "minimum": "min",
    "maximum": "max",
    "minLength": "min_length",
    "maxLength": "max_length",
}

# 视频输入引用的元素契约（后端 model/param_normalize.go 的常量）
REFERENCE_TYPES = ("image", "video", "audio")
REFERENCE_ROLES = ("first_frame", "last_frame", "reference_image",
                   "reference_video", "reference_audio")


def sanitize(node):
    """递归剥掉服务端专有字段，保留其余结构原样。"""
    if isinstance(node, dict):
        return {
            k: sanitize(v)
            for k, v in node.items()
            if k not in SERVER_ONLY_KEYS
        }
    if isinstance(node, list):
        return [sanitize(v) for v in node]
    return node


def load_items(path):
    """读取 docs 导出文件里的 items 数组。"""
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    data = payload.get("data", payload)
    if isinstance(data, dict):
        return data.get("items") or data.get("models") or []
    if isinstance(data, list):
        return data
    return []


def parse_params(raw):
    """管理端导出的 params 是 JSON 字符串，客户端详情接口里是对象，两种都要吃。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return None


def normalize_category(value):
    """线上 /pricing 返回 'Image'/'Video'，docs 导出里是 'image'/'图片'，统一成小写英文。"""
    if not value:
        return None
    text = str(value).strip().lower()
    return {"图片": "image", "视频": "video", "文本": "text", "音频": "audio"}.get(text, text)


def inject_canonical_items(params):
    """补齐后端约定、但库里经常漏写的数组元素结构。

    input_references 的元素固定是 {type,url,role}，运行时只解析这三个键；
    images 的元素是图片字符串。缺 items 时客户端只能瞎猜，这里统一补上。
    """
    for block in params.values():
        if not isinstance(block, dict):
            continue
        props = block.get("properties")
        if not isinstance(props, dict):
            continue
        for name, spec in props.items():
            if not isinstance(spec, dict) or spec.get("type") != "array":
                continue
            if isinstance(spec.get("items"), dict) and spec["items"]:
                continue
            if name == "input_references":
                spec["items"] = {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "required": True,
                                 "enum": list(REFERENCE_TYPES)},
                        "url": {"type": "string", "required": True},
                        "role": {"type": "string", "enum": list(REFERENCE_ROLES)},
                    },
                }
            elif name == "images":
                spec["items"] = {"type": "string"}
    return params


def convert_openapi_prop(spec):
    """OpenAPI 属性 -> 快照内部的属性结构（递归）。"""
    if not isinstance(spec, dict):
        return {}
    out = {}
    for key in ("type", "description", "default", "enum"):
        if key in spec:
            out[key] = spec[key]
    for source, target in OPENAPI_RENAMES.items():
        if source in spec:
            out[target] = spec[source]
    for key in ("min", "max", "min_length", "max_length"):
        if key in spec and key not in out:
            out[key] = spec[key]
    if isinstance(spec.get("items"), dict):
        out["items"] = convert_openapi_prop(spec["items"])
    if isinstance(spec.get("properties"), dict):
        out["properties"] = {
            name: convert_openapi_prop(child)
            for name, child in spec["properties"].items()
        }
    return out


def openapi_to_params(schema, category):
    """OpenAPI Request schema -> {分类: {properties: {...}}}，required 还原成逐字段布尔。"""
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    required = set(item for item in (schema.get("required") or []) if isinstance(item, str))
    converted = {}
    for name, spec in props.items():
        prop = convert_openapi_prop(spec)
        if name in required:
            prop["required"] = True
        converted[name] = prop
    return {category: {"properties": converted}}


def fetch_api_items(base):
    """GET {base}/pricing/schema?all=1，返回 (items, meta)。"""
    url = base.rstrip("/") + "/pricing/schema?all=1"
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "easyapi-skill-build/1.0",
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("success"):
        raise SystemExit("接口返回失败：%s" % (payload.get("message") if isinstance(payload, dict) else payload))
    data = payload.get("data") or {}
    return data.get("items") or [], data


def build_from_api(base):
    """从线上 schema 接口构建快照。"""
    items, data = fetch_api_items(base)
    models = {}
    stats = {"scanned": len(items), "kept": 0, "skipped_no_params": 0}

    for item in items:
        name = item.get("model")
        if not name:
            continue
        category = normalize_category(item.get("category"))
        params = openapi_to_params(item.get("request"), category or "unknown")
        if not params:
            stats["skipped_no_params"] += 1
            continue
        params = inject_canonical_items(sanitize(params))

        endpoints = {}
        for endpoint in item.get("endpoints") or []:
            if isinstance(endpoint, dict) and endpoint.get("type") and endpoint.get("path"):
                endpoints[endpoint["type"]] = {
                    "path": endpoint["path"],
                    "method": endpoint.get("method") or "POST",
                }

        entry = {"category": category, "params": params}
        if endpoints:
            entry["endpoints"] = endpoints
        models[name] = entry
        stats["kept"] += 1

    return models, stats, {
        "generated_from": ["%s/pricing/schema" % base.rstrip("/")],
        "pricing_version": data.get("pricing_version"),
        "schema_version": data.get("schema_version"),
    }


def build(docs_dir, sources):
    models = {}
    stats = {"scanned": 0, "kept": 0, "skipped_no_params": 0}

    for name in sources:
        path = os.path.join(docs_dir, name)
        if not os.path.exists(path):
            print("warn: 缺少 %s，跳过" % path, file=sys.stderr)
            continue

        for item in load_items(path):
            stats["scanned"] += 1
            model_name = item.get("model_name")
            if not model_name:
                continue

            params = parse_params(item.get("params"))
            # 只有带 properties 的才算有效 schema；仅有模态摘要的没有校验价值
            if not params or not any(
                isinstance(v, dict) and v.get("properties") for v in params.values()
            ):
                stats["skipped_no_params"] += 1
                continue

            # 后扫到的文件不覆盖先扫到的：image/video 专门导出优先于 all_models 大杂烩
            if model_name in models:
                continue

            endpoints = item.get("endpoints")
            if isinstance(endpoints, str) and endpoints.strip():
                try:
                    endpoints = json.loads(endpoints)
                except ValueError:
                    endpoints = None

            entry = {
                "category": normalize_category(item.get("category")),
                "params": inject_canonical_items(sanitize(params)),
            }
            if item.get("description"):
                entry["description"] = item["description"]
            if item.get("tags"):
                entry["tags"] = item["tags"]
            if isinstance(endpoints, dict) and endpoints:
                entry["endpoints"] = endpoints

            models[model_name] = entry
            stats["kept"] += 1

    return models, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-api", metavar="BASE",
                        help="从线上 /pricing/schema?all=1 生成（推荐）")
    source.add_argument("--docs", help="easyapi-ai-api/docs 目录（离线）")
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    parser.add_argument("--sources", nargs="*", default=list(DEFAULT_SOURCES))
    args = parser.parse_args()

    if args.from_api:
        models, stats, extra_meta = build_from_api(args.from_api)
        generated_from = extra_meta.pop("generated_from")
    else:
        models, stats = build(args.docs, args.sources)
        extra_meta = {}
        generated_from = list(args.sources)

    if not models:
        print("error: 没有提取到任何模型，检查来源参数", file=sys.stderr)
        return 1

    meta = {
        "generated_at": datetime.date.today().isoformat(),
        "generated_from": generated_from,
        "model_count": len(models),
        "note": (
            "内置参数快照，用于线上 /pricing/schema 不可用或未声明 params 时兜底。"
            "线上接口返回有效 schema 时以线上为准；重跑 tools/build_snapshot.py --from-api 刷新。"
        ),
    }
    meta.update({key: value for key, value in extra_meta.items() if value})

    snapshot = {"_meta": meta, "models": dict(sorted(models.items()))}

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")

    by_cat = {}
    for entry in models.values():
        cat = entry.get("category") or "unknown"
        by_cat[cat] = by_cat.get(cat, 0) + 1

    print("已写入 %s" % args.out)
    print("  扫描 %d 条，保留 %d 条，无 params 跳过 %d 条"
          % (stats["scanned"], stats["kept"], stats["skipped_no_params"]))
    print("  分类分布: %s" % ", ".join("%s=%d" % kv for kv in sorted(by_cat.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
