#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyAPI 统一调用脚本（仅 Python 标准库，无需 pip 安装）。

设计目标：把「拼接口参数」这件事从模型手里拿走。
参数按模型 schema 预校验后才发送，异步任务由本脚本轮询到完成并落盘，
模型只需要读本脚本输出的紧凑摘要，不需要自己拼 JSON 或写轮询循环。

子命令：
  login / logout / config / quota / models / schema / image / video / chat / compare

运行 `python3 easyapi.py <子命令> --help` 看具体参数。
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import http.server
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# 网关入口。调用（/v1/...）、定价（/pricing）、额度（/usage/token）都在同一个域名下，
# 线上实测 token.easyapi.com 全部可用。私有化部署若把定价拆到别的域名，用 pricing_base 覆盖。
DEFAULT_GATEWAY_BASE = "https://token.easyapi.com"
DEFAULT_PRICING_BASE = DEFAULT_GATEWAY_BASE
# 控制台（前端）入口，与网关 API 不同域。浏览器授权页是前端路由，必须用这个域名开。
DEFAULT_CONSOLE_BASE = "https://www.easyapi.com"

# 非网关主机的特征响应。配错 base_url 时（比如指到一个普通的 Java 服务）会看到
# Spring Boot 的 "No static resource"，明确指出来比报一个含糊的 400 有用得多。
NOT_A_GATEWAY_MARKER = "No static resource"

# 1 USD = 500000 quota（后端 QuotaPerUnit）
QUOTA_PER_UNIT = 500000

# 状态目录（配置 + 缓存）。EASYAPI_HOME 可覆盖，便于测试与多套环境隔离。
CONFIG_DIR = os.environ.get("EASYAPI_HOME") or os.path.join(os.path.expanduser("~"), ".easyapi")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(CONFIG_DIR, "cache")
DETAIL_CACHE_DIR = os.path.join(CACHE_DIR, "detail")
# 单模型 schema 缓存（/pricing/schema）与全量参数包缓存。
# 全量包让参数校验不再依赖随版本发布的内置快照，新模型上线即自动生效。
SCHEMA_DETAIL_CACHE_DIR = os.path.join(CACHE_DIR, "schema")
SCHEMA_BUNDLE_PATH = os.path.join(CACHE_DIR, "schemas.json")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_PATH = os.path.join(SKILL_DIR, "references", "model-snapshot.json")

DEFAULT_TTL = 86400          # 模型/参数缓存 24 小时
DEFAULT_HTTP_TIMEOUT = 120
DEFAULT_VIDEO_TIMEOUT = 900  # 生视频最长等待
DEFAULT_OUT_DIR = "easyapi-out"

# 登录后自动创建/复用的令牌名。固定名复用：重复登录不会堆积一堆 key，
# 额度受账号余额约束，符合「这个 key 就是我账号」的直觉。
CLI_KEY_NAME = "easyapi-skill"

# 令牌状态（common.TokenStatus*），给用户看的文案
TOKEN_STATUS_LABEL = {1: "启用", 2: "禁用", 3: "已过期", 4: "额度耗尽"}
TOKEN_STATUS_ENABLED = 1

# 网页验证码开关的 app-config 字段名。开启时 CLI 密码登录会被挡，
# 提前探测出来可以直接指路，而不是让用户对着一个莫名的 403 猜。
CAPTCHA_CONFIG_KEYS = ("turnstile_check", "aliyun_captcha_enabled")

# 浏览器授权（loopback）
DEFAULT_CLIENT_NAME = "EasyAPI CLI"
AUTH_CALLBACK_WAIT = 300       # 等用户在浏览器里点授权的最长时间

_CALLBACK_OK_HTML = (
    "<!doctype html><meta charset=\"utf-8\"><title>授权成功</title>"
    "<body style=\"font-family:system-ui,sans-serif;text-align:center;padding-top:18vh\">"
    "<h2>授权成功</h2><p>请回到命令行工具继续，本页面可以关闭。</p></body>")

_CALLBACK_FAIL_HTML = (
    "<!doctype html><meta charset=\"utf-8\"><title>授权失败</title>"
    "<body style=\"font-family:system-ui,sans-serif;text-align:center;padding-top:18vh\">"
    "<h2>授权失败</h2><p>%s</p><p>请回到命令行工具重试。</p></body>")

# 校验时允许但不认识就告警的字段（网关会透传给上游）
UNKNOWN_PARAM_LEVEL = "warn"

# 请求信封字段：由调用方必然提供，不属于 params.properties，
# 不能因为 schema 里没有就告警（否则每次调用都会出现无意义的提示）
ENVELOPE_FIELDS = frozenset({"model"})

# params 支持的四类模型，顺序固定，用于分类推断与报错文案
SCHEMA_CATEGORIES = ("text", "image", "audio", "video")


class EasyAPIError(Exception):
    """带可执行建议的错误。hint 会直接展示给用户。"""

    def __init__(self, message, hint=None, detail=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, payload, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows 上权限位语义有限，失败不影响使用


def load_config():
    cfg = _read_json(CONFIG_PATH, {})
    return cfg if isinstance(cfg, dict) else {}


def save_config(cfg):
    _write_json(CONFIG_PATH, cfg, private=True)


def resolve_api_key(cfg, required=True):
    """key 来源优先级：环境变量 > 配置文件。

    刻意不把 key 作为命令行参数，避免进入 shell history 与进程列表。
    """
    key = (os.environ.get("EASYAPI_API_KEY") or "").strip()
    if key:
        return key, "环境变量 EASYAPI_API_KEY"
    key = (cfg.get("api_key") or "").strip()
    if key:
        return key, "配置文件 %s" % CONFIG_PATH
    if not required:
        return None, None
    raise EasyAPIError(
        "未配置 EasyAPI API Key",
        hint=(
            "agent 请直接运行（Bash 超时给足 6 分钟，脚本最长等 5 分钟授权）：\n"
            "    python3 %s login --browser\n"
            "用户只需在弹出的网页里点一下「授权」，脚本会自动创建/复用 API Key 并验证，"
            "全程无凭据经过对话；浏览器没弹出时把 stderr 里的授权页地址转成链接发给用户。\n"
            "若上面命令报错，按报错里的提示降级（未部署授权端点时让用户在自己的终端"
            "运行 `login` 或 `login --access-token`，key 不会出现在对话里）。"
            % os.path.abspath(__file__)
        ),
    )


def resolve_bases(cfg):
    """返回 (网关入口, 定价入口)。定价入口默认跟随网关入口，一般不需要单独配置。"""
    base = (
        os.environ.get("EASYAPI_BASE_URL")
        or cfg.get("base_url")
        or DEFAULT_GATEWAY_BASE
    ).rstrip("/")
    pricing = (
        os.environ.get("EASYAPI_PRICING_BASE_URL")
        or cfg.get("pricing_base")
        or base
    ).rstrip("/")
    return base, pricing


def resolve_console_base(cfg):
    """控制台（前端）地址。

    浏览器授权页是前端路由，和网关 API **不同域**：线上前端在 www.easyapi.com，
    接口在 token.easyapi.com。两者路径同名（都是 /cli/authorize），
    拿网关域名去开页面会打到只接受 POST 的 API 路由上（GET 返回 404），
    所以这里必须分开解析。
    """
    return (
        os.environ.get("EASYAPI_CONSOLE_BASE_URL")
        or cfg.get("console_base")
        or DEFAULT_CONSOLE_BASE
    ).rstrip("/")


def resolve_ttl(cfg):
    try:
        return int(cfg.get("cache_ttl", DEFAULT_TTL))
    except (TypeError, ValueError):
        return DEFAULT_TTL


def resolve_timeout(args, fallback=DEFAULT_HTTP_TIMEOUT):
    value = getattr(args, "timeout", None)
    return value if isinstance(value, int) and value > 0 else fallback


def default_model(cfg, category):
    return (cfg.get("default_%s_model" % category) or "").strip() or None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _send_raw(method, url, data, content_type, headers, timeout):
    """返回 (status, headers, raw_bytes)。HTTP 错误不抛异常，交给调用方判断。"""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if content_type:
        req.add_header("Content-Type", content_type)
    req.add_header("User-Agent", "easyapi-skill/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        raise EasyAPIError(
            "网络请求失败：%s" % exc.reason,
            hint="检查网络连通性，或确认 base_url 配置是否正确（easyapi.py config）。",
        )
    except TimeoutError:
        raise EasyAPIError("请求超时（%ss）：%s" % (timeout, url))


def http_request(method, url, headers=None, body=None, timeout=DEFAULT_HTTP_TIMEOUT):
    """发 JSON 请求。返回 (status, headers, raw_bytes)。"""
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return _send_raw(method, url, data, "application/json" if data is not None else None,
                     headers, timeout)


def encode_multipart(fields, files):
    """构造 multipart/form-data 请求体（仅标准库）。

    fields 是 {名字: 值}，值为 None 的跳过；files 是 [(字段名, 文件名, 字节)]，
    同一字段名可重复出现 —— 网关侧多图就是靠重复同名部件表达的。
    """
    boundary = "----easyapi" + secrets.token_hex(16)
    buf = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        buf += ("--%s\r\n" % boundary).encode()
        buf += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
        buf += str(value).encode("utf-8")
        buf += b"\r\n"
    for name, filename, data in files:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        buf += ("--%s\r\n" % boundary).encode()
        buf += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (name, filename)).encode()
        buf += ("Content-Type: %s\r\n\r\n" % mime).encode()
        buf += data
        buf += b"\r\n"
    buf += ("--%s--\r\n" % boundary).encode()
    return "multipart/form-data; boundary=%s" % boundary, bytes(buf)


def http_request_multipart(method, url, fields, files, headers=None,
                           timeout=DEFAULT_HTTP_TIMEOUT):
    """发 multipart/form-data 请求（图生图编辑用）。返回 (status, headers, raw_bytes)。"""
    content_type, body = encode_multipart(fields, files)
    return _send_raw(method, url, body, content_type, headers, timeout)


def http_request_multipart_json(method, url, fields, files, headers=None,
                                timeout=DEFAULT_HTTP_TIMEOUT):
    """发 multipart 请求并把响应按 JSON 解析。"""
    status, _hdrs, raw = http_request_multipart(method, url, fields, files,
                                                headers=headers, timeout=timeout)
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        payload = json.loads(text) if text.strip() else None
    except ValueError:
        payload = None
    return status, payload, text


def http_json(method, url, headers=None, body=None, timeout=DEFAULT_HTTP_TIMEOUT):
    status, _hdrs, raw = http_request(method, url, headers, body, timeout)
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        payload = json.loads(text) if text.strip() else None
    except ValueError:
        payload = None
    return status, payload, text


def translate_error(status, payload, text, model=None):
    """把网关错误翻译成可执行建议。这是 skill 降低试错成本的关键一环。"""
    message = ""
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "detail"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                message = val.strip()
                break
            if isinstance(val, dict) and isinstance(val.get("message"), str):
                message = val["message"].strip()
                break
    if not message:
        message = (text or "").strip()[:400] or "HTTP %s" % status

    hint = None
    low = message.lower()

    # 配错 base_url 是最容易犯又最难自查的错误，优先识别
    if NOT_A_GATEWAY_MARKER in (text or ""):
        return message, (
            "这个地址不是 EasyAPI 网关（返回了 %r，是普通 Java 服务的 404 页面）。"
            "用 `easyapi.py config` 检查入口地址，默认应为 %s。"
            % (NOT_A_GATEWAY_MARKER, DEFAULT_GATEWAY_BASE)
        )

    if status == 401 or "invalid token" in low or "token not provided" in low:
        hint = "API Key 无效或未提供。用 `easyapi.py quota` 自检，或重新执行 `easyapi.py login`。"
    elif status == 403:
        hint = "无权访问该模型，可能是令牌未开通该模型或分组不匹配。用 `easyapi.py models` 看当前可见模型。"
    elif status == 404:
        hint = "模型不存在或对当前分组不可见。用 `easyapi.py models` 确认可用模型名。"
    elif status == 429 or "rate limit" in low or "too many" in low:
        hint = "触发限流，稍后重试。"
    elif status in (400, 422):
        if "model is required" in low:
            hint = "请求体里没有 model 字段。用 --model 指定，或用 `easyapi.py config --default-image-model X` 设默认值。"
        elif "insufficient" in low or "quota" in low or "balance" in low:
            hint = "额度不足，请充值后重试。用 `easyapi.py quota` 查当前余额。"
        elif "no available channel" in low or "channel" in low:
            hint = "该模型当前没有可用渠道，换一个模型或稍后重试。"
        else:
            hint = "参数被上游拒绝。用 `easyapi.py schema %s` 看该模型的合法参数与取值。" % (model or "<模型名>")
    elif status == 402:
        hint = "额度不足，请充值后重试。用 `easyapi.py quota` 查当前余额。"
    elif status >= 500:
        hint = "网关或上游异常，可稍后重试。"

    return message, hint


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------


def cache_read(path, ttl, refresh=False):
    if refresh:
        return None
    entry = _read_json(path)
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if ttl >= 0 and (time.time() - fetched_at) > ttl:
        return None
    return entry


def cache_write(path, payload):
    entry = dict(payload)
    entry["fetched_at"] = time.time()
    _write_json(path, entry)
    return entry


def _safe_name(model):
    return re.sub(r"[^A-Za-z0-9._-]", "_", model)


def _detail_cache_path(model):
    return os.path.join(DETAIL_CACHE_DIR, _safe_name(model) + ".json")


def _schema_cache_path(model):
    return os.path.join(SCHEMA_DETAIL_CACHE_DIR, _safe_name(model) + ".json")


def clear_detail_cache():
    if not os.path.isdir(DETAIL_CACHE_DIR):
        return
    for name in os.listdir(DETAIL_CACHE_DIR):
        if name.endswith(".json"):
            try:
                os.remove(os.path.join(DETAIL_CACHE_DIR, name))
            except OSError:
                pass


def clear_schema_cache():
    """作废参数 schema 缓存（全量包 + 单模型）。"""
    for directory in (SCHEMA_DETAIL_CACHE_DIR,):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass
    try:
        if os.path.exists(SCHEMA_BUNDLE_PATH):
            os.remove(SCHEMA_BUNDLE_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 模型列表与 schema
# --------------------------------------------------------------------------


def fetch_pricing(cfg, refresh=False):
    """拉模型列表（匿名可访问，不需要 key）。返回 (payload, source)。"""
    ttl = resolve_ttl(cfg)
    cache_path = os.path.join(CACHE_DIR, "pricing.json")
    cached = cache_read(cache_path, ttl, refresh)
    if cached and cached.get("payload"):
        return cached["payload"], "cache"

    _base, pricing_base = resolve_bases(cfg)
    url = pricing_base + "/pricing"
    status, payload, text = http_json("GET", url)
    if status != 200 or not isinstance(payload, dict) or not payload.get("data"):
        message, hint = translate_error(status, payload, text)
        raise EasyAPIError("获取模型列表失败：%s" % message, hint=hint)

    old = _read_json(cache_path) or {}
    old_version = old.get("pricing_version")
    new_version = payload.get("pricing_version")
    cache_write(cache_path, {"payload": payload, "pricing_version": new_version})
    # pricing_version 是后端给客户端的契约变更信号，变了就作废单模型详情与参数缓存
    if old_version and new_version and old_version != new_version:
        clear_detail_cache()
        clear_schema_cache()

    return payload, "network"


def fetch_detail(cfg, model, refresh=False):
    """拉单模型详情（匿名可访问）。返回 (params_dict_or_None, source)。"""
    ttl = resolve_ttl(cfg)
    cache_path = _detail_cache_path(model)
    cached = cache_read(cache_path, ttl, refresh)
    if cached is not None:
        return cached.get("params"), "cache"

    _base, pricing_base = resolve_bases(cfg)
    url = pricing_base + "/pricing/detail?" + urllib.parse.urlencode({"model_name": model})
    try:
        status, payload, text = http_json("GET", url)
    except EasyAPIError:
        # 详情只是校验用的辅助信息，拿不到不该让整个生成流程失败 ——
        # 交给上层回退到内置快照
        return None, "error"
    if status == 404:
        cache_write(cache_path, {"params": None, "reason": "not_found"})
        return None, "not_found"
    if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
        # 详情拿不到不该让整个命令失败，降级到快照
        return None, "error"

    data = payload.get("data") or {}
    params = data.get("params")
    cache_write(cache_path, {"params": params, "model": model})
    return params, "network"


def load_snapshot():
    snap = _read_json(SNAPSHOT_PATH, {})
    return snap if isinstance(snap, dict) else {}


def _category_key(params, category):
    """params 里的分类键是 image/video/text；模型 category 字段可能是 Image/Video。"""
    if not isinstance(params, dict) or not category:
        return None
    want = category.strip().lower()
    for key in params:
        if str(key).strip().lower() == want:
            return key
    return None


def extract_properties(params, category):
    key = _category_key(params, category)
    if not key:
        return None
    block = params.get(key)
    if not isinstance(block, dict):
        return None
    props = block.get("properties")
    return props if isinstance(props, dict) and props else None


def _openapi_prop_to_internal(spec):
    """把 /pricing/schema 的 OpenAPI 3.1 属性还原成脚本内部结构。

    线上接口用 JSON Schema 命名（minimum / minLength，required 提到父级数组），
    内置快照与审计口径沿用 model.params 的命名（min / min_length，required 布尔）。
    统一在这里做一次转换，后面的校验、展示、合并只认一种形态。
    """
    if not isinstance(spec, dict):
        return {}
    out = {}
    for key in ("type", "description", "default", "enum", "items", "properties"):
        if key in spec:
            out[key] = spec[key]
    if "minimum" in spec:
        out["min"] = spec["minimum"]
    if "maximum" in spec:
        out["max"] = spec["maximum"]
    if "minLength" in spec:
        out["min_length"] = spec["minLength"]
    if "maxLength" in spec:
        out["max_length"] = spec["maxLength"]
    # 兼容旧命名（内置快照、老版本接口返回的仍是 min/max/min_length）
    for key in ("min", "max", "min_length", "max_length"):
        if key in spec:
            out[key] = spec[key]
    if isinstance(out.get("items"), dict):
        out["items"] = _openapi_prop_to_internal(out["items"])
    if isinstance(out.get("properties"), dict):
        out["properties"] = {
            name: _openapi_prop_to_internal(child)
            for name, child in out["properties"].items()
        }
    return out


def openapi_schema_to_props(schema):
    """从 OpenAPI Request schema 取回 properties，并把父级 required 还原成逐字段布尔。"""
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    required = set(item for item in (schema.get("required") or []) if isinstance(item, str))
    out = {}
    for name, spec in props.items():
        prop = _openapi_prop_to_internal(spec)
        if name in required:
            prop["required"] = True
        out[name] = prop
    return out


def fetch_schema_bundle(cfg, refresh=False):
    """拉全量参数 schema（GET /pricing/schema?all=1）。

    返回 {"items": {模型名: {category, endpoints, schemas}}, "pricing_version": ...}。
    老部署没有这个接口时返回 None，并缓存一个 unsupported 标记，避免每次调用都白跑一趟。
    """
    ttl = resolve_ttl(cfg)
    cached = cache_read(SCHEMA_BUNDLE_PATH, ttl, refresh)
    if cached is not None:
        if cached.get("unsupported") or cached.get("error"):
            return None
        bundle = cached.get("bundle")
        if isinstance(bundle, dict):
            return bundle

    _base, pricing_base = resolve_bases(cfg)
    url = pricing_base + "/pricing/schema?all=1"
    try:
        status, payload, text = http_json("GET", url)
    except EasyAPIError:
        cache_write(SCHEMA_BUNDLE_PATH, {"error": True})
        return None

    if status == 404:
        cache_write(SCHEMA_BUNDLE_PATH, {"unsupported": True})
        return None
    if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
        cache_write(SCHEMA_BUNDLE_PATH, {"error": True})
        return None

    data = payload.get("data") or {}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        # 老网关或代理把 /pricing/schema 落到了 /pricing 的响应上（data 是列表），
        # 当作不支持处理，走后面的单模型/详情/快照回退。
        cache_write(SCHEMA_BUNDLE_PATH, {"unsupported": True})
        return None
    items = {}
    for row in data["items"]:
        if not isinstance(row, dict):
            continue
        name = row.get("model")
        if not name:
            continue
        category = str(row.get("category") or "").strip().lower() or None
        schemas = {}
        props = openapi_schema_to_props(row.get("request"))
        if props:
            # 统一包成 {分类: {properties: {...}}}，与 /pricing/detail 的 params 同形
            schemas[category or "unknown"] = {"properties": props}
        items[name] = {
            "category": category,
            "endpoints": row.get("endpoints") or [],
            "schemas": schemas,
        }

    bundle = {
        "items": items,
        "pricing_version": data.get("pricing_version") or payload.get("pricing_version"),
        "schema_version": data.get("schema_version"),
    }
    cache_write(SCHEMA_BUNDLE_PATH, {"bundle": bundle})
    return bundle


def fetch_model_schema(cfg, model, refresh=False):
    """单模型 schema 接口（批量接口不可用时的第二顺位）。"""
    ttl = resolve_ttl(cfg)
    cache_path = _schema_cache_path(model)
    cached = cache_read(cache_path, ttl, refresh)
    if cached is not None:
        return cached.get("payload")

    _base, pricing_base = resolve_bases(cfg)
    url = pricing_base + "/pricing/schema?" + urllib.parse.urlencode({"model_name": model})
    try:
        status, payload, text = http_json("GET", url)
    except EasyAPIError:
        return None
    if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
        cache_write(cache_path, {"payload": None, "status": status})
        return None

    entry = payload.get("data")
    if not isinstance(entry, dict):
        cache_write(cache_path, {"payload": None, "reason": "unexpected_shape"})
        return None
    category = str(entry.get("category") or "").strip().lower() or None
    result = {
        "category": category,
        "endpoints": entry.get("endpoints") or [],
        "schemas": {},
        "example": entry.get("example"),
        "billing": entry.get("billing"),
    }
    props = openapi_schema_to_props(entry.get("request"))
    if props:
        result["schemas"][category or "unknown"] = {"properties": props}
    cache_write(cache_path, {"payload": result})
    return result


def merge_props(primary, secondary):
    """按属性合并两套参数定义：primary 覆盖同名属性，secondary 只补缺失的。

    后端只转发 params 里声明过的动态字段，所以「谁声明谁说了算」——线上声明
    永远优先。内置快照只用来补线上还没填的字段，避免线上只填了 prompt/duration
    就把分辨率的枚举与上下限整份丢掉（以前的整份回退就是这个毛病）。
    """
    merged = {}
    for name, spec in (secondary or {}).items():
        if isinstance(spec, dict):
            merged[name] = dict(spec)
    # added = 只存在于快照、线上没声明的字段（用于提示合并了哪些补充定义）
    added = sorted(name for name in (secondary or {}) if name not in (primary or {}))
    for name, spec in (primary or {}).items():
        merged[name] = dict(spec) if isinstance(spec, dict) else spec
    return merged, added


def resolve_schema(cfg, model, category, refresh=False):
    """解析模型参数 schema，按四级来源取最权威的一份，再逐字段合并。

    1. /pricing/schema?all=1 全量包（一次拿全，带 endpoints，新模型自动生效）
    2. /pricing/schema?model_name= 单模型（只有单模型接口的部署）
    3. /pricing/detail 的 params（更老的部署）
    4. 内置快照 model-snapshot.json（完全离线兜底）
    """
    notes = []
    meta = {"sources": [], "endpoints": [], "category": None, "example": None}
    primary = {}
    primary_source = None

    bundle = fetch_schema_bundle(cfg, refresh)
    if bundle:
        entry = (bundle.get("items") or {}).get(model)
        if isinstance(entry, dict):
            meta["endpoints"] = entry.get("endpoints") or []
            meta["category"] = entry.get("category")
            props = extract_properties(entry.get("schemas"), category)
            if props:
                primary = props
                primary_source = "schema-bundle"

    if not primary:
        single = fetch_model_schema(cfg, model, refresh)
        if isinstance(single, dict):
            meta["endpoints"] = single.get("endpoints") or meta["endpoints"]
            meta["category"] = meta["category"] or single.get("category")
            meta["example"] = single.get("example")
            props = extract_properties(single.get("schemas"), category)
            if props:
                primary = props
                primary_source = "schema"

    if not primary:
        live_params, _live_source = fetch_detail(cfg, model, refresh)
        props = extract_properties(live_params, category)
        if props:
            primary = props
            primary_source = "live-detail"

    snap = load_snapshot()
    snap_entry = (snap.get("models") or {}).get(model)
    snap_props = extract_properties((snap_entry or {}).get("params"), category)
    snapshot_date = (snap.get("_meta") or {}).get("generated_at")

    if primary:
        merged, added = merge_props(primary, snap_props)
        meta["sources"] = [primary_source] + (["snapshot"] if added else [])
        meta["source"] = primary_source
        meta["snapshot_date"] = snapshot_date
        if added:
            shown = "、".join(added[:6]) + ("…" if len(added) > 6 else "")
            notes.append(
                "线上未声明这 %d 个参数，已用内置快照补全取值校验：%s。"
                "未声明的动态字段可能被网关丢弃，报错时优先看 schema 里的字段。"
                % (len(added), shown)
            )
        return merged, meta, notes

    if snap_props:
        meta.update({"sources": ["snapshot"], "source": "snapshot",
                     "snapshot_date": snapshot_date})
        return snap_props, meta, notes

    meta["source"] = None
    notes.append(
        "线上与内置快照都没有 %s 的参数定义，已跳过参数预校验（请求仍会发送）。"
        "若报参数错误，请用 `easyapi.py schema %s` 确认，或到管理端补该模型的 params。"
        % (model, model)
    )
    return {}, meta, notes


def resolve_category(cfg, model, explicit=None, refresh=False):
    """推断模型的参数分类。绝不静默按 image 处理（旧版在这里会误判视频模型）。

    顺序：--category → /pricing 的 category → schema 包里的 category →
    逐个分类试探 → 报错并列出候选。
    """
    if explicit:
        return str(explicit).strip().lower()

    try:
        entry, _src = find_model(cfg, model, None, refresh)
    except EasyAPIError:
        entry = None
    if entry and entry.get("category"):
        candidate = str(entry["category"]).strip().lower()
        if candidate in SCHEMA_CATEGORIES:
            return candidate

    bundle = fetch_schema_bundle(cfg, refresh)
    if bundle:
        row = (bundle.get("items") or {}).get(model)
        if isinstance(row, dict) and row.get("category"):
            return str(row["category"]).strip().lower()

    single = fetch_model_schema(cfg, model, refresh)
    if isinstance(single, dict) and single.get("category"):
        return str(single["category"]).strip().lower()

    for candidate in SCHEMA_CATEGORIES:
        props, _meta, _notes = resolve_schema(cfg, model, candidate, refresh)
        if props:
            return candidate

    raise EasyAPIError(
        "无法判断 %s 属于哪一类（image / video / text / audio）" % model,
        hint="用 --category 指定；或先 `easyapi.py models --search %s` 确认模型名。" % model,
    )


def _endpoint_matches_kind(item, kind):
    """端点是否与本次操作同类。

    supported_endpoint_types 反映的是渠道接线方式（OpenAI 兼容渠道就是 openai →
    /v1/chat/completions），不代表图片/视频该打哪个路径。所以只在类型或路径里
    真的出现 image/video 时才采用，否则回退到该操作的约定路径。
    """
    text = ("%s %s" % ((item or {}).get("type") or "",
                       (item or {}).get("path") or "")).lower()
    if kind == "image":
        return "image" in text
    if kind == "video":
        return "video" in text
    if kind == "audio":
        return "audio" in text
    return True


def resolve_submit_endpoint(cfg, model, kind, meta=None, refresh=False):
    """解析模型的提交路径与可选端点列表。

    优先用 /pricing 的 supported_endpoint（端点类型 -> {path, method}）配合模型的
    supported_endpoint_types，但只采纳与本次操作同类的端点。解析不到时回退约定路径：
    /v1/videos 与 /v1/video/generations 在后端都指向同一个任务处理器，回退是安全的。
    """
    fallback = {"image": "/v1/images/generations",
                "video": "/v1/videos",
                "audio": "/v1/audio/speech"}.get(kind)
    endpoints = list((meta or {}).get("endpoints") or [])

    try:
        entry, _src = find_model(cfg, model, kind, refresh)
    except EasyAPIError:
        entry = None
    if entry:
        try:
            payload, _src = fetch_pricing(cfg, refresh)
        except EasyAPIError:
            payload = {}
        supported = payload.get("supported_endpoint") or {}
        for endpoint_type in entry.get("supported_endpoint_types") or []:
            info = supported.get(endpoint_type)
            if isinstance(info, dict) and info.get("path"):
                candidate = {
                    "type": endpoint_type,
                    "path": info["path"],
                    "method": info.get("method") or "POST",
                }
                if candidate not in endpoints:
                    endpoints.append(candidate)

    for item in endpoints:
        path = (item or {}).get("path")
        if path and _endpoint_matches_kind(item, kind):
            return path, endpoints
    return fallback, endpoints


def find_model(cfg, model, category=None, refresh=False):
    """在模型列表里找一条记录，顺带拿到 quota_type 等计费字段。"""
    payload, source = fetch_pricing(cfg, refresh)
    for entry in payload.get("data") or []:
        if entry.get("model_name") == model:
            if category and (entry.get("category") or "").lower() != category.lower():
                continue
            return entry, source
    return None, source


def models_by_category(cfg, category, refresh=False):
    payload, source = fetch_pricing(cfg, refresh)
    want = category.strip().lower()
    rows = [
        m for m in (payload.get("data") or [])
        if (m.get("category") or "").strip().lower() == want
    ]
    return rows, payload, source


# --------------------------------------------------------------------------
# 参数归一化与校验
# --------------------------------------------------------------------------

# 尺寸/宽高比里的高频误写：全角乘号、星号、大写 X
_SIZE_SEP_RE = re.compile(r"\s*[×✕✖＊*Xxｘ]\s*")
# 只有整串形如「数字 x 数字」才认为是在写尺寸，
# 否则会把提示词里的 x 也改掉（"一只猫 x 一只狗"）
_SIZE_PAIR_RE = re.compile(r"^\s*\d+\s*[×✕✖＊*Xxｘ]\s*\d+\s*$")


def normalize_size_like(value):
    """把 1024×1024 / 1024*1024 / 1024 X 1024 归一成 1024x1024。

    只在整串就是一个尺寸对时才改写，避免误伤 prompt 等自由文本。
    """
    if not isinstance(value, str):
        return value
    if not _SIZE_PAIR_RE.match(value):
        return value.strip()
    return _SIZE_SEP_RE.sub("x", value).strip()


def coerce_value(name, spec, value):
    """按 schema 声明的类型把 CLI 字符串转成正确的 JSON 类型。"""
    if not isinstance(spec, dict):
        return value, []

    declared = (spec.get("type") or "").strip().lower()
    notes = []

    if isinstance(value, (list, dict, bool)):
        return value, notes

    if isinstance(value, str):
        value = value.strip()

    if declared == "integer":
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise EasyAPIError(
                "参数 %s 需要整数，收到 %r" % (name, value),
                hint="例：--%s 3" % name.replace("_", "-"),
            )
        if num != int(num):
            notes.append("参数 %s 收到 %s，已取整为 %d" % (name, value, int(num)))
        return int(num), notes

    if declared == "number":
        try:
            return float(value), notes
        except (TypeError, ValueError):
            raise EasyAPIError("参数 %s 需要数字，收到 %r" % (name, value))

    if declared == "boolean":
        low = str(value).strip().lower()
        if low in ("true", "1", "yes", "y", "on"):
            return True, notes
        if low in ("false", "0", "no", "n", "off"):
            return False, notes
        raise EasyAPIError(
            "参数 %s 需要布尔值，收到 %r" % (name, value),
            hint="用 --%s true / --%s false" % (
                name.replace("_", "-"), name.replace("_", "-")),
        )

    return value, notes


def normalize_enum(name, spec, value):
    """枚举值做大小写不敏感匹配，命中后回写成 schema 里的标准写法。"""
    allowed = spec.get("enum")
    if not isinstance(allowed, list) or not allowed:
        return value, []

    if value in allowed:
        return value, []

    if isinstance(value, str):
        for candidate in allowed:
            if isinstance(candidate, str) and candidate.lower() == value.lower():
                return candidate, [
                    "参数 %s 的取值 %r 已按模型定义规范化为 %r" % (name, value, candidate)
                ]

    raise EasyAPIError(
        "参数 %s 取值 %r 不在允许集合中" % (name, value),
        hint="可选值：%s" % " / ".join(repr(v) for v in allowed),
    )


def check_bounds(name, spec, value):
    notes = []
    if isinstance(value, bool):
        return notes

    if isinstance(value, (int, float)):
        low, high = spec.get("min"), spec.get("max")
        if isinstance(low, (int, float)) and value < low:
            raise EasyAPIError(
                "参数 %s = %s 小于下限 %s" % (name, value, low),
                hint="取值范围 %s ~ %s" % (low, high if high is not None else "∞"),
            )
        if isinstance(high, (int, float)) and value > high:
            raise EasyAPIError(
                "参数 %s = %s 超过上限 %s" % (name, value, high),
                hint="取值范围 %s ~ %s" % (low if low is not None else "-∞", high),
            )

    if isinstance(value, str):
        min_len, max_len = spec.get("min_length"), spec.get("max_length")
        if isinstance(min_len, int) and len(value) < min_len:
            raise EasyAPIError("参数 %s 长度不足 %d" % (name, min_len))
        if isinstance(max_len, int) and len(value) > max_len:
            raise EasyAPIError(
                "参数 %s 长度 %d 超过上限 %d 字符" % (name, len(value), max_len),
                hint="请精简该字段后重试。",
            )

    return notes


def validate_params(props, values, strict=False, known_extra=()):
    """按 schema 校验并归一化参数。返回 (clean, notes, warnings)。

    这是整个 skill 的核心：把「传参错误」在本地拦下来，并给出精确到参数的提示。
    known_extra 里的字段视为脚本自己按上游契约注入的（如参考图字段），
    不参与 schema 检查、也不告警 —— 我们知道它们是对的，只是模型声明里没有。
    """
    clean = {}
    notes = []
    warnings = []

    if not props:
        return dict(values), notes, warnings

    for name, raw in values.items():
        if raw is None:
            continue
        if name in ENVELOPE_FIELDS or name in known_extra:
            clean[name] = raw
            continue
        spec = props.get(name)
        if spec is None:
            msg = "参数 %s 不在该模型的参数定义里" % name
            if strict:
                raise EasyAPIError(
                    msg,
                    hint="去掉该参数，或用 --param %s=值 显式透传给上游。" % name,
                )
            warnings.append(msg + "，已原样透传给上游")
            clean[name] = raw
            continue

        value = raw
        if isinstance(value, str):
            value = normalize_size_like(value)

        value, n1 = coerce_value(name, spec, value)
        value, n2 = normalize_enum(name, spec, value)
        n3 = check_bounds(name, spec, value)

        notes.extend(n1)
        notes.extend(n2)
        notes.extend(n3)
        clean[name] = value

    # 必填校验
    missing = [
        name for name, spec in props.items()
        if isinstance(spec, dict) and spec.get("required") is True and name not in clean
    ]
    if missing:
        details = []
        for name in missing:
            desc = (props.get(name) or {}).get("description") or ""
            details.append("  - %s：%s" % (name, desc) if desc else "  - %s" % name)
        raise EasyAPIError(
            "缺少必填参数：%s" % "、".join(missing),
            hint="必填参数说明：\n%s" % "\n".join(details),
        )

    return clean, notes, warnings


def fill_defaults(props, values, keys):
    """把 schema 里的默认值填进请求体（仅限显式列出的键，避免污染请求）。"""
    out = dict(values)
    for key in keys:
        if key in out:
            continue
        spec = props.get(key)
        if isinstance(spec, dict) and "default" in spec:
            out[key] = spec["default"]
    return out


def fill_declared_defaults(props, values):
    """只填 schema 里显式声明的默认值。

    不再替用户凭空补 size / duration / resolution：这些值一旦猜错会直接改变
    出片规格与扣费。上游有默认值就交给上游，没有就让 schema 的 required 报错。
    """
    keys = [name for name, spec in (props or {}).items()
            if isinstance(spec, dict) and "default" in spec]
    return fill_defaults(props, values, keys)


def parse_param_flags(pairs):
    """解析 --param key=value，值尝试按 JSON 解析，失败则当字符串。"""
    out = {}
    for item in pairs or []:
        if "=" not in item:
            raise EasyAPIError(
                "--param 需要 key=value 形式，收到 %r" % item,
                hint="例：--param negative_prompt=模糊",
            )
        key, raw = item.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


class Reporter:
    """stdout 只放结果，stderr 放进度，方便 agent 稳定解析。"""

    def __init__(self, as_json=False, quiet=False):
        self.as_json = as_json
        self.quiet = quiet

    def progress(self, message):
        if not self.quiet:
            sys.stderr.write("[easyapi] %s\n" % message)
            sys.stderr.flush()

    def result(self, payload, human_lines):
        if self.as_json:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        else:
            for line in human_lines:
                sys.stdout.write(line + "\n")
        sys.stdout.flush()


def usd_from_quota(quota):
    try:
        return float(quota) / QUOTA_PER_UNIT
    except (TypeError, ValueError):
        return 0.0


def fmt_usd(value):
    if value is None:
        return "—"
    return "$%.4f" % value


QUOTA_TYPE_LABEL = {0: "按量", 1: "按次", 2: "按秒"}


def billing_label(entry):
    return QUOTA_TYPE_LABEL.get(entry.get("quota_type"), "未知")


def pick_ratio(table, resolution):
    """在分辨率倍率表里取值。

    各模型的分辨率键写法并不统一：Kling 用 720p/1080p/2k/4k，
    seedance-2.5 却用 480/720（不带 p）。所以要做容错匹配，否则会误判成「没有价格」。
    """
    if not isinstance(table, dict) or not table:
        return None
    if not resolution:
        return table.get("default")

    want = str(resolution).strip().lower()
    for cand in (want, want.rstrip("p"), want + "p"):
        if cand in table:
            return table[cand]

    norm = want.rstrip("p")
    for key, val in table.items():
        if str(key).strip().lower().rstrip("p") == norm:
            return val
    return table.get("default")


def estimate_usd(entry, group_ratio=1.0, seconds=None, resolution=None, n=None):
    """估算单次调用费用（美元）。返回 (金额, 无法估算的原因)。

    按量模型依赖 token 数，无法在本地估算。
    注意：这里不乘 model_discount —— 该折扣是否计入实际账单口径不稳定
    （见 references/models.md），价格对比用未折扣价更可靠。
    """
    quota_type = entry.get("quota_type")
    ratio = group_ratio if isinstance(group_ratio, (int, float)) and group_ratio else 1.0

    if quota_type == 1:
        price = entry.get("model_price")
        if not isinstance(price, (int, float)):
            return None, "缺少 model_price"
        return price * ratio * (n or 1), None

    if quota_type == 2:
        per_second = entry.get("video_per_second_ratio")
        if not isinstance(per_second, (int, float)):
            per_second = pick_ratio(entry.get("video_per_second_ratios"), resolution)
        model_ratio = entry.get("model_ratio")
        if not isinstance(per_second, (int, float)):
            return None, "该分辨率没有按秒倍率"
        if not isinstance(model_ratio, (int, float)):
            return None, "缺少 model_ratio"
        return (seconds or 5) * model_ratio * per_second * ratio, None

    return None, "按 token 计费"


# --------------------------------------------------------------------------
# 结果落盘
# --------------------------------------------------------------------------


def ensure_out_dir(out_dir):
    path = os.path.abspath(out_dir or DEFAULT_OUT_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def save_bytes(out_dir, stem, ext, data):
    path = os.path.join(out_dir, "%s%s" % (stem, ext))
    counter = 1
    while os.path.exists(path):
        path = os.path.join(out_dir, "%s-%d%s" % (stem, counter, ext))
        counter += 1
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def download_to(out_dir, stem, url, timeout=DEFAULT_HTTP_TIMEOUT):
    status, headers, raw = http_request("GET", url, timeout=timeout)
    if status != 200:
        raise EasyAPIError("下载结果失败（HTTP %s）：%s" % (status, url))
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1]
    if not ext or len(ext) > 6:
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip()
        ext = mimetypes.guess_extension(ctype) or ".bin"
    return save_bytes(out_dir, stem, ext, raw)


def decode_data_url(value):
    """data:image/png;base64,xxx -> (bytes, ext)，不是 data URL 则返回 None。"""
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    try:
        header, payload = value.split(",", 1)
    except ValueError:
        return None
    ext = ".bin"
    if "/" in header:
        subtype = header.split("/", 1)[1].split(";")[0].strip()
        if subtype:
            ext = "." + ("jpg" if subtype == "jpeg" else subtype)
    try:
        return base64.b64decode(payload), ext
    except (ValueError, TypeError):
        return None


def to_input_reference(value, ref_type=None):
    """把 --ref 的值转成 input_references 元素。本地文件转 data URL。"""
    if re.match(r"^https?://", value) or value.startswith("data:"):
        return {"type": ref_type or "image", "url": value}

    if os.path.exists(value):
        with open(value, "rb") as fh:
            raw = fh.read()
        ctype = mimetypes.guess_type(value)[0] or "image/png"
        encoded = base64.b64encode(raw).decode("ascii")
        return {"type": ref_type or "image", "url": "data:%s;base64,%s" % (ctype, encoded)}

    raise EasyAPIError(
        "参考素材 %r 既不是 URL 也不是存在的文件" % value,
        hint="传 https 链接，或传本地文件路径。",
    )


def timestamp_stem(model):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", model or "result")
    return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), safe)


# --------------------------------------------------------------------------
# 登录与令牌自动配置
# --------------------------------------------------------------------------


def _read_input_line(prompt):
    """交互读一行；非 tty（管道/CI）时从 stdin 读。

    两种模式都支持是为了 `echo -e "user@x.com\\npass" | easyapi.py login` 这类脚本化场景。
    """
    if sys.stdin.isatty():
        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            return input().strip()
        except EOFError:
            return ""
    line = sys.stdin.readline()
    return line.strip() if line else ""


def _read_secret(prompt):
    """读敏感值：tty 下不回显（getpass），管道下直接读行。"""
    if sys.stdin.isatty():
        try:
            import getpass
            return getpass.getpass(prompt).strip()
        except Exception:
            pass  # 某些终端 getpass 不可用，退回普通读行
    line = sys.stdin.readline()
    return line.strip() if line else ""


def _opener_json(opener, method, url, body=None, timeout=DEFAULT_HTTP_TIMEOUT):
    """带 cookie jar 的 JSON 请求（登录序列需要会话 cookie 来走 2FA）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "easyapi-skill/1.0")
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        payload = json.loads(text) if text.strip() else None
    except ValueError:
        payload = None
    return status, payload, text


def _login_error_hint(status, payload, text):
    """登录失败的针对性提示。验证码/密码登录开关/账号锁定都是真实会遇到的。"""
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("msg") or "").strip()
    if not message:
        message = (text or "").strip()[:300] or ("HTTP %s" % status)
    low = message.lower()

    if NOT_A_GATEWAY_MARKER in (text or ""):
        return message, ("这个地址不是 EasyAPI 网关。用 `easyapi.py config` 检查入口地址，"
                         "默认应为 %s。" % DEFAULT_GATEWAY_BASE)
    if "密码登录" in message and "关闭" in message or "password login" in low:
        return message, "平台已关闭密码登录。改用 `--access-token`（粘贴系统访问令牌）。"
    if "turnstile" in low or "captcha" in low or "验证" in message and "人机" in message:
        return message, ("网页人机验证已开启，命令行密码登录无法通过。"
                         "改用 `--browser`（浏览器授权）或 `--access-token`。")
    if "锁定" in message or "locked" in low:
        return message, "账号因多次登录失败被临时锁定，等待解锁后重试。"
    if status == 401 or "密码" in message or "password" in low:
        return message, "账号或密码不正确。注意账号是登录邮箱。"
    return message, None


def _fetch_app_config(base):
    """拉取公开的 app-config。失败返回空 dict（探测类调用不该让登录流程崩掉）。

    注意线上响应把字段包在 data 里（{"success":..,"data":{...}}），必须解包，
    否则验证码开关和 cli_authorize 永远读不到 —— 降级判断会整体走错分支。
    """
    try:
        status, payload, _text = http_json("GET", base + "/app-config", timeout=20)
    except EasyAPIError:
        return {}
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _captcha_enabled(app_config):
    """网页验证码是否开启。开了的话密码登录走不通，提前说比让用户猜 403 强。"""
    return any(bool(app_config.get(key)) for key in CAPTCHA_CONFIG_KEYS)


def _cli_authorize_supported(base, app_config):
    """判断平台是否支持浏览器授权，双层探测。

    第一层看 app-config 的 cli_authorize 标志（权威且零成本）；
    但线上可能出现「端点已部署、标志漏配」的版本错位 —— 2026-09 实测真实发生过，
    所以标志缺失时退第二层：直接打 /cli/exchange。
    路由存在与否用「未知路径必 404 Invalid URL、已知路径进业务校验」的差分判定：
    参数错（400/200+success:false）= 支持，404 = 不支持。
    """
    if app_config.get("cli_authorize"):
        return True
    try:
        status, payload, _text = http_json(
            "POST", base + "/cli/exchange", body={}, timeout=20)
    except EasyAPIError:
        return False
    if status == 404:
        return False
    # 401/400/422/200 + success:false（如「Invalid parameters」）都说明路由存在
    if status == 200:
        return isinstance(payload, dict) and payload.get("success") is not True
    return status in (400, 401, 422)


def _login_fallback_hint(base, app_config):
    """按平台当前能力给出最可行的替代登录方式。

    目的是别把用户引向走不通的路：人机验证开着时密码登录必被拦，
    未部署 CLI 授权端点时浏览器授权必失败，两者要区分对待。
    """
    if _cli_authorize_supported(base, app_config):
        return "改用 `login --browser`（浏览器点一下授权，无需输密码）。"
    if _captcha_enabled(app_config):
        return ("平台开启了人机验证，且网关尚未部署 CLI 授权端点，密码登录与浏览器授权都走不通；"
                "请让用户在自己的终端运行 `login --access-token`"
                "（控制台 /setting 的「安全设置 → 系统访问令牌」页复制令牌和用户 ID），"
                "已有 sk- key 的用户可用 `login --api-key`。")
    return "请让用户在自己的终端运行 `login`（按提示输账号密码），或 `login --access-token`。"


def _password_login(base, reporter, username=None):
    """账号密码登录，返回登录数据（含 access_token 与 id）。

    密码只在本函数内存中出现：不写日志、不写盘、不进 argv。
    access_token 是账号管理凭证，用它建完 key 就丢弃，绝不持久化。
    """
    app_config = _fetch_app_config(base)
    if _captcha_enabled(app_config):
        raise EasyAPIError(
            "平台开启了网页人机验证，命令行密码登录无法通过",
            hint=_login_fallback_hint(base, app_config))

    if not username:
        username = _read_input_line("账号（登录邮箱）: ")
    if not username:
        raise EasyAPIError("未输入账号。")
    password = _read_secret("密码（输入不回显）: ")
    if not password:
        raise EasyAPIError("未输入密码。")

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(path, body):
        return _opener_json(opener, "POST", base + path, body)

    status, payload, text = post("/user/login", {"username": username, "password": password})
    data = (payload or {}).get("data") or {}
    if data.get("require_2fa"):
        reporter.progress("该账号开启了两步验证，需要输入验证码。")
        code = _read_input_line("两步验证码（6 位数字或备用码）: ")
        if not code:
            raise EasyAPIError("需要两步验证码，但未输入。",
                               hint="6 位 TOTP 动态码或备用码之一。")
        status, payload, text = post("/user/login/2fa", {"code": code})
        data = (payload or {}).get("data") or {}

    if status != 200 or not (payload or {}).get("success") or not data.get("access_token"):
        message, hint = _login_error_hint(status, payload, text)
        raise EasyAPIError("登录失败：%s" % message, hint=hint)
    return data


def _access_token_login(base, reporter):
    """用控制台的系统访问令牌登录。适合不愿意在任何 CLI 里输密码的用户。

    令牌在控制台「个人设置 → 安全设置 → 系统访问令牌」可查看；
    用户 ID 同页可见。
    """
    reporter.progress("请到控制台 %s/setting 的「安全设置 → 系统访问令牌」复制访问令牌。" % base)
    token = _read_secret("访问令牌（输入不回显）: ")
    if not token:
        raise EasyAPIError("未输入访问令牌。")
    user_id = _read_input_line("用户 ID（同页可见）: ")
    if not user_id.isdigit():
        raise EasyAPIError("用户 ID 应为数字，收到 %r。" % user_id)
    return {"access_token": token, "id": int(user_id), "username": None}


def dashboard_headers(access_token, user_id):
    """管理接口鉴权头。网关要求两个头同时出现且 user_id 与令牌归属一致。"""
    return {"Authorization": "Bearer %s" % access_token, "EasyAPI-User": str(user_id)}


def _cli_exchange(base, ticket):
    """凭一次性授权码换取登录态（access_token + user_id）。"""
    status, payload, text = http_json("POST", base + "/cli/exchange",
                                      body={"ticket": ticket})
    data = (payload or {}).get("data") or {}
    if status != 200 or not (payload or {}).get("success") or not data.get("access_token"):
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or "").strip()
        if not message:
            message = (text or "").strip()[:300] or ("HTTP %s" % status)
        hint = ("授权码是一次性的且 2 分钟内有效。重新执行 `login --browser`，"
                "或改用 `login` / `login --access-token`。")
        raise EasyAPIError("授权码兑换失败：%s" % message, hint=hint)
    # 后端 /cli/exchange 返回 user_id（与 console_access 同构），统一成登录链路的 id
    if "id" not in data and data.get("user_id") is not None:
        data["id"] = data["user_id"]
    return data


class _CallbackCapture:
    """本地回调监听捕获到的结果（ticket 或错误，二选一先到）。"""

    def __init__(self):
        self.ticket = None
        self.error = None
        self.detail = None


def _start_callback_listener(expected_state):
    """在 127.0.0.1 的随机空闲端口上起一次性回调监听。返回 (server, capture)。"""
    capture = _CallbackCapture()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 静音

        def _respond(self, html):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self._respond(_CALLBACK_FAIL_HTML % "未知路径")
                return
            params = urllib.parse.parse_qs(parsed.query)
            got_error = (params.get("error") or [""])[0]
            got_state = (params.get("state") or [""])[0]
            got_ticket = (params.get("ticket") or [""])[0]
            if got_error:
                capture.error = got_error
                capture.detail = (params.get("error_description") or [""])[0]
                self._respond(_CALLBACK_FAIL_HTML % (got_error or "授权被取消"))
                return
            # state 是 CLI 生成的一次性随机串，用来确认这次回调确实属于本次登录
            if got_state != expected_state:
                capture.error = "state 不匹配"
                capture.detail = "回调可能来自另一次授权，已丢弃。请重试。"
                self._respond(_CALLBACK_FAIL_HTML % "state 校验失败，请回到命令行工具重试")
                return
            capture.ticket = got_ticket
            self._respond(_CALLBACK_OK_HTML)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, capture


def _wait_for_callback(server, capture, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if capture.ticket or capture.error:
            break
        time.sleep(0.2)
    server.shutdown()
    server.server_close()
    if capture.error:
        raise EasyAPIError(
            "浏览器授权未完成：%s" % capture.error,
            hint=(capture.detail or "请重试，或改用 `login` / `login --access-token`。"))
    if not capture.ticket:
        raise EasyAPIError(
            "等待浏览器授权超时（%ss）" % timeout,
            hint=("重试一次，或改用 `login --browser --manual`（手动粘贴授权码）、"
                  "`login`（账号密码）。"))
    return capture.ticket


def _check_authorize_page(console_base):
    """确认授权页真的在那个地址上（前端域和网关域是分开的，配错很常见）。

    只把明确的 404 当作错误：SPA 可能对任意路径都回 200，那就无法判断，
    交给浏览器去打开即可，不误报。
    """
    url = console_base + "/cli/authorize"
    try:
        status, _headers, _raw = http_request("GET", url, timeout=20)
    except EasyAPIError:
        return  # 网络问题不拦，让浏览器去试
    if status == 404:
        raise EasyAPIError(
            "授权页不存在：%s 返回 404" % url,
            hint=("控制台（前端）地址可能配错了。线上前端是 %s、接口是 %s，两者不同域。\n"
                  "用 `easyapi.py config --console-base <你的控制台地址>` 修正。"
                  % (DEFAULT_CONSOLE_BASE, DEFAULT_GATEWAY_BASE)),
        )


def _browser_login(base, reporter, client_name, manual=False, console_base=None):
    """浏览器授权登录：起本地监听 → 开授权页 → 拿一次性授权码 → 换登录态。

    授权页是**前端路由**（控制台域），票据兑换是**网关 API**（网关域）——
    两个域名必须分开用，混用会打到只接受 POST 的 API 路由上。
    manual 模式不自动开浏览器（跨机器 / 无默认浏览器场景），授权码也可手动粘贴 ——
    授权页会把票据同时显示出来，专为这条路径兜底。
    """
    app_config = _fetch_app_config(base)
    if not _cli_authorize_supported(base, app_config):
        raise EasyAPIError("当前平台不支持网页授权", hint=_login_fallback_hint(base, app_config))

    console = (console_base or DEFAULT_CONSOLE_BASE).rstrip("/")
    _check_authorize_page(console)

    state = secrets.token_hex(16)
    server, capture = _start_callback_listener(state)
    port = server.server_address[1]
    redirect_uri = "http://127.0.0.1:%d/callback" % port
    query = urllib.parse.urlencode({
        "redirect_uri": redirect_uri,
        "state": state,
        "client_name": client_name,
    })
    authorize_url = "%s/cli/authorize?%s" % (console, query)

    reporter.progress("授权页地址：%s" % authorize_url)
    if manual:
        reporter.progress("在任意浏览器打开上面的地址并点击授权。")
        reporter.progress("如果浏览器和本脚本不在同一台机器上，自动跳转会失败——"
                          "把授权页显示的「一次性授权码」粘贴到这里即可：")
        pasted = _read_input_line("授权码（留空则等待本机自动跳回）: ")
        if pasted:
            server.shutdown()
            server.server_close()
            return _cli_exchange(base, pasted)
    else:
        reporter.progress("正在打开浏览器…如果没弹出来，手动打开上面的地址。")
        # EASYAPI_NO_BROWSER=1：无头环境/测试用，只打印地址不开浏览器
        if os.environ.get("EASYAPI_NO_BROWSER") != "1":
            webbrowser.open(authorize_url)

    ticket = _wait_for_callback(server, capture, AUTH_CALLBACK_WAIT)
    return _cli_exchange(base, ticket)


def dashboard_request(method, path, headers, body=None, base=None, timeout=DEFAULT_HTTP_TIMEOUT):
    """调用用户侧管理接口。

    注意：网关有些错误返回 HTTP 200 + success:false（如令牌数量达到上限），
    所以必须同时检查 success 字段，只看状态码会漏掉失败。
    """
    status, payload, text = http_json(method, base + path, headers=headers,
                                      body=body, timeout=timeout)
    ok = status < 400 and isinstance(payload, dict) and payload.get("success") is not False
    if ok:
        return payload

    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("msg") or "").strip()
    if not message:
        message = (text or "").strip()[:300] or ("HTTP %s" % status)

    hint = None
    low = message.lower()
    if NOT_A_GATEWAY_MARKER in (text or ""):
        hint = ("这个地址不是 EasyAPI 网关。用 `easyapi.py config` 检查入口地址，"
                "默认应为 %s。" % DEFAULT_GATEWAY_BASE)
    elif status == 401 or "user id" in low or "令牌" in message and "无效" in message:
        hint = "访问令牌无效，或 EasyAPI-User 与令牌不匹配。到控制台 /setting 重新查看。"
    elif "最大令牌数量" in message or "max token" in low:
        hint = "令牌数量已达上限。到控制台 %s/console/token 删掉不用的令牌后重试，" \
               "或用 --key-name 换个名字。" % base
    return_message = "管理接口调用失败（%s）：%s" % (path, message)
    raise EasyAPIError(return_message, hint=hint)


def _list_tokens(base, headers, keyword):
    payload = dashboard_request(
        "GET", "/tokens?" + urllib.parse.urlencode(
            {"keyword": keyword, "p": 1, "page_size": 100}),
        headers, base=base)
    data = (payload or {}).get("data") or {}
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _fetch_token_key(base, headers, token_id):
    """取令牌明文。网关返回的是不带 sk- 前缀的 48 位 raw key，这里补上。"""
    payload = dashboard_request("POST", "/token/%s/key" % token_id, headers, base=base)
    raw = ((payload or {}).get("data") or {}).get("key")
    if not raw:
        raise EasyAPIError("接口没有返回令牌明文（令牌 id=%s）" % token_id)
    return raw if raw.startswith("sk-") else "sk-" + raw


def _describe_token_row(t):
    return "%s（id=%s，状态：%s）" % (
        t.get("name") or "(未命名)", t.get("id"),
        TOKEN_STATUS_LABEL.get(t.get("status"), "未知"))


def provision_api_key(base, access_token, user_id, key_name, reporter,
                      reuse=None, create_new=False):
    """找到或创建可用的 sk- key。

    策略（固定名复用 + 不擅自新建）：
    1. 显式 --reuse-key：按 id 或名字精确匹配已有令牌，取明文；
    2. 已有同名启用令牌：直接复用，不新建（避免反复登录堆积 key）；
    3. 没有同名令牌但账号下已有其它可用令牌：**停下来让 agent 问用户**
       （复用哪个 / 新建专用），绝不静默新建——用户可能已有自己的 key，
       擅自再建一个会在控制台堆积；
    4. 账号下一个可用令牌都没有：自动创建（全新账号零摩擦）。
    同名令牌被禁用时不擅自启用，只如实报告。
    """
    headers = dashboard_headers(access_token, user_id)

    # 1) 显式指定复用
    if reuse:
        all_tokens = _list_tokens(base, headers, "")
        target = None
        want = str(reuse).strip()
        for t in all_tokens:
            if not isinstance(t, dict):
                continue
            if want.isdigit() and str(t.get("id")) == want:
                target = t
                break
            if t.get("name") == want:
                target = t
                break
        if target is None:
            raise EasyAPIError(
                "账号下找不到令牌 %r" % reuse,
                hint="用 `easyapi.py login --access-token` 后按提示查看，"
                     "或到控制台 %s/console/token 确认名字/ID。" % base)
        if target.get("status") != TOKEN_STATUS_ENABLED:
            raise EasyAPIError(
                "指定的令牌 %s" % _describe_token_row(target),
                hint="到控制台 %s/console/token 启用它，或换一个。" % base)
        reporter.progress("按指定复用令牌 %s" % _describe_token_row(target))
        return _fetch_token_key(base, headers, target["id"])

    # 2) 同名复用
    matches = [t for t in _list_tokens(base, headers, key_name)
               if isinstance(t, dict) and t.get("name") == key_name]

    if matches:
        enabled = [t for t in matches if t.get("status") == TOKEN_STATUS_ENABLED]
        if not enabled:
            state = TOKEN_STATUS_LABEL.get(matches[0].get("status"), "不可用")
            raise EasyAPIError(
                "已存在同名令牌「%s」，但当前状态是「%s」" % (key_name, state),
                hint="到控制台 %s/console/token 启用或删除它后重试；"
                     "脚本不会擅自改动你的令牌状态。" % base)
        chosen = enabled[0]
        others = [t for t in enabled if t["id"] != chosen["id"]]
        reporter.progress("复用已有令牌「%s」（id=%s）" % (key_name, chosen["id"]))
        if others:
            reporter.progress("另有 %d 个同名令牌被忽略：%s"
                              % (len(others), ", ".join(str(t["id"]) for t in others)))
        return _fetch_token_key(base, headers, chosen["id"])

    # 3) 没有同名令牌：先看账号下有没有其它可用令牌，有则把选择权交回用户
    if not create_new:
        others = [t for t in _list_tokens(base, headers, "")
                  if isinstance(t, dict) and t.get("status") == TOKEN_STATUS_ENABLED]
        if others:
            listing = "\n".join("  - " + _describe_token_row(t) for t in others[:10])
            raise EasyAPIError(
                "账号下已有 %d 个可用令牌，但没有名为「%s」的专用令牌" % (len(others), key_name),
                hint=(
                    "请让用户选一个，然后二选一重跑 login：\n"
                    "  A) 复用已有令牌：--reuse-key <名字或ID>\n"
                    "  B) 新建专用令牌（推荐，便于单独吊销）：--create-new\n"
                    "当前可用令牌：\n%s" % listing))

    # 4) 全新账号（或用户选择新建）：自动创建
    reporter.progress("正在创建「%s」（不限额度、永不过期）…" % key_name)
    dashboard_request("POST", "/token", headers, base=base, body={
        "name": key_name,
        "unlimited_quota": True,
        "expired_time": -1,
        "model_limits_enabled": False,
    })
    matches = [t for t in _list_tokens(base, headers, key_name)
               if isinstance(t, dict) and t.get("name") == key_name]
    if not matches:
        raise EasyAPIError(
            "令牌已创建但按名字查不到（id 未知）",
            hint="到控制台 %s/console/token 查看名为「%s」的令牌，"
                 "或直接用 `--api-key` 粘贴它的 sk- key。" % (base, key_name))
    return _fetch_token_key(base, headers, matches[0]["id"])


def _account_balance(base, access_token, user_id):
    """用登录态查账号余额（/user/self 是 UserAuth，sk- key 查不到这个）。

    不限量令牌自己的 /usage/token 余额字段是无意义的（恒为不限量），
    用户真正关心的是账号级余额，登录时正好手里有 access_token，顺手查一次。
    """
    try:
        payload = dashboard_request(
            "GET", "/user/self", dashboard_headers(access_token, user_id), base=base)
    except EasyAPIError:
        return None
    data = (payload or {}).get("data") or {}
    quota = data.get("quota")
    return quota if isinstance(quota, (int, float)) else None


def _save_key_and_verify(cfg, reporter, key, account_quota_usd=None):
    """写入配置并立即验证，避免用户以为配好了其实不能用。"""
    cfg["api_key"] = key
    save_config(cfg)
    reporter.progress("已写入 %s（权限 600）" % CONFIG_PATH)
    try:
        payload, human = _quota_report(cfg, key_override=key)
        if account_quota_usd is not None:
            payload["account_balance_usd"] = account_quota_usd
            human.append("账号余额        %s（sk- key 查不到账号余额，登录时顺带查的）"
                         % fmt_usd(account_quota_usd))
        reporter.result(payload, ["API Key 配置完成，验证通过。"] + human)
    except EasyAPIError as exc:
        raise EasyAPIError("key 已保存，但验证失败：%s" % exc.message, hint=exc.hint)


# --------------------------------------------------------------------------
# 子命令：配置与账户
# --------------------------------------------------------------------------


def cmd_login(args, reporter):
    cfg = load_config()
    base, _pricing = resolve_bases(cfg)

    if args.api_key:
        # 兜底路径：用户已经有 sk- key，直接粘贴。不经过账号。
        key = _read_secret("粘贴 EasyAPI API Key（输入不回显）: ")
        if not key:
            raise EasyAPIError("没有读到 API Key。")
        if not key.startswith("sk-"):
            reporter.progress("提示：EasyAPI 的 key 通常以 sk- 开头，收到的不是，仍继续尝试验证。")
        _save_key_and_verify(cfg, reporter, key)
        return 0

    if args.browser:
        login_data = _browser_login(base, reporter, client_name=args.client_name,
                                    manual=args.manual,
                                    console_base=resolve_console_base(cfg))
    elif args.access_token:
        login_data = _access_token_login(base, reporter)
    else:
        login_data = _password_login(base, reporter, username=args.username)

    who = login_data.get("username") or ("用户 %s" % login_data.get("id"))
    reporter.progress("登录成功：%s" % who)

    # access_token 只在这一步使用：建/取 key、查账号余额，用完即丢，不写任何文件
    key = provision_api_key(base, login_data["access_token"], login_data["id"],
                            args.key_name, reporter,
                            reuse=args.reuse_key, create_new=args.create_new)
    account_quota = _account_balance(base, login_data["access_token"], login_data["id"])
    account_usd = usd_from_quota(account_quota) if account_quota is not None else None
    reporter.progress("已取得 API Key（%s…%s），只保存这个 key，登录令牌不落盘。"
                      % (key[:6], key[-4:]))
    _save_key_and_verify(cfg, reporter, key, account_quota_usd=account_usd)
    return 0


def cmd_logout(args, reporter):
    cfg = load_config()
    had = bool(cfg.pop("api_key", None))
    save_config(cfg)
    reporter.result({"ok": True, "removed": had},
                    ["已从 %s 移除 api_key。" % CONFIG_PATH if had else "配置里本来就没有 api_key。"])
    return 0


def cmd_config(args, reporter):
    cfg = load_config()
    changed = []

    def set_key(name, value):
        if value is not None:
            cfg[name] = value
            changed.append(name)

    set_key("base_url", args.base_url)
    set_key("pricing_base", args.pricing_base)
    set_key("console_base", args.console_base)
    set_key("ref_mode", args.ref_mode)
    set_key("default_image_model", args.default_image_model)
    set_key("default_video_model", args.default_video_model)
    set_key("default_text_model", args.default_text_model)
    if args.cache_ttl is not None:
        cfg["cache_ttl"] = int(args.cache_ttl)
        changed.append("cache_ttl")

    if changed:
        save_config(cfg)
        reporter.progress("已更新：%s" % ", ".join(changed))

    if args.clear_cache:
        clear_detail_cache()
        try:
            os.remove(os.path.join(CACHE_DIR, "pricing.json"))
        except OSError:
            pass
        reporter.progress("已清空模型与参数缓存")

    base, pricing = resolve_bases(cfg)
    console = resolve_console_base(cfg)
    key, source = resolve_api_key(cfg, required=False)
    masked = (key[:6] + "..." + key[-4:]) if key and len(key) > 12 else ("已设置" if key else None)

    payload = {
        "config_path": CONFIG_PATH,
        "base_url": base,
        "pricing_base": pricing,
        "console_base": console,
        "api_key_set": bool(key),
        "api_key_source": source,
        "default_image_model": cfg.get("default_image_model"),
        "default_video_model": cfg.get("default_video_model"),
        "default_text_model": cfg.get("default_text_model"),
        "ref_mode": cfg.get("ref_mode") or "auto",
        "cache_ttl": resolve_ttl(cfg),
    }
    human = [
        "配置文件        %s" % CONFIG_PATH,
        "网关入口(API)   %s" % base,
        "定价入口        %s" % ("同网关入口" if pricing == base else pricing),
        "控制台(前端)    %s" % console,
        "API Key         %s" % (masked + "（来自 %s）" % source if masked else "未配置"),
        "参考图模式      %s" % (cfg.get("ref_mode") or "auto（按模型家族自动选）"),
        "默认生图模型    %s" % (cfg.get("default_image_model") or "未设置"),
        "默认生视频模型  %s" % (cfg.get("default_video_model") or "未设置"),
        "缓存有效期      %ss" % resolve_ttl(cfg),
    ]
    reporter.result(payload, human)
    return 0


def _quota_report(cfg, key_override=None):
    key = key_override or resolve_api_key(cfg)[0]
    base, _pricing = resolve_bases(cfg)
    status, payload, text = http_json(
        "GET", base + "/usage/token", headers={"Authorization": "Bearer " + key}
    )
    if status != 200 or not isinstance(payload, dict) or not payload.get("data"):
        message, hint = translate_error(status, payload, text)
        raise EasyAPIError("查询额度失败：%s" % message, hint=hint)

    data = payload["data"]
    granted = data.get("total_granted")
    used = data.get("total_used")
    available = data.get("total_available")
    unlimited = bool(data.get("unlimited_quota"))

    # -1 = 永不过期。网关正常会把它归一成 0，但别指望——time.localtime(-1)
    # 在 Windows 上直接抛 OSError，所以要自己兜住
    expires_at = data.get("expires_at")
    never_expires = isinstance(expires_at, (int, float)) and expires_at < 0
    if never_expires or not expires_at:
        expires_at = None

    result = {
        "ok": True,
        "name": data.get("name"),
        "unlimited_quota": unlimited,
        "total_granted_usd": usd_from_quota(granted),
        "total_used_usd": usd_from_quota(used),
        "total_available_usd": usd_from_quota(available),
        "never_expires": never_expires,
        "expires_at": expires_at,
        "model_limits_enabled": bool(data.get("model_limits_enabled")),
        "model_limits": data.get("model_limits") or {},
    }

    human = [
        "令牌名称        %s" % (data.get("name") or "—"),
        "总额度          %s" % ("不限量" if unlimited else fmt_usd(result["total_granted_usd"])),
        "已用            %s" % fmt_usd(result["total_used_usd"]),
        "剩余            %s" % ("不限量" if unlimited else fmt_usd(result["total_available_usd"])),
    ]
    if unlimited:
        # 不限量令牌的 granted/available 字段是无意义的，用户真正想知道的是账号还剩多少钱；
        # 但 /usage/token 只给令牌视角，账号余额需要登录态，这里必须说清楚，别让人误读
        human.append("说明            该令牌不限量，实际消耗从账号余额扣除；"
                     "账号余额请控制台 %s 查看（sk- key 查不到账号级余额）" % DEFAULT_CONSOLE_BASE)
    if result["never_expires"]:
        human.append("过期时间        永不过期")
    elif result["expires_at"]:
        human.append("过期时间        %s" % time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(result["expires_at"])))
    if result["model_limits_enabled"]:
        names = ", ".join(sorted(result["model_limits"])) or "（空）"
        human.append("模型白名单      %s" % names)
    return result, human


def cmd_quota(args, reporter):
    payload, human = _quota_report(load_config())
    reporter.result(payload, human)
    return 0


def cmd_env(args, reporter):
    """环境自检：解释器、版本、目录、快照与配置，--network 时探测网关能力。

    换机器、换工具、换安装方式（npx skills / 手动复制）后先跑这一条，
    能把「python 解释器不对」「skill 目录没解析对」「老部署没有 schema 接口」
    这三类问题一次定位，而不是等到生成时才报一个含糊的错误。
    """
    snap = load_snapshot()
    meta = snap.get("_meta") or {}
    cfg = load_config()
    info = {
        "ok": True,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "skill_dir": SKILL_DIR,
        "config_dir": CONFIG_DIR,
        "snapshot_date": meta.get("generated_at"),
        "snapshot_models": len(snap.get("models") or {}),
        "gateway_base": cfg.get("base_url") or DEFAULT_GATEWAY_BASE,
        "pricing_base": cfg.get("pricing_base") or cfg.get("base_url") or DEFAULT_PRICING_BASE,
        "key_configured": bool(cfg.get("api_key")),
    }

    if args.network:
        try:
            payload, _source = fetch_pricing(cfg, args.refresh)
            info["pricing_ok"] = True
            info["pricing_models"] = len(payload.get("data") or [])
            info["pricing_version"] = payload.get("pricing_version")
        except EasyAPIError as exc:
            info["ok"] = False
            info["pricing_ok"] = False
            info["pricing_error"] = exc.message

        try:
            _base, pricing_base = resolve_bases(cfg)
            status, _payload, _text = http_json("GET", pricing_base + "/pricing/schema")
            # 新部署缺参数返回 400，老部署没这个路由返回 404 —— 用状态码区分能力
            info["schema_endpoint"] = "supported" if status in (200, 400) else "unsupported"
        except EasyAPIError:
            info["schema_endpoint"] = "unknown"

    human = [
        "解释器      %s（Python %s）" % (info["python"], info["python_version"]),
        "Skill 目录  %s" % info["skill_dir"],
        "配置目录    %s" % info["config_dir"],
        "网关入口    %s" % info["gateway_base"],
        "定价入口    %s" % info["pricing_base"],
        "API Key     %s" % ("已配置" if info["key_configured"] else "未配置（配置后可用生图/生视频）"),
        "内置快照    %s（%s 个模型）"
        % (info["snapshot_date"] or "日期未知", info["snapshot_models"]),
    ]
    if args.network:
        if info.get("pricing_ok"):
            human.append("线上定价    正常（%s 个模型，pricing_version=%s）"
                         % (info["pricing_models"], info.get("pricing_version") or "—"))
        else:
            human.append("线上定价    失败：%s" % info.get("pricing_error"))
        human.append("schema 接口 %s" % {
            "supported": "可用（/pricing/schema）",
            "unsupported": "不可用，将回退 /pricing/detail 与内置快照",
        }.get(info.get("schema_endpoint"), "未知"))
    reporter.result(info, human)
    return 0


# --------------------------------------------------------------------------
# 子命令：模型与参数查询
# --------------------------------------------------------------------------


def cmd_models(args, reporter):
    cfg = load_config()
    if args.category:
        rows, payload, source = models_by_category(cfg, args.category, args.refresh)
        group_ratio = ((payload.get("group_ratio") or {}).get("default") or 1)
    else:
        payload, source = fetch_pricing(cfg, args.refresh)
        rows = payload.get("data") or []
        group_ratio = ((payload.get("group_ratio") or {}).get("default") or 1)

    if args.search:
        needle = args.search.lower()
        rows = [m for m in rows if needle in (m.get("model_name") or "").lower()]

    vendors = {v.get("id"): v.get("name") for v in (payload.get("vendors") or [])}
    rows = sorted(rows, key=lambda m: (m.get("category") or "", m.get("model_name") or ""))

    items = []
    for m in rows:
        price, reason = estimate_usd(m, group_ratio, seconds=args.seconds,
                                     resolution=args.resolution)
        items.append({
            "model_name": m.get("model_name"),
            "category": m.get("category"),
            "vendor": vendors.get(m.get("vendor_id")),
            "billing": billing_label(m),
            "quota_type": m.get("quota_type"),
            "model_price_usd": m.get("model_price"),
            "model_ratio": m.get("model_ratio"),
            "estimated_usd": price,
            "estimate_unavailable_reason": reason,
            "description": m.get("description"),
        })

    if args.json:
        reporter.result(
            {"ok": True, "source": source, "count": len(items),
             "pricing_version": payload.get("pricing_version"), "models": items},
            [],
        )
        return 0

    human = []
    if source == "cache":
        human.append("（来自本地缓存，--refresh 可强制刷新）")
    human.append("%-28s %-8s %-6s %-10s %s" % ("模型", "分类", "计费", "预估单价", "厂商"))
    human.append("-" * 78)
    for item in items:
        human.append("%-28s %-8s %-6s %-10s %s" % (
            item["model_name"], item["category"] or "—", item["billing"],
            fmt_usd(item["estimated_usd"]), item["vendor"] or "—",
        ))
    human.append("")
    human.append("共 %d 个模型。预估单价基于 --seconds/--resolution 假设，按量模型无法本地估算。" % len(items))
    reporter.result({"ok": True, "source": source, "count": len(items), "models": items}, human)
    return 0


def _describe_param(name, spec):
    if not isinstance(spec, dict):
        return "", "", "", ""
    ptype = spec.get("type") or "?"
    required = "是" if spec.get("required") is True else "否"
    default = spec.get("default")
    default_text = "—" if default is None else json.dumps(default, ensure_ascii=False)

    bits = []
    if spec.get("enum"):
        bits.append(" / ".join(str(v) for v in spec["enum"]))
    elif ptype in ("integer", "number") and ("min" in spec or "max" in spec):
        bits.append("%s ~ %s" % (spec.get("min", "-∞"), spec.get("max", "∞")))
    elif ptype == "string" and spec.get("max_length"):
        bits.append("最长 %s 字符" % spec["max_length"])
    if spec.get("description"):
        bits.append(spec["description"])
    return ptype, required, default_text, "；".join(bits)


def build_example(props, model):
    """按必填项生成一个可发送的示例请求体（服务端没给 example 时用）。

    prompt 常常是「条件必填」（有参考图时可省），所以即使没标 required，
    只要声明了 prompt 就补上，示例才不会只剩一个 model 字段。
    """
    example = {"model": model}
    for name in sorted(props or {}):
        spec = props.get(name)
        if not isinstance(spec, dict):
            continue
        if spec.get("required") is not True and name != "prompt":
            continue
        if spec.get("enum"):
            example[name] = spec["enum"][0]
        elif "default" in spec:
            example[name] = spec["default"]
        elif name == "prompt":
            example[name] = "一只猫在草地上奔跑"
        elif spec.get("type") == "integer":
            example[name] = int(spec.get("min", 1) or 1)
        elif spec.get("type") == "number":
            example[name] = spec.get("min", 1) or 1
        elif spec.get("type") == "boolean":
            example[name] = False
        elif spec.get("type") == "array":
            example[name] = []
        elif spec.get("type") == "object":
            example[name] = {}
        else:
            example[name] = "sample"
    return example


def cmd_schema(args, reporter):
    cfg = load_config()
    category = resolve_category(cfg, args.model, args.category, args.refresh)
    props, meta, notes = resolve_schema(cfg, args.model, category, args.refresh)

    entry = None
    try:
        entry, _src = find_model(cfg, args.model, None, args.refresh)
    except EasyAPIError:
        entry = None

    endpoints = list(meta.get("endpoints") or [])
    submit_path, endpoints = resolve_submit_endpoint(
        cfg, args.model, category, meta, args.refresh)

    required = sorted(
        name for name, spec in (props or {}).items()
        if isinstance(spec, dict) and spec.get("required") is True
    )
    example = meta.get("example") or build_example(props, args.model)
    billing = meta.get("billing")
    if not billing and entry:
        billing = billing_label(entry)

    sources = list(meta.get("sources") or [])
    source_label = {
        "schema-bundle": "线上 /pricing/schema（全量参数包）",
        "schema": "线上 /pricing/schema（单模型）",
        "live-detail": "线上 /pricing/detail",
        "snapshot": "内置快照（%s）" % (meta.get("snapshot_date") or "日期未知"),
    }.get(meta.get("source"), "无")
    if meta.get("source") and meta.get("source") != "snapshot" and "snapshot" in sources:
        source_label += " + 内置快照补全缺失字段"

    payload = {
        "ok": bool(props),
        "model": args.model,
        "category": category,
        "schema_source": meta.get("source"),
        "schema_sources": sources,
        "schema_source_label": source_label,
        "snapshot_date": meta.get("snapshot_date"),
        "endpoints": endpoints,
        "submit_endpoint": {"method": "POST", "path": submit_path},
        "required": required,
        "example": example,
        "billing": billing,
        "params": props,
        "notes": notes,
    }

    if args.json:
        reporter.result(payload, [])
        return 0

    human = ["模型 %s（%s）" % (args.model, category), "参数来源：%s" % source_label]
    if billing:
        human.append("计费方式：%s" % billing)
    if endpoints:
        human.append("提交端点：POST %s" % submit_path)
        others = ["%s %s" % ((e or {}).get("method") or "POST", (e or {}).get("path"))
                  for e in endpoints
                  if (e or {}).get("path") and (e or {}).get("path") != submit_path]
        if others:
            human.append("渠道其它端点：%s" % "、".join(others))
    if required:
        human.append("必填参数：%s" % "、".join(required))
    human.append("")
    if not props:
        human.append("没有可用的参数定义，无法预校验。")
        human.extend(notes)
        reporter.result(payload, human)
        return 0

    # 参数名可能很长（如 sequential_image_generation），列宽按实际内容算
    name_width = max([len("参数")] + [len(n) for n in props])
    human.append("%-*s %-9s %-5s %-8s %s" % (name_width, "参数", "类型", "必填", "默认", "取值 / 说明"))
    human.append("-" * (name_width + 40))
    for name in sorted(props):
        ptype, is_required, default_text, detail = _describe_param(name, props[name])
        human.append("%-*s %-9s %-5s %-8s %s"
                     % (name_width, name, ptype, is_required, default_text, detail))
    human.append("")
    human.append("共 %d 个参数。" % len(props))
    human.append("")
    human.append("示例请求体：")
    human.append(json.dumps(example, ensure_ascii=False, indent=2))
    if notes:
        human.append("")
        human.extend("提示：" + n for n in notes)
    reporter.result(payload, human)
    return 0


def cmd_compare(args, reporter):
    cfg = load_config()
    payload, source = fetch_pricing(cfg, args.refresh)
    group_ratio = ((payload.get("group_ratio") or {}).get("default") or 1)
    all_models = {m.get("model_name"): m for m in (payload.get("data") or [])}

    if args.models:
        selected = [all_models.get(name) for name in args.models]
        missing = [name for name, m in zip(args.models, selected) if m is None]
        if missing:
            raise EasyAPIError(
                "以下模型在当前分组不可见：%s" % "、".join(missing),
                hint="用 `easyapi.py models --category %s` 看可用模型。"
                     % (args.category or "image"),
            )
    else:
        category = args.category or "image"
        selected = [m for m in all_models.values()
                    if (m.get("category") or "").lower() == category.lower()]

    rows = []
    for m in selected:
        if not m:
            continue
        price, reason = estimate_usd(m, group_ratio, seconds=args.seconds,
                                     resolution=args.resolution)
        discount = m.get("model_discount")
        rows.append({
            "model_name": m.get("model_name"),
            "category": m.get("category"),
            "billing": billing_label(m),
            "estimated_usd": price,
            "estimate_unavailable_reason": reason,
            "model_price_usd": m.get("model_price"),
            "model_ratio": m.get("model_ratio"),
            "model_discount": discount,
            "video_per_second_ratios": m.get("video_per_second_ratios"),
            "description": m.get("description"),
        })

    # 有价的排前面按价升序，无价的按原因分组排在后面
    priced = sorted([r for r in rows if r["estimated_usd"] is not None],
                    key=lambda r: r["estimated_usd"])
    unpriced = sorted([r for r in rows if r["estimated_usd"] is None],
                      key=lambda r: (r["model_ratio"] or 0))

    if args.json:
        reporter.result({"ok": True, "source": source, "assumptions": {
            "seconds": args.seconds, "resolution": args.resolution,
            "group_ratio": group_ratio,
        }, "models": priced + unpriced}, [])
        return 0

    human = []
    if args.category and not args.models:
        human.append("分类：%s" % args.category)
    human.append("假设：时长 %ss，分辨率 %s，分组倍率 %s"
                 % (args.seconds, args.resolution, group_ratio))
    human.append("注意：不含 model_discount，实际账单以平台扣费为准。")
    human.append("")
    human.append("%-28s %-6s %-10s %s" % ("模型", "计费", "预估单价", "说明"))
    human.append("-" * 88)
    for r in priced:
        note = ""
        if isinstance(r["model_discount"], (int, float)) and r["model_discount"] != 1:
            note = "折扣 %s（未计入）" % r["model_discount"]
        human.append("%-28s %-6s %-10s %s" % (
            r["model_name"], r["billing"], fmt_usd(r["estimated_usd"]), note))

    if unpriced:
        by_reason = {}
        for r in unpriced:
            by_reason.setdefault(r["estimate_unavailable_reason"], []).append(r)
        human.append("")
        human.append("以下模型无法本地估算：")
        for reason, group in sorted(by_reason.items()):
            human.append("  【%s】" % reason)
            for r in group:
                extra = ""
                if reason == "该分辨率没有按秒倍率":
                    keys = sorted((r.get("video_per_second_ratios") or {}).keys())
                    if keys:
                        extra = "  可选分辨率：%s" % " / ".join(keys)
                elif reason == "按 token 计费":
                    extra = "  ratio %s（1 倍率 = $2/百万 token）" % r["model_ratio"]
                human.append("    %-26s %s%s" % (r["model_name"], r["billing"], extra))
        if any(r["estimate_unavailable_reason"] == "该分辨率没有按秒倍率" for r in unpriced):
            human.append("")
            human.append("提示：用 --resolution 指定该模型支持的分辨率即可算出价格。")
    reporter.result({"ok": True, "models": priced + unpriced}, human)
    return 0


# --------------------------------------------------------------------------
# 参考图（图生图）投递方式
# --------------------------------------------------------------------------

# 网关按 URL 路径决定 relay 模式（/v1/images/edits vs /v1/images/generations），
# 各渠道 adaptor 再转成上游格式，所以「走哪个端点 + 用哪个字段」是按模型家族分的，
# 不存在统一写法。
#
# ⚠️ 关键事实：在 /v1/images/generations 的 JSON body 里传 `images` 数组**是无效的** ——
# 网关会原样转发，但上游不解释这个字段；部分渠道还会静默按文生图出图，
# 用户拿到一张没参考到参考图的图却以为成功了。
REF_MODE_EDIT = "edits"    # multipart POST /v1/images/edits，文件字段 image（多图重复 image[]）
REF_MODE_IMAGE = "image"   # JSON POST /v1/images/generations，字段 image（URL 或 data URL）
REF_MODE_NONE = "unsupported"

REF_MODE_LABEL = {
    REF_MODE_EDIT: "multipart /v1/images/edits（上传参考图文件）",
    REF_MODE_IMAGE: "JSON /v1/images/generations 的 image 字段",
    REF_MODE_NONE: "不支持",
}

# 家族 → 默认投递方式。改这里之前先读 references/image.md 的「通道差异」一节。
# 默认值来自 2026-09-29 对生产环境的真实调用实测（见 image.md 的实测记录）。
REF_MODE_BY_FAMILY = (
    ("gpt-image", REF_MODE_EDIT),
    ("chatgpt-image", REF_MODE_EDIT),
    ("dall-e", REF_MODE_EDIT),
    ("qwen-image", REF_MODE_EDIT),   # 实测 multipart edits 可用，参考图生效
    ("wan2", REF_MODE_EDIT),
    # seedream 实测：multipart edits 返回 404 openai_error（当前通道不接通），
    # JSON generations 的 image 字段可用且参考图确实生效。
    ("seedream", REF_MODE_IMAGE),
)
REF_MODE_UNSUPPORTED = ("gemini", "nano-banana")

REF_MODE_SWITCH_HINT = (
    "不同上游通道支持参考图的方式不同，可换一条路重试："
    "`--ref-mode edits`（multipart 上传文件）或 `--ref-mode image`（JSON 的 image 字段）。")

REF_MAX_BYTES = 10 * 1024 * 1024   # 上游普遍限制 10MB 左右


def resolve_ref_mode(model, override, cfg):
    """决定参考图投递方式。优先级：--ref-mode > config ref_mode > 家族默认。"""
    name = (model or "").strip().lower()
    for prefix in REF_MODE_UNSUPPORTED:
        if name.startswith(prefix):
            return REF_MODE_NONE
    chosen = (override or cfg.get("ref_mode") or "").strip().lower()
    if chosen in (REF_MODE_EDIT, REF_MODE_IMAGE):
        return chosen
    for prefix, mode in REF_MODE_BY_FAMILY:
        if name.startswith(prefix):
            return mode
    return REF_MODE_EDIT  # 未知家族：multipart 是网关里实现最广的一条路


def _download_ref(url, reporter, timeout=DEFAULT_HTTP_TIMEOUT):
    """下载远程参考图，返回 (文件名, 字节)。multipart 只能传字节，URL 必须先取回来。"""
    status, _headers, raw = http_request("GET", url, timeout=timeout)
    if status != 200 or not raw:
        raise EasyAPIError("下载参考图失败（HTTP %s）：%s" % (status, url))
    if len(raw) > REF_MAX_BYTES:
        reporter.progress("提示：参考图 %.1fMB 偏大，上游可能拒绝（普遍限制 10MB）"
                          % (len(raw) / 1048576.0))
    name = os.path.basename(urllib.parse.urlparse(url).path) or "reference.png"
    return name, raw


def load_ref_material(value, mode, reporter, timeout=DEFAULT_HTTP_TIMEOUT):
    """按投递方式准备一份参考图素材。

    edits 模式只能传字节（URL 先下载）；image 模式接受 URL 或 data URL
    （上游契约是「URL 或 Base64」）。
    返回 ("file", (文件名, 字节)) 或 ("value", 字符串)。
    """
    raw_value = (value or "").strip()
    if not raw_value:
        raise EasyAPIError("参考图为空。")

    if re.match(r"^https?://", raw_value):
        if mode == REF_MODE_IMAGE:
            return "value", raw_value
        name, data = _download_ref(raw_value, reporter, timeout)
        return "file", (name, data)

    if raw_value.startswith("data:"):
        decoded = decode_data_url(raw_value)
        if decoded is None:
            raise EasyAPIError("参考图的 data URL 解析失败。")
        raw, ext = decoded
        if mode == REF_MODE_IMAGE:
            return "value", raw_value
        return "file", ("reference" + ext, raw)

    if os.path.exists(raw_value):
        with open(raw_value, "rb") as fh:
            data = fh.read()
        if not data:
            raise EasyAPIError("参考图文件是空的：%s" % raw_value)
        if len(data) > REF_MAX_BYTES:
            reporter.progress("提示：参考图 %.1fMB 偏大，上游可能拒绝（普遍限制 10MB）"
                              % (len(data) / 1048576.0))
        if mode == REF_MODE_IMAGE:
            mime = mimetypes.guess_type(raw_value)[0] or "image/png"
            return "value", "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        return "file", (os.path.basename(raw_value), data)

    raise EasyAPIError(
        "参考图 %r 既不是 http(s) 链接、data URL，也不是存在的文件" % value,
        hint="传公网图片链接或本地文件路径；本地路径请确认拼写与当前工作目录。")


# --------------------------------------------------------------------------
# 子命令：生图
# --------------------------------------------------------------------------


def _resolve_model(cfg, explicit, category, reporter, refresh=False):
    """模型解析：--model > 配置默认 > 报错并列出候选（绝不猜）。"""
    if explicit:
        return explicit

    configured = default_model(cfg, category)
    if configured:
        reporter.progress("未指定 --model，使用配置的默认模型 %s" % configured)
        return configured

    rows, _payload, _src = models_by_category(cfg, category, refresh)
    names = [m.get("model_name") for m in rows][:20]
    raise EasyAPIError(
        "没有指定模型，配置里也没有 default_%s_model" % category,
        hint=(
            "请让用户选一个模型，然后二选一：\n"
            "  1) 本次指定：--model <模型名>\n"
            "  2) 设为默认：easyapi.py config --default-%s-model <模型名>\n"
            "当前 %s 分类可用模型（前 20 个）：\n  %s"
            % (category, category, "\n  ".join(names) or "（无）")
        ),
    )


def cmd_image(args, reporter):
    cfg = load_config()
    base, _pricing = resolve_bases(cfg)

    model = _resolve_model(cfg, args.model, "image", reporter, args.refresh)
    props, meta, notes = resolve_schema(cfg, model, "image", args.refresh)

    refs = [normalize_size_like(r) for r in (args.ref or [])]
    ref_mode = resolve_ref_mode(model, args.ref_mode, cfg) if refs else None
    if ref_mode == REF_MODE_NONE:
        raise EasyAPIError(
            "%s 不支持通过 images 接口传参考图" % model,
            hint="gemini 系图像模型的参考图要走多模态对话（chat --image），"
                 "不是 images 接口；或改用 seedream / qwen-image / gpt-image 系列。")

    values = {"model": model, "prompt": args.prompt}
    for name in ("size", "quality", "background", "moderation", "output_format",
                 "output_compression", "partial_images", "style", "user", "input_fidelity"):
        val = getattr(args, name, None)
        if val is not None:
            values[name] = val
    if args.n is not None:
        values["n"] = args.n
    if args.stream:
        values["stream"] = True
    if args.watermark is not None:
        values["watermark"] = args.watermark
    values.update(parse_param_flags(args.param))

    # 参考图按上游契约注入：edits 走 multipart 文件部件，image 走 JSON image 字段。
    # 这两种字段形态是脚本按渠道 adaptor 行为决定的，不在模型声明里，标记为已知避免误报。
    ref_files = []     # [(文件名, 字节)]，edits 模式
    ref_values = []    # [字符串]，image 模式
    for ref in refs:
        kind, payload = load_ref_material(ref, ref_mode, reporter)
        if kind == "file":
            ref_files.append(payload)
        else:
            ref_values.append(payload)
    if ref_mode == REF_MODE_IMAGE:
        values["image"] = ref_values[0] if len(ref_values) == 1 else ref_values

    known_extra = {"image"} if ref_mode == REF_MODE_IMAGE else set()
    clean, vnotes, warnings = validate_params(props, values, strict=args.strict,
                                              known_extra=known_extra)
    notes.extend(vnotes)

    # 只填 schema 显式声明的默认值（如 n），不替用户猜尺寸
    clean = fill_declared_defaults(props, clean)

    if ref_mode == REF_MODE_EDIT:
        endpoint = "/v1/images/edits"
    else:
        endpoint = "/v1/images/generations"
        resolved, _endpoints = resolve_submit_endpoint(
            cfg, model, "image", meta, args.refresh)
        # 只接受生成类路径：带 edit 的端点留给参考图模式，别把无参考图的请求发过去
        if resolved and "edit" not in resolved.lower():
            endpoint = resolved
    timeout = resolve_timeout(args)

    def summarize_shape():
        shape = dict(clean)
        if ref_files:
            shape["<files>"] = ["%s（%d 字节）" % (n, len(d)) for n, d in ref_files]
        return shape

    if args.dry_run:
        detail = [
            "DRY RUN — 未发送请求，无需 API Key。",
            "地址  %s" % (base + endpoint),
        ]
        if ref_mode:
            detail.append("参考图投递  %s" % REF_MODE_LABEL[ref_mode])
        detail += ["请求体：", json.dumps(summarize_shape(), ensure_ascii=False, indent=2)]
        detail += (["", "提示："] + ["  - " + n for n in notes + warnings]
                   if notes or warnings else [])
        reporter.result({
            "ok": True, "dry_run": True, "command": "image", "model": model,
            "endpoint": endpoint, "url": base + endpoint, "ref_mode": ref_mode,
            "ref_files": [n for n, _d in ref_files],
            "schema_source": meta.get("source"),
            "request": summarize_shape(), "notes": notes, "warnings": warnings,
        }, detail)
        return 0

    key, _src = resolve_api_key(cfg)
    reporter.progress("提交生图请求：%s（%s%s）"
                      % (model, meta.get("source") or "no-schema",
                         "，参考图走 %s" % ref_mode if ref_mode else ""))
    auth = {"Authorization": "Bearer " + key}
    if ref_mode == REF_MODE_EDIT:
        # 单图用 image，多图重复 image[]（三家 adaptor 的共用约定）
        if len(ref_files) == 1:
            file_parts = [("image", ref_files[0][0], ref_files[0][1])]
        else:
            file_parts = [("image[]", name, data) for name, data in ref_files]
        status, payload, text = http_request_multipart_json(
            "POST", base + endpoint, clean, file_parts, headers=auth, timeout=timeout)
    else:
        status, payload, text = http_json(
            "POST", base + endpoint, headers=auth, body=clean, timeout=timeout)
    if status != 200:
        message, hint = translate_error(status, payload, text, model)
        if refs:
            hint = ((hint + "\n" if hint else "")
                    + "这是一次带参考图的请求；" + REF_MODE_SWITCH_HINT)
        raise EasyAPIError("生图失败：%s" % message, hint=hint)

    items = (payload or {}).get("data") or []
    if not items:
        raise EasyAPIError(
            "接口返回成功但没有图片数据",
            detail=json.dumps(payload, ensure_ascii=False)[:600],
        )

    out_dir = ensure_out_dir(args.out)
    stem = timestamp_stem(model)
    files = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        entry = {"index": idx, "url": item.get("url"), "revised_prompt": item.get("revised_prompt")}
        decoded = decode_data_url(item.get("b64_json"))
        if decoded is None and item.get("b64_json"):
            try:
                decoded = (base64.b64decode(item["b64_json"]), ".png")
            except (ValueError, TypeError):
                decoded = None

        try:
            if decoded is not None:
                raw, ext = decoded
                path = save_bytes(out_dir, "%s-%d" % (stem, idx), ext, raw)
                entry["path"] = path
                entry["bytes"] = len(raw)
            elif item.get("url"):
                path = download_to(out_dir, "%s-%d" % (stem, idx), item["url"], timeout)
                entry["path"] = path
                entry["bytes"] = os.path.getsize(path)
        except EasyAPIError as exc:
            entry["download_error"] = exc.message
        files.append(entry)

    if refs:
        # 上游对参考图的支持依赖通道，客户端无法从响应里判断参考图是否真被采用，
        # 所以如实说明走了哪条路 + 换路的办法，让用户能自己核对结果。
        notes.append("参考图已按 %s 发送；若结果看起来没参考到参考图，"
                     "用 --ref-mode %s 换另一条路重试。"
                     % (REF_MODE_LABEL[ref_mode],
                        REF_MODE_IMAGE if ref_mode == REF_MODE_EDIT else REF_MODE_EDIT))

    result = {
        "ok": True, "command": "image", "model": model, "endpoint": endpoint,
        "ref_mode": ref_mode, "count": len(files), "files": files,
        "notes": notes, "warnings": warnings,
        "usage": (payload or {}).get("usage"),
        "metadata": (payload or {}).get("metadata"),
    }

    human = ["生图完成：%s，共 %d 张" % (model, len(files))]
    if ref_mode:
        human.append("  参考图    %s" % REF_MODE_LABEL[ref_mode])
    for f in files:
        if f.get("path"):
            human.append("  文件  %s" % f["path"])
        if f.get("url"):
            human.append("  链接  %s" % f["url"])
        if f.get("download_error"):
            human.append("  下载失败  %s" % f["download_error"])
    if notes or warnings:
        human.append("")
        human.extend(["提示："] + ["  - " + n for n in notes + warnings])
    reporter.result(result, human)
    return 0


# --------------------------------------------------------------------------
# 子命令：生视频
# --------------------------------------------------------------------------

VIDEO_DONE = {"completed", "success", "succeeded", "done"}
VIDEO_FAIL = {"failed", "failure", "error", "cancelled", "canceled"}


def extract_task_id(payload):
    """提交响应在不同端点上字段名不同，全部兜住。"""
    if not isinstance(payload, dict):
        return None
    for key in ("id", "task_id", "job_id"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for nest in ("data", "result", "output"):
        inner = payload.get(nest)
        if isinstance(inner, dict):
            found = extract_task_id(inner)
            if found:
                return found
        elif isinstance(inner, str) and inner.strip() and nest == "data":
            return inner.strip()
    return None


def poll_video_task(cfg, key, base, task_id, reporter, timeout, interval,
                    endpoint="/v1/videos"):
    deadline = time.time() + timeout
    wait = interval
    last_status = None
    # 查询路径跟随提交路径：/v1/videos 与 /v1/video/generations 都是「路径 + 任务 ID」
    poll_url = "%s%s/%s" % (base, endpoint.rstrip("/"), urllib.parse.quote(task_id))

    while True:
        status, payload, text = http_json(
            "GET", poll_url,
            headers={"Authorization": "Bearer " + key},
        )
        if status != 200:
            message, hint = translate_error(status, payload, text)
            raise EasyAPIError("查询任务状态失败：%s" % message, hint=hint)

        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}

        # GateAI 风格失败时 code != 200 且 data 为 null
        code = (payload or {}).get("code")
        if isinstance(code, int) and code != 200 and not data:
            raise EasyAPIError("任务查询返回错误：%s" % ((payload or {}).get("msg") or code))

        state = str(data.get("status") or "").strip().lower()
        if state != last_status:
            reporter.progress("任务状态：%s" % (state or "未知"))
            last_status = state

        if state in VIDEO_DONE:
            return data
        if state in VIDEO_FAIL:
            raise EasyAPIError(
                "视频生成失败：%s" % (data.get("message") or "上游未给出原因"),
                hint="可换模型或调整提示词后重试。",
            )

        if time.time() >= deadline:
            raise EasyAPIError(
                "等待视频生成超时（%ss），任务可能仍在进行" % timeout,
                hint=(
                    "任务 ID：%s\n"
                    "稍后可手动查询：GET %s\n"
                    "或加大等待时间：--timeout %d"
                    % (task_id, poll_url, timeout * 2)
                ),
            )

        time.sleep(min(wait, max(1, deadline - time.time())))
        wait = min(wait * 1.3, 10)


def cmd_video(args, reporter):
    cfg = load_config()
    base, _pricing = resolve_bases(cfg)

    model = _resolve_model(cfg, args.model, "video", reporter, args.refresh)
    props, meta, notes = resolve_schema(cfg, model, "video", args.refresh)

    values = {"model": model}
    if args.prompt:
        values["prompt"] = args.prompt
    for name in ("size", "resolution", "aspect_ratio", "duration",
                 "generate_audio", "seed", "negative_prompt", "mode"):
        val = getattr(args, name, None)
        if val is not None:
            values[name] = val
    if args.fps is not None:
        values["fps"] = args.fps
    if args.image:
        values["image"] = normalize_size_like(args.image)
    if args.images:
        values["images"] = [normalize_size_like(v) for v in args.images]

    # 首尾帧/参考素材一律走 input_references（顶层 first_frame_* 会被上游丢弃）
    refs = []
    if args.first_frame:
        refs.append(to_input_reference(args.first_frame, "image"))
        refs[-1]["role"] = "first_frame"
    if args.last_frame:
        if not args.first_frame:
            raise EasyAPIError(
                "--last-frame 必须和 --first-frame 一起用",
                hint="尾帧不能单独出现，请同时给出首帧。",
            )
        refs.append(to_input_reference(args.last_frame, "image"))
        refs[-1]["role"] = "last_frame"
    for value in (args.ref_image or []):
        item = to_input_reference(value, "image")
        item["role"] = "reference_image"
        refs.append(item)
    for value in (args.ref_video or []):
        item = to_input_reference(value, "video")
        item["role"] = "reference_video"
        refs.append(item)
    for value in (args.ref_audio or []):
        item = to_input_reference(value, "audio")
        item["role"] = "reference_audio"
        refs.append(item)
    if refs:
        values["input_references"] = refs

    values.update(parse_param_flags(args.param))

    clean, vnotes, warnings = validate_params(props, values, strict=args.strict)
    notes.extend(vnotes)
    clean = fill_declared_defaults(props, clean)

    # 端点由模型声明解析（Kling 官方形态、seedance 等都有自己的路径），
    # 解析不到回退 /v1/videos——它与 /v1/video/generations 在后端是同一个处理器
    endpoint, endpoints = resolve_submit_endpoint(cfg, model, "video", meta, args.refresh)
    timeout = resolve_timeout(args, DEFAULT_VIDEO_TIMEOUT)
    if args.dry_run:
        reporter.result({
            "ok": True, "dry_run": True, "command": "video", "model": model,
            "endpoint": endpoint, "url": base + endpoint,
            "schema_source": meta.get("source"),
            "request": clean, "notes": notes, "warnings": warnings,
        }, [
            "DRY RUN — 未发送请求，无需 API Key。",
            "地址  %s" % (base + endpoint),
            "请求体：",
            json.dumps(clean, ensure_ascii=False, indent=2),
        ] + (["", "提示："] + ["  - " + n for n in notes + warnings] if notes or warnings else []))
        return 0

    key, _src = resolve_api_key(cfg)
    reporter.progress("提交生视频任务：%s（%s）" % (model, meta.get("source") or "no-schema"))
    status, payload, text = http_json(
        "POST", base + endpoint,
        headers={"Authorization": "Bearer " + key},
        body=clean, timeout=timeout,
    )
    if status != 200:
        message, hint = translate_error(status, payload, text, model)
        raise EasyAPIError("提交视频任务失败：%s" % message, hint=hint)

    task_id = extract_task_id(payload)
    if not task_id:
        raise EasyAPIError(
            "提交成功但没解析到任务 ID",
            detail=json.dumps(payload, ensure_ascii=False)[:600],
        )
    reporter.progress("任务已提交：%s" % task_id)

    if args.no_wait:
        reporter.result({
            "ok": True, "command": "video", "model": model, "task_id": task_id,
            "status": "submitted", "endpoint": endpoint,
            "poll_url": "%s%s/%s" % (base, endpoint.rstrip("/"), task_id),
            "request": clean, "notes": notes, "warnings": warnings,
        }, [
            "任务已提交（未等待完成）：%s" % task_id,
            "查询状态：GET %s%s/%s" % (base, endpoint.rstrip("/"), task_id),
        ])
        return 0

    final = poll_video_task(cfg, key, base, task_id, reporter, timeout,
                            args.poll_interval, endpoint=endpoint)

    download_url = final.get("download_url") or final.get("url")
    out_dir = ensure_out_dir(args.out)
    stem = timestamp_stem(model)
    saved = None
    download_error = None

    try:
        if download_url:
            saved = download_to(out_dir, stem, download_url, timeout)
        else:
            # 没有 download_url 时回退到 content 代理端点
            saved = download_to(
                out_dir, stem, "%s/v1/videos/%s/content" % (base, task_id), timeout)
    except EasyAPIError as exc:
        download_error = exc.message

    result = {
        "ok": True, "command": "video", "model": model, "task_id": task_id,
        "status": final.get("status"), "duration": final.get("duration"),
        "resolution": final.get("resolution"), "aspect_ratio": final.get("aspect_ratio"),
        "generate_audio": final.get("generate_audio"),
        "estimated_cost": final.get("estimated_cost"),
        "billed_cost": final.get("billed_cost"),
        "billing_status": final.get("billing_status"),
        "download_url": download_url,
        "path": saved,
        "download_error": download_error,
        "notes": notes, "warnings": warnings,
    }

    human = ["视频生成完成：%s" % model]
    human.append("  任务 ID   %s" % task_id)
    if final.get("duration"):
        human.append("  时长      %ss" % final["duration"])
    if final.get("resolution"):
        human.append("  分辨率    %s" % final["resolution"])
    if saved:
        human.append("  文件      %s" % saved)
    if download_url:
        human.append("  链接      %s" % download_url)
    if download_error:
        human.append("  下载失败  %s" % download_error)
    if final.get("billed_cost"):
        human.append("  实际扣费  $%s" % final["billed_cost"])
    elif final.get("estimated_cost"):
        human.append("  预估扣费  $%s" % final["estimated_cost"])
    if notes or warnings:
        human.append("")
        human.extend(["提示："] + ["  - " + n for n in notes + warnings])
    reporter.result(result, human)
    return 0


# --------------------------------------------------------------------------
# 子命令：文本对话
# --------------------------------------------------------------------------


def cmd_chat(args, reporter):
    cfg = load_config()
    base, _pricing = resolve_bases(cfg)

    model = _resolve_model(cfg, args.model, "text", reporter, args.refresh)

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    if args.image:
        content = [{"type": "text", "text": args.prompt}]
        for value in args.image:
            if re.match(r"^https?://", value) or value.startswith("data:"):
                url = value
            elif os.path.exists(value):
                with open(value, "rb") as fh:
                    ctype = mimetypes.guess_type(value)[0] or "image/png"
                    url = "data:%s;base64,%s" % (
                        ctype, base64.b64encode(fh.read()).decode("ascii"))
            else:
                raise EasyAPIError("图片 %r 既不是 URL 也不是存在的文件" % value)
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": args.prompt})

    body = {"model": model, "messages": messages}
    if args.max_tokens is not None:
        body["max_tokens"] = args.max_tokens
    if args.temperature is not None:
        body["temperature"] = args.temperature
    body.update(parse_param_flags(args.param))

    if args.dry_run:
        reporter.result({"ok": True, "dry_run": True, "command": "chat",
                         "model": model, "endpoint": "/v1/chat/completions",
                         "url": base + "/v1/chat/completions",
                         "request": body}, [
            "DRY RUN — 未发送请求，无需 API Key。",
            "地址  %s/v1/chat/completions" % base,
            "请求体：",
            json.dumps(body, ensure_ascii=False, indent=2),
        ])
        return 0

    key, _src = resolve_api_key(cfg)
    reporter.progress("请求文本模型：%s" % model)
    status, payload, text = http_json(
        "POST", base + "/v1/chat/completions",
        headers={"Authorization": "Bearer " + key},
        body=body, timeout=resolve_timeout(args),
    )
    if status != 200:
        message, hint = translate_error(status, payload, text, model)
        raise EasyAPIError("对话请求失败：%s" % message, hint=hint)

    choices = (payload or {}).get("choices") or []
    content = ""
    if choices and isinstance(choices[0], dict):
        content = ((choices[0].get("message") or {}).get("content") or "").strip()

    result = {
        "ok": True, "command": "chat", "model": model,
        "content": content, "usage": (payload or {}).get("usage"),
        "finish_reason": (choices[0].get("finish_reason") if choices else None),
    }
    human = [content] if content else ["（模型返回了空内容）"]
    usage = result["usage"] or {}
    if usage:
        human.append("")
        human.append("tokens：输入 %s / 输出 %s / 合计 %s"
                     % (usage.get("prompt_tokens"), usage.get("completion_tokens"),
                        usage.get("total_tokens")))
    reporter.result(result, human)
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="easyapi.py",
        description="EasyAPI 统一调用脚本（生图 / 生视频 / 对话 / 模型与参数查询）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    common.add_argument("--refresh", action="store_true", help="忽略缓存强制拉取")
    common.add_argument("--quiet", action="store_true", help="不输出进度信息")
    common.add_argument("--dry-run", action="store_true", help="只校验并打印请求体，不发送")
    common.add_argument("--strict", action="store_true", help="schema 之外的参数直接报错")
    # 默认值留空，由各子命令按自己的语义决定（生视频要等很久，不能用 HTTP 超时值）
    common.add_argument("--timeout", type=int, default=None,
                        help="超时秒数（生视频默认 %d，其余默认 %d）"
                             % (DEFAULT_VIDEO_TIMEOUT, DEFAULT_HTTP_TIMEOUT))

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="登录账号并自动配置 API Key（在用户自己的终端运行）",
                       parents=[common])
    p.add_argument("--browser", action="store_true",
                   help="浏览器授权登录：打开授权页点一下即可，无需输密码（推荐）")
    p.add_argument("--manual", action="store_true",
                   help="配合 --browser：不开浏览器，打印授权地址并手动粘贴授权码")
    p.add_argument("--client-name", default=DEFAULT_CLIENT_NAME,
                   help="授权页上展示的工具名（默认 %s）" % DEFAULT_CLIENT_NAME)
    p.add_argument("--username", help="账号（登录邮箱）；缺省时交互询问")
    p.add_argument("--access-token", action="store_true",
                   help="用控制台的系统访问令牌登录（不想输密码时用）")
    p.add_argument("--api-key", action="store_true",
                   help="直接粘贴已有的 sk- key（跳过账号与自动创建）")
    p.add_argument("--key-name", default=CLI_KEY_NAME,
                   help="自动创建/复用的令牌名（默认 %s）" % CLI_KEY_NAME)
    p.add_argument("--reuse-key", metavar="名字或ID",
                   help="复用账号下已有的某个令牌，而不是新建（配合报错里的列表）")
    p.add_argument("--create-new", action="store_true",
                   help="即使账号下已有其它令牌，也新建一个专用令牌")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="移除已保存的 API Key")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("config", help="查看或修改配置")
    p.add_argument("--base-url", help="网关入口（API），默认 %s" % DEFAULT_GATEWAY_BASE)
    p.add_argument("--pricing-base",
                   help="定价入口，仅当与网关入口不同域时才需要单独设置")
    p.add_argument("--console-base",
                   help="控制台（前端）入口，默认 %s；浏览器授权页在这里" % DEFAULT_CONSOLE_BASE)
    p.add_argument("--default-image-model", help="默认生图模型")
    p.add_argument("--default-video-model", help="默认生视频模型")
    p.add_argument("--default-text-model", help="默认文本模型")
    p.add_argument("--ref-mode", choices=[REF_MODE_EDIT, REF_MODE_IMAGE],
                   help="持久指定参考图投递方式（覆盖家族默认；不分模型一律生效）")
    p.add_argument("--cache-ttl", type=int, help="模型缓存有效期（秒）")
    p.add_argument("--clear-cache", action="store_true", help="清空模型与参数缓存")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("quota", help="查询令牌额度与用量", parents=[common])
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("env", help="环境自检：解释器 / 目录 / 快照 / 网关能力",
                       parents=[common])
    p.add_argument("--network", action="store_true",
                   help="联网探测网关与 /pricing/schema 接口是否可用")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("models", help="列出可用模型（匿名可访问）", parents=[common])
    p.add_argument("--category", help="按分类过滤：image / video / text")
    p.add_argument("--search", help="按模型名模糊搜索")
    p.add_argument("--seconds", type=int, default=5, help="按秒计费模型的估算时长（默认 5）")
    p.add_argument("--resolution", default="720p", help="估算用的分辨率（默认 720p）")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("schema", help="查看某个模型的参数定义（匿名可访问）", parents=[common])
    p.add_argument("model", help="模型名，如 seedream-4.5")
    p.add_argument("--category", help="参数分类：image / video / text（默认按模型分类推断）")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("compare", help="按价格对比模型（匿名可访问）", parents=[common])
    p.add_argument("models", nargs="*", help="要对比的模型名；留空则对比整个分类")
    p.add_argument("--category", default="image", help="留空 models 时的分类（默认 image）")
    p.add_argument("--seconds", type=int, default=5, help="按秒计费模型的估算时长（默认 5）")
    p.add_argument("--resolution", default="720p", help="估算用的分辨率（默认 720p）")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("image", help="生成图片", parents=[common])
    p.add_argument("--model", help="模型名；缺省用配置的默认生图模型")
    p.add_argument("--prompt", required=True, help="图像描述（必填）")
    p.add_argument("--size", help="输出尺寸，如 1024x1024")
    p.add_argument("--n", type=int, help="生成张数")
    p.add_argument("--quality", help="质量档")
    p.add_argument("--background", help="背景：transparent / opaque / auto")
    p.add_argument("--moderation", help="审核强度：low / auto")
    p.add_argument("--output-format", help="输出格式：png / jpeg / webp")
    p.add_argument("--output-compression", type=int, help="压缩率 0-100")
    p.add_argument("--partial-images", type=int, help="流式中间图数量 0-3")
    p.add_argument("--style", help="风格（仅部分模型支持）")
    p.add_argument("--input-fidelity", help="输入保真度")
    p.add_argument("--watermark", help="是否加水印 true/false")
    p.add_argument("--user", help="终端用户标识")
    p.add_argument("--stream", action="store_true", help="流式生成")
    p.add_argument("--ref", action="append",
                   help="参考图（图生图）：URL 或本地路径，可重复")
    p.add_argument("--ref-mode", choices=[REF_MODE_EDIT, REF_MODE_IMAGE],
                   help="强制参考图投递方式；默认按模型家族自动选（见 references/image.md）")
    p.add_argument("--param", action="append", help="额外参数 key=value，可重复")
    p.add_argument("--out", help="结果保存目录（默认 %s）" % DEFAULT_OUT_DIR)
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("video", help="生成视频（自动轮询到完成）", parents=[common])
    p.add_argument("--model", help="模型名；缺省用配置的默认生视频模型")
    p.add_argument("--prompt", help="视频描述")
    p.add_argument("--duration", help="时长秒数")
    p.add_argument("--resolution", help="分辨率档，如 720p / 1080P")
    p.add_argument("--aspect-ratio", help="宽高比，如 16:9")
    p.add_argument("--size", help="精确尺寸，如 1280x720")
    p.add_argument("--fps", type=int, help="帧率")
    p.add_argument("--generate-audio", help="是否生成音频 true/false")
    p.add_argument("--seed", type=int, help="随机种子")
    p.add_argument("--negative-prompt", help="负向提示词（部分模型支持）")
    p.add_argument("--mode", help="生成模式（部分模型支持）")
    p.add_argument("--image", help="单张输入图 URL 或本地路径")
    p.add_argument("--images", action="append", help="多张输入图，可重复")
    p.add_argument("--first-frame", help="首帧图片 URL 或本地路径")
    p.add_argument("--last-frame", help="尾帧图片（需与 --first-frame 同时给出）")
    p.add_argument("--ref-image", action="append", help="参考图，可重复")
    p.add_argument("--ref-video", action="append", help="参考视频，可重复")
    p.add_argument("--ref-audio", action="append", help="参考音频，可重复")
    p.add_argument("--param", action="append", help="额外参数 key=value，可重复")
    p.add_argument("--out", help="结果保存目录（默认 %s）" % DEFAULT_OUT_DIR)
    p.add_argument("--no-wait", action="store_true", help="提交后立即返回，不等待完成")
    p.add_argument("--poll-interval", type=float, default=3.0, help="轮询初始间隔秒数")
    p.set_defaults(func=cmd_video)

    p = sub.add_parser("chat", help="文本 / 多模态对话", parents=[common])
    p.add_argument("--model", help="模型名；缺省用配置的默认文本模型")
    p.add_argument("--prompt", required=True, help="用户输入")
    p.add_argument("--system", help="系统提示词")
    p.add_argument("--image", action="append", help="图片 URL 或本地路径，可重复")
    p.add_argument("--max-tokens", type=int, help="最大输出 token")
    p.add_argument("--temperature", type=float, help="采样温度")
    p.add_argument("--param", action="append", help="额外参数 key=value，可重复")
    p.set_defaults(func=cmd_chat)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    reporter = Reporter(as_json=getattr(args, "json", False),
                        quiet=getattr(args, "quiet", False))
    try:
        return args.func(args, reporter) or 0
    except EasyAPIError as exc:
        payload = {"ok": False, "error": exc.message}
        if exc.hint:
            payload["hint"] = exc.hint
        if exc.detail:
            payload["detail"] = exc.detail
        reporter.result(payload, ["错误：%s" % exc.message]
                        + (["", "建议：", exc.hint] if exc.hint else [])
                        + (["", "详情：", exc.detail] if exc.detail else []))
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断\n")
        return 130
    except Exception as exc:  # noqa: BLE001
        # 兜底：任何未预期的异常也要变成结构化输出。
        # 调用方是 agent，拿到 traceback 无法自行判断该改什么，必须给出可读的错误。
        import traceback
        detail = traceback.format_exc() if os.environ.get("EASYAPI_DEBUG") else None
        reporter.result(
            {"ok": False, "error": "未预期的错误：%s: %s" % (type(exc).__name__, exc),
             **({"detail": detail} if detail else {})},
            ["错误：未预期的错误 %s: %s" % (type(exc).__name__, exc),
             "", "这通常是脚本 bug，请把下面的信息反馈给维护者：",
             "  %s" % traceback.format_exc().strip().splitlines()[-1]]
            + (["", detail] if detail else []),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
