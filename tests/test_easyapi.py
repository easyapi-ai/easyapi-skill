#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""easyapi.py 的测试。

分两层：
  1. 纯逻辑单测（无网络）—— 参数校验、归一化、价格估算、错误翻译。
     这些是 skill 的核心价值，必须确定性地覆盖。
  2. mock server 集成测试 —— 用本地 HTTP 服务模拟网关，验证提交、轮询、
     下载这条异步链路的真实行为（含超时与失败分支）。

运行：python3 tests/test_easyapi.py
"""

import base64
import hashlib
import http.server
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
# 技能本体在插件目录里（插件规范要求 skills/<name>/SKILL.md）。
# 两个技能共用同一份运行时：studio 的脚本是源，auth 的是字节副本。
SKILL_DIR = os.path.join(REPO_ROOT, "plugins", "easyapi", "skills", "easyapi-studio")
SCRIPT = os.path.join(SKILL_DIR, "scripts", "easyapi.py")
AUTH_SKILL_DIR = os.path.join(REPO_ROOT, "plugins", "easyapi", "skills", "easyapi-auth")
AUTH_SCRIPT = os.path.join(AUTH_SKILL_DIR, "scripts", "easyapi.py")

# 1x1 PNG：测试用参考图/返回图，字节稳定便于断言完整上传
TINY_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
                "q842iQAAAABJRU5ErkJggg==")
TINY_PNG = base64.b64decode(TINY_PNG_B64)


def load_module():
    """把脚本当模块导入，用于测纯函数。"""
    spec = importlib.util.spec_from_file_location("easyapi_mod", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_module()


# ---------------------------------------------------------------------------
# 1. 纯逻辑
# ---------------------------------------------------------------------------


class TestBaseResolution(unittest.TestCase):
    """线上实测：调用、定价、额度都在同一个域名下，默认值必须是网关地址。

    曾经的错误默认值（api.easyapi.com）指向一个普通 Java 服务，所有 /v1 调用都会 400，
    所以这里把默认值钉死。
    """

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("EASYAPI_BASE_URL", "EASYAPI_PRICING_BASE_URL",
                                 "EASYAPI_CONSOLE_BASE_URL")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_default_is_the_gateway_host(self):
        base, pricing = mod.resolve_bases({})
        self.assertEqual(base, "https://token.easyapi.com")
        self.assertEqual(pricing, base)

    def test_pricing_follows_base_url_by_default(self):
        base, pricing = mod.resolve_bases({"base_url": "https://gw.example.com"})
        self.assertEqual(base, "https://gw.example.com")
        self.assertEqual(pricing, base, "定价入口应默认跟随网关入口")

    def test_pricing_can_be_overridden_for_private_deployments(self):
        base, pricing = mod.resolve_bases({
            "base_url": "https://gw.example.com",
            "pricing_base": "https://price.example.com",
        })
        self.assertEqual(base, "https://gw.example.com")
        self.assertEqual(pricing, "https://price.example.com")

    def test_env_wins_over_config(self):
        os.environ["EASYAPI_BASE_URL"] = "https://env.example.com"
        base, _pricing = mod.resolve_bases({"base_url": "https://cfg.example.com"})
        self.assertEqual(base, "https://env.example.com")

    def test_trailing_slash_stripped(self):
        base, pricing = mod.resolve_bases({"base_url": "https://gw.example.com/"})
        self.assertEqual(base, "https://gw.example.com")
        self.assertEqual(pricing, "https://gw.example.com")

    def test_console_base_defaults_to_frontend_domain(self):
        # 前端与接口不同域：控制台在 www，网关在 token。
        # 曾用网关域名拼授权页地址 → 打到只接受 POST 的 API 路由（GET 404）
        console = mod.resolve_console_base({})
        self.assertEqual(console, "https://www.easyapi.com")
        self.assertNotEqual(console, mod.DEFAULT_GATEWAY_BASE)

    def test_console_base_env_override(self):
        os.environ["EASYAPI_CONSOLE_BASE_URL"] = "https://console.example.com/"
        self.assertEqual(mod.resolve_console_base({}), "https://console.example.com")

    def test_console_base_config_override(self):
        self.assertEqual(
            mod.resolve_console_base({"console_base": "https://cfg.example.com"}),
            "https://cfg.example.com")


class TestWrongHostDetection(unittest.TestCase):
    """配错 base_url 时给出明确指引，而不是一个含糊的 400。"""

    SPRING_BODY = ('{"code":-1,"message":"No static resource v1/images/generations.",'
                   '"path":"http://api.example.com/v1/images/generations"}')

    def test_marker_detected_and_hint_names_the_cause(self):
        _msg, hint = mod.translate_error(400, None, self.SPRING_BODY)
        self.assertIn("不是 EasyAPI 网关", hint)
        self.assertIn(mod.DEFAULT_GATEWAY_BASE, hint, "要告诉用户正确地址是什么")

    def test_marker_takes_priority_over_generic_400(self):
        payload = {"code": -1, "message": "No static resource pricing."}
        _msg, hint = mod.translate_error(400, payload, json.dumps(payload))
        self.assertIn("网关", hint)
        self.assertNotIn("schema", hint, "不该退回到通用的参数建议")

    def test_normal_gateway_error_unaffected(self):
        _msg, hint = mod.translate_error(400, {"message": "model is required"}, "")
        self.assertIn("--model", hint)


class TestNormalizeSizeLike(unittest.TestCase):
    def test_fullwidth_and_star_separators(self):
        for raw in ("1024×1024", "1024*1024", "1024 X 1024", "1024ｘ1024", " 1024x1024 "):
            self.assertEqual(mod.normalize_size_like(raw), "1024x1024", raw)

    def test_non_string_passthrough(self):
        self.assertEqual(mod.normalize_size_like(5), 5)
        self.assertIsNone(mod.normalize_size_like(None))

    def test_does_not_touch_other_values(self):
        self.assertEqual(mod.normalize_size_like("16:9"), "16:9")
        self.assertEqual(mod.normalize_size_like("一只猫在跑"), "一只猫在跑")
        # 自由文本里的 x 不能被当成尺寸分隔符改写
        self.assertEqual(mod.normalize_size_like("一只猫 x 一只狗"), "一只猫 x 一只狗")
        self.assertEqual(mod.normalize_size_like("2 x 3 网格"), "2 x 3 网格")


class TestCoerceValue(unittest.TestCase):
    def test_integer_from_string(self):
        value, notes = mod.coerce_value("n", {"type": "integer"}, "3")
        self.assertEqual(value, 3)
        self.assertEqual(notes, [])

    def test_integer_truncation_is_reported(self):
        value, notes = mod.coerce_value("n", {"type": "integer"}, "3.7")
        self.assertEqual(value, 3)
        self.assertTrue(notes, "取整必须告知用户")

    def test_integer_rejects_garbage(self):
        with self.assertRaises(mod.EasyAPIError):
            mod.coerce_value("n", {"type": "integer"}, "abc")

    def test_boolean_variants(self):
        for raw in ("true", "TRUE", "1", "yes", "on"):
            value, _ = mod.coerce_value("stream", {"type": "boolean"}, raw)
            self.assertIs(value, True, raw)
        for raw in ("false", "0", "no", "off"):
            value, _ = mod.coerce_value("stream", {"type": "boolean"}, raw)
            self.assertIs(value, False, raw)

    def test_boolean_rejects_garbage(self):
        with self.assertRaises(mod.EasyAPIError):
            mod.coerce_value("stream", {"type": "boolean"}, "maybe")

    def test_number(self):
        value, _ = mod.coerce_value("ratio", {"type": "number"}, "1.5")
        self.assertEqual(value, 1.5)


class TestNormalizeEnum(unittest.TestCase):
    def test_exact_match(self):
        value, notes = mod.normalize_enum("resolution", {"enum": ["720P", "1080P"]}, "720P")
        self.assertEqual(value, "720P")
        self.assertEqual(notes, [])

    def test_case_insensitive_rewrite(self):
        value, notes = mod.normalize_enum("resolution", {"enum": ["720P", "1080P"]}, "1080p")
        self.assertEqual(value, "1080P", "必须回写成 schema 里的标准写法")
        self.assertTrue(notes, "归一化必须告知用户")

    def test_invalid_lists_allowed(self):
        with self.assertRaises(mod.EasyAPIError) as ctx:
            mod.normalize_enum("resolution", {"enum": ["720P", "1080P"]}, "4k")
        self.assertIn("720P", ctx.exception.hint)
        self.assertIn("1080P", ctx.exception.hint)

    def test_no_enum_is_noop(self):
        value, _ = mod.normalize_enum("size", {}, "任意值")
        self.assertEqual(value, "任意值")


class TestCheckBounds(unittest.TestCase):
    def test_below_min(self):
        with self.assertRaises(mod.EasyAPIError):
            mod.check_bounds("duration", {"min": 3, "max": 15}, 2)

    def test_above_max(self):
        with self.assertRaises(mod.EasyAPIError):
            mod.check_bounds("duration", {"min": 3, "max": 15}, 30)

    def test_in_range_ok(self):
        self.assertEqual(mod.check_bounds("duration", {"min": 3, "max": 15}, 5), [])

    def test_string_max_length(self):
        with self.assertRaises(mod.EasyAPIError):
            mod.check_bounds("prompt", {"max_length": 5}, "这句话明显超过了五个字")

    def test_string_length_within_limit_ok(self):
        self.assertEqual(mod.check_bounds("prompt", {"max_length": 50}, "短"), [])

    def test_booleans_skip_numeric_bounds(self):
        # bool 是 int 的子类，不特判会被 0/1 边界误伤
        self.assertEqual(mod.check_bounds("generate_audio", {"min": 0, "max": 1}, True), [])


class TestValidateParams(unittest.TestCase):
    PROPS = {
        "prompt": {"type": "string", "required": True, "max_length": 10},
        "duration": {"type": "integer", "min": 3, "max": 15, "default": 5},
        "resolution": {"type": "string", "enum": ["720P", "1080P"], "default": "720P"},
    }

    def test_happy_path_types_coerced(self):
        clean, notes, warnings = mod.validate_params(
            self.PROPS, {"prompt": "海边", "duration": "8", "resolution": "1080p"})
        self.assertEqual(clean["duration"], 8)
        self.assertEqual(clean["resolution"], "1080P")
        self.assertEqual(warnings, [])

    def test_missing_required_raises_with_description(self):
        with self.assertRaises(mod.EasyAPIError) as ctx:
            mod.validate_params(self.PROPS, {"duration": 5})
        self.assertIn("prompt", ctx.exception.message)

    def test_unknown_param_warns_by_default(self):
        clean, _notes, warnings = mod.validate_params(
            self.PROPS, {"prompt": "x", "cfg_scale": 7})
        self.assertEqual(clean["cfg_scale"], 7, "未知参数仍要透传给上游")
        self.assertTrue(any("cfg_scale" in w for w in warnings))

    def test_unknown_param_errors_in_strict_mode(self):
        with self.assertRaises(mod.EasyAPIError):
            mod.validate_params(self.PROPS, {"prompt": "x", "cfg_scale": 7}, strict=True)

    def test_envelope_fields_never_warn(self):
        _clean, _notes, warnings = mod.validate_params(
            self.PROPS, {"model": "Kling-3.0", "prompt": "x"})
        self.assertEqual(warnings, [], "model 是信封字段，不该告警")

    def test_none_values_dropped(self):
        clean, _n, _w = mod.validate_params(self.PROPS, {"prompt": "x", "duration": None})
        self.assertNotIn("duration", clean)

    def test_no_schema_means_no_validation(self):
        clean, _n, warnings = mod.validate_params({}, {"prompt": "x", "任意": 1})
        self.assertEqual(clean, {"prompt": "x", "任意": 1})
        self.assertEqual(warnings, [])


class TestFillDefaults(unittest.TestCase):
    PROPS = {"duration": {"default": 5}, "resolution": {"default": "720P"}, "seed": {}}

    def test_only_listed_keys_filled(self):
        out = mod.fill_defaults(self.PROPS, {"prompt": "x"}, ("duration", "resolution"))
        self.assertEqual(out["duration"], 5)
        self.assertEqual(out["resolution"], "720P")
        self.assertNotIn("seed", out)

    def test_explicit_value_wins(self):
        out = mod.fill_defaults(self.PROPS, {"duration": 9}, ("duration",))
        self.assertEqual(out["duration"], 9)


class TestBuiltinDefaultsAndTimeouts(unittest.TestCase):
    def test_builtin_default_models(self):
        self.assertEqual(mod.BUILTIN_DEFAULT_MODELS["image"], "qwen-image-3.0-pro")
        self.assertEqual(mod.BUILTIN_DEFAULT_MODELS["video"], "wan3.0-video")

    def test_configured_default_wins_and_empty_string_clears(self):
        cfg = {"default_image_model": "seedream-4.5"}
        self.assertEqual(mod.configured_default_model(cfg, "image"), "seedream-4.5")
        self.assertIsNone(mod.configured_default_model({"default_image_model": "  "}, "image"))
        self.assertIsNone(mod.configured_default_model({}, "image"))

    def test_image_waits_thirty_minutes_by_default(self):
        # 生图是同步接口，慢模型 + 高峰期远超 2 分钟；这里钉死 30 分钟
        self.assertEqual(mod.DEFAULT_IMAGE_TIMEOUT, 1800)
        self.assertEqual(mod.resolve_timeout(object(), mod.DEFAULT_IMAGE_TIMEOUT), 1800)
        self.assertEqual(mod.resolve_timeout(object(), mod.DEFAULT_VIDEO_TIMEOUT), 900)


class TestVideoSubmitLock(unittest.TestCase):
    """并发提交锁：防止多进程同时通过台账检查、各提交一次。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="easyapi-lock-")
        self._old = mod.VIDEO_LOCK_PATH
        mod.VIDEO_LOCK_PATH = os.path.join(self.dir, "video_submit.lock")

    def tearDown(self):
        mod.VIDEO_LOCK_PATH = self._old
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_lock_is_exclusive(self):
        mod.acquire_video_submit_lock()
        try:
            with self.assertRaises(mod.EasyAPIError):
                mod.acquire_video_submit_lock(timeout=0.3)
        finally:
            mod.release_video_submit_lock()
        mod.acquire_video_submit_lock(timeout=0.3)
        mod.release_video_submit_lock()

    def test_stale_lock_is_reclaimed(self):
        with open(mod.VIDEO_LOCK_PATH, "w", encoding="utf-8") as fh:
            fh.write("999999")
        old = time.time() - mod.VIDEO_LOCK_STALE - 10
        os.utime(mod.VIDEO_LOCK_PATH, (old, old))
        mod.acquire_video_submit_lock(timeout=0.3)
        mod.release_video_submit_lock()
        self.assertFalse(os.path.exists(mod.VIDEO_LOCK_PATH))


class TestPickRatio(unittest.TestCase):
    """各模型分辨率键写法不统一，这是线上真实存在的坑。"""

    def test_kling_style_keys(self):
        table = {"720p": 1, "1080p": 1.333333, "2k": 1.666667, "4k": 4.999989}
        self.assertEqual(mod.pick_ratio(table, "1080p"), 1.333333)
        self.assertEqual(mod.pick_ratio(table, "1080P"), 1.333333)

    def test_seedance_style_keys_without_p(self):
        # seedance-2.5 的 video_per_second_ratios 键是 480/720，不带 p
        table = {"480": 1, "720": 2.248054}
        self.assertEqual(mod.pick_ratio(table, "720p"), 2.248054)
        self.assertEqual(mod.pick_ratio(table, "720"), 2.248054)

    def test_falls_back_to_default(self):
        self.assertEqual(mod.pick_ratio({"default": 1.5}, "9k"), 1.5)

    def test_empty_table(self):
        self.assertIsNone(mod.pick_ratio({}, "720p"))
        self.assertIsNone(mod.pick_ratio(None, "720p"))


class TestEstimateUsd(unittest.TestCase):
    def test_per_call(self):
        usd, reason = mod.estimate_usd({"quota_type": 1, "model_price": 0.03})
        self.assertAlmostEqual(usd, 0.03)
        self.assertIsNone(reason)

    def test_per_call_times_n(self):
        usd, _ = mod.estimate_usd({"quota_type": 1, "model_price": 0.03}, n=4)
        self.assertAlmostEqual(usd, 0.12)

    def test_per_second(self):
        entry = {"quota_type": 2, "model_ratio": 0.089286,
                 "video_per_second_ratios": {"720p": 1}}
        usd, reason = mod.estimate_usd(entry, seconds=5, resolution="720p")
        self.assertAlmostEqual(usd, 0.089286 * 1 * 5)
        self.assertIsNone(reason)

    def test_per_second_without_matching_resolution(self):
        entry = {"quota_type": 2, "model_ratio": 1,
                 "video_per_second_ratios": {"480": 1}}
        usd, reason = mod.estimate_usd(entry, seconds=5, resolution="4k")
        self.assertIsNone(usd)
        self.assertIn("分辨率", reason)

    def test_token_based_cannot_estimate(self):
        usd, reason = mod.estimate_usd({"quota_type": 0, "model_ratio": 3.39})
        self.assertIsNone(usd)
        self.assertIn("token", reason)

    def test_group_ratio_applied(self):
        usd, _ = mod.estimate_usd({"quota_type": 1, "model_price": 1.0}, group_ratio=0.5)
        self.assertAlmostEqual(usd, 0.5)


class TestExtractTaskId(unittest.TestCase):
    """提交响应在不同端点上字段名不同，全部要兜住。"""

    def test_direct_fields(self):
        self.assertEqual(mod.extract_task_id({"id": "a"}), "a")
        self.assertEqual(mod.extract_task_id({"task_id": "b"}), "b")
        self.assertEqual(mod.extract_task_id({"job_id": "c"}), "c")

    def test_nested(self):
        self.assertEqual(mod.extract_task_id({"data": {"task_id": "d"}}), "d")
        self.assertEqual(mod.extract_task_id({"data": {"job_id": "e"}}), "e")

    def test_data_as_bare_string(self):
        self.assertEqual(mod.extract_task_id({"data": "f"}), "f")

    def test_missing(self):
        self.assertIsNone(mod.extract_task_id({}))
        self.assertIsNone(mod.extract_task_id(None))
        self.assertIsNone(mod.extract_task_id({"status": "queued"}))

    def test_blank_string_is_not_an_id(self):
        self.assertIsNone(mod.extract_task_id({"id": "   "}))


class TestDecodeDataUrl(unittest.TestCase):
    def test_png(self):
        raw, ext = mod.decode_data_url("data:image/png;base64,aGk=")
        self.assertEqual(raw, b"hi")
        self.assertEqual(ext, ".png")

    def test_jpeg_mapped_to_jpg(self):
        _raw, ext = mod.decode_data_url("data:image/jpeg;base64,aGk=")
        self.assertEqual(ext, ".jpg")

    def test_not_a_data_url(self):
        self.assertIsNone(mod.decode_data_url("https://example.com/a.png"))
        self.assertIsNone(mod.decode_data_url(None))


class TestTranslateError(unittest.TestCase):
    def test_401_points_at_key(self):
        _msg, hint = mod.translate_error(401, {"message": "unauthorized"}, "")
        self.assertIn("Key", hint)

    def test_model_is_required_gives_specific_hint(self):
        _msg, hint = mod.translate_error(400, {"message": "model is required"}, "")
        self.assertIn("--model", hint)

    def test_quota_exhausted(self):
        _msg, hint = mod.translate_error(402, {"message": "insufficient quota"}, "")
        self.assertIn("额度", hint)

    def test_400_falls_back_to_schema_hint(self):
        _msg, hint = mod.translate_error(400, {"message": "bad size"}, "", model="seedream-4.5")
        self.assertIn("seedream-4.5", hint)

    def test_message_extracted_from_nested_error(self):
        msg, _hint = mod.translate_error(400, {"error": {"message": "boom"}}, "")
        self.assertEqual(msg, "boom")

    def test_non_json_body(self):
        msg, _hint = mod.translate_error(500, None, "<html>bad gateway</html>")
        self.assertIn("bad gateway", msg)


class TestSchemaResolution(unittest.TestCase):
    """schema 取值优先级：线上 > 内置快照。"""

    def test_extract_properties_case_insensitive_category(self):
        params = {"image": {"properties": {"prompt": {"type": "string"}}}}
        self.assertIsNotNone(mod.extract_properties(params, "Image"))
        self.assertIsNotNone(mod.extract_properties(params, "image"))

    def test_extract_properties_missing(self):
        self.assertIsNone(mod.extract_properties({"image": {"input": ["text"]}}, "image"))
        self.assertIsNone(mod.extract_properties(None, "image"))
        self.assertIsNone(mod.extract_properties({"video": {}}, "image"))

    def test_snapshot_loads_and_covers_live_models(self):
        snap = mod.load_snapshot()
        self.assertTrue(snap.get("models"), "内置快照必须存在")
        # 线上这些模型 params 为 null，全靠快照兜底
        for name in ("seedream-4.5", "qwen-image-2.0", "seedance-2.0", "hailuo-2.3"):
            entry = snap["models"].get(name)
            self.assertIsNotNone(entry, "快照缺少 %s" % name)
            props = mod.extract_properties(entry["params"], entry["category"])
            self.assertTrue(props, "快照里 %s 没有 properties" % name)

    def test_snapshot_has_no_server_only_fields(self):
        raw = open(os.path.join(SKILL_DIR, "references",
                                "model-snapshot.json"), encoding="utf-8").read()
        for leaked in ("upstream_name", "confidence", '"source"'):
            self.assertNotIn(leaked, raw, "服务端专有字段不该下发给客户端")


class TestParseParamFlags(unittest.TestCase):
    def test_json_values_parsed(self):
        self.assertEqual(mod.parse_param_flags(["a=1", "b=true", "c=null"]),
                         {"a": 1, "b": True, "c": None})

    def test_bare_string_fallback(self):
        self.assertEqual(mod.parse_param_flags(["negative_prompt=模糊"]),
                         {"negative_prompt": "模糊"})

    def test_missing_equals_raises(self):
        with self.assertRaises(mod.EasyAPIError):
            mod.parse_param_flags(["oops"])

    def test_empty(self):
        self.assertEqual(mod.parse_param_flags(None), {})
        self.assertEqual(mod.parse_param_flags([]), {})


class TestUsdFromQuota(unittest.TestCase):
    def test_conversion(self):
        self.assertAlmostEqual(mod.usd_from_quota(500000), 1.0)
        self.assertAlmostEqual(mod.usd_from_quota(0), 0.0)

    def test_garbage_is_zero(self):
        self.assertEqual(mod.usd_from_quota(None), 0.0)
        self.assertEqual(mod.usd_from_quota("x"), 0.0)


# ---------------------------------------------------------------------------
# 2. mock server 集成测试
# ---------------------------------------------------------------------------


class MockGateway:
    """模拟网关：定价接口、任务提交、状态轮询、结果下载。

    poll_sequence 控制每次轮询返回的状态，用来覆盖「轮询到完成」和「上游失败」两条分支。
    """

    def __init__(self, poll_sequence=("in_progress", "completed"), download_ok=True,
                 wrong_host=False, users=None, tokens=None, require_2fa=False,
                 max_tokens=False, captcha=False, two_fa_code="123456",
                 cli_auth_enabled=True, schema_bundle=None, schema_single=None,
                 detail_models=None, pricing_rows=None, cli_poll_enabled=False,
                 fail_message="上游拒绝：内容不合规", fail_billing_status=None):
        self.poll_sequence = list(poll_sequence)
        self.download_ok = download_ok
        self.fail_message = fail_message
        self.fail_billing_status = fail_billing_status
        self.wrong_host = wrong_host
        self.users = dict(users) if users else {"user@example.com": "secret-pass"}
        self.tokens = [dict(t) for t in (tokens or [])]
        self.require_2fa = require_2fa
        self.max_tokens = max_tokens
        self.captcha = captcha
        self.two_fa_code = two_fa_code
        self.cli_auth_enabled = cli_auth_enabled
        # False = 老后端：没有 /cli/session 与 /cli/ticket，CLI 必须回退 loopback
        self.cli_poll_enabled = cli_poll_enabled
        # /pricing/schema 的两个模式：None 表示老部署（路由不存在 -> 404）
        self.schema_bundle = schema_bundle
        self.schema_single = dict(schema_single or {})
        # None = 任何模型都返回同一份详情；给定集合时只对这些模型返回，其余 404
        self.detail_models = None if detail_models is None else set(detail_models)
        # /pricing 的模型行；默认一个按秒计费的视频模型
        self.pricing_rows = pricing_rows or [{"model_name": "mock-video", "category": "Video",
                                              "quota_type": 2, "model_ratio": 0.1,
                                              "video_per_second_ratios": {"720p": 1}}]
        self.cli_tickets = {}
        self.cli_sessions = {}   # state -> {"verifier_hash": ...}
        self.cli_pending = {}    # state -> ticket（授权后、等 CLI 取走）
        self.created_tokens = []
        self.token_bodies = []    # POST /token 的请求体，用于断言设备绑定/额度
        self.deleted_tokens = []  # DELETE /token/:id 的 id
        self.requests = []
        self.raw_requests = []   # [(path, headers, raw_bytes)]，用于断言 multipart 结构
        self.polls = 0
        self._server = None
        self._thread = None
        self.base_url = None

    def start(self):
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # 静音

            def _send(self, code, payload, ctype="application/json"):
                body = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        if not isinstance(payload, bytes) else payload)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                gateway.requests.append(("GET", self.path))
                gateway.raw_requests.append((self.path, dict(self.headers), b""))
                if gateway.wrong_host:
                    # 模拟指到一个普通 Java 服务：所有路由都返回 Spring 的 404 页面
                    return self._send(400, {
                        "code": -1,
                        "message": "No static resource %s." % self.path.lstrip("/"),
                        "path": gateway.base_url + self.path,
                    })
                if self.path.startswith("/app-config"):
                    # 线上真实结构：字段包在 data 里（曾因没解包导致降级判断全错）
                    return self._send(200, {"success": True, "data": {
                        "turnstile_check": gateway.captcha,
                        "aliyun_captcha_enabled": False,
                        "cli_authorize": gateway.cli_auth_enabled,
                    }})
                if self.path.startswith("/cli/ticket"):
                    # 轮询取票；老后端整条路由不存在（404），CLI 据此回退 loopback
                    if not gateway.cli_poll_enabled:
                        return self._send(404, {"error": {"message": "Invalid URL (GET %s)" % self.path}})
                    from urllib.parse import urlparse, parse_qs
                    query = parse_qs(urlparse(self.path).query)
                    state = (query.get("state") or [""])[0]
                    verifier = (query.get("verifier") or [""])[0]
                    session = gateway.cli_sessions.get(state)
                    if session is None:
                        return self._send(200, {"success": False,
                                                "message": "授权会话不存在或已过期，请回到命令行重新发起登录"})
                    if hashlib.sha256(verifier.encode("utf-8")).hexdigest() != session["verifier_hash"]:
                        return self._send(200, {"success": False, "message": "verifier 校验失败"})
                    ticket = gateway.cli_pending.get(state)
                    if not ticket:
                        return self._send(200, {"success": True, "data": {"status": "pending"}})
                    return self._send(200, {"success": True,
                                            "data": {"status": "ready", "ticket": ticket}})
                if self.path.startswith("/cli/authorize"):
                    # 授权页是前端路由（GET 返回 HTML），与 API 的 POST /cli/authorize 同路径
                    if not gateway.cli_auth_enabled:
                        return self._send(404, {"error": {"message": "Invalid URL (GET %s)" % self.path}})
                    return self._send(200, b"<!doctype html><title>EasyAPI</title>",
                                      "text/html; charset=utf-8")
                if self.path.startswith("/tokens"):
                    from urllib.parse import urlparse, parse_qs
                    keyword = (parse_qs(urlparse(self.path).query).get("keyword") or [""])[0]
                    items = [t for t in gateway.tokens if keyword in t["name"]]
                    return self._send(200, {"success": True, "data": {"items": items,
                                                                      "total": len(items)}})
                if self.path.startswith("/user/self"):
                    auth = self.headers.get("Authorization") or ""
                    if auth != "Bearer dash-access-token":
                        return self._send(401, {"success": False, "message": "未登录"})
                    return self._send(200, {"success": True, "data": {
                        "id": 42, "username": "user@example.com",
                        "quota": 8000000, "used_quota": 2000000, "request_count": 7}})
                if self.path.startswith("/usage/token"):
                    return self._send(200, {"code": True, "message": "ok", "data": {
                        "object": "token_usage", "name": "t", "total_granted": 5000000,
                        "total_used": 1000000, "total_available": 4000000,
                        "unlimited_quota": False, "model_limits_enabled": False,
                        "model_limits": {}, "expires_at": -1}})
                if self.path.startswith("/pricing/schema"):
                    from urllib.parse import urlparse, parse_qs
                    query = parse_qs(urlparse(self.path).query)
                    if query.get("all") == ["1"]:
                        if gateway.schema_bundle is None:
                            return self._send(404, {"success": False,
                                                    "message": "Invalid URL"})
                        return self._send(200, {"success": True,
                                                "data": gateway.schema_bundle})
                    name = (query.get("model_name") or [""])[0]
                    if not name:
                        # 新部署缺参数返回 400；老部署整条路由不存在返回 404
                        if gateway.schema_bundle is None and not gateway.schema_single:
                            return self._send(404, {"success": False,
                                                    "message": "Invalid URL"})
                        return self._send(400, {"success": False,
                                                "message": "model_name is required"})
                    if name not in gateway.schema_single:
                        return self._send(404, {"success": False,
                                                "message": "model not found"})
                    return self._send(200, {"success": True,
                                            "data": gateway.schema_single[name]})
                if self.path.startswith("/pricing/detail"):
                    from urllib.parse import urlparse, parse_qs
                    name = (parse_qs(urlparse(self.path).query).get("model_name") or [""])[0]
                    if gateway.detail_models is not None and name not in gateway.detail_models:
                        return self._send(404, {"success": False, "message": "model not found"})
                    return self._send(200, {"success": True, "data": {"params": {
                        "video": {"properties": {
                            "prompt": {"type": "string"},
                            "duration": {"type": "integer", "min": 3, "max": 15, "default": 5},
                            "resolution": {"type": "string",
                                           "enum": ["720P", "1080P"], "default": "720P"},
                        }}}}})
                if self.path.startswith("/pricing"):
                    return self._send(200, {
                        "success": True, "pricing_version": "v-test",
                        "group_ratio": {"default": 1}, "vendors": [],
                        "data": gateway.pricing_rows,
                    })
                if self.path.startswith("/v1/videos/"):
                    if self.path.endswith("/content"):
                        return self._send(200, b"FAKEVIDEOBYTES", "video/mp4")
                    idx = min(gateway.polls, len(gateway.poll_sequence) - 1)
                    state = gateway.poll_sequence[idx]
                    gateway.polls += 1
                    data = {"job_id": "task-123", "status": state}
                    if state == "completed":
                        data["download_url"] = gateway.base_url + "/result.mp4"
                        data["billed_cost"] = "0.0500"
                    if state == "failed":
                        if gateway.fail_message:
                            data["message"] = gateway.fail_message
                        if gateway.fail_billing_status:
                            data["billing_status"] = gateway.fail_billing_status
                    return self._send(200, {"code": 200, "msg": "", "data": data})
                if self.path == "/result.mp4":
                    if not gateway.download_ok:
                        return self._send(500, {"message": "nope"})
                    return self._send(200, b"FAKEVIDEOBYTES", "video/mp4")
                if self.path == "/ref.png":
                    # 供「远程参考图先下载再上传」的测试用
                    return self._send(200, TINY_PNG, "image/png")
                return self._send(404, {"message": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                gateway.requests.append(("POST", self.path, raw.decode("utf-8", "replace")))
                gateway.raw_requests.append((self.path, dict(self.headers), raw))
                if gateway.wrong_host:
                    return self._send(400, {
                        "code": -1,
                        "message": "No static resource %s." % self.path.lstrip("/"),
                        "path": gateway.base_url + self.path,
                    })
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}

                # 生图/图像编辑：同步返回一张极小 PNG（b64_json），供落盘断言
                if self.path in ("/v1/images/generations", "/v1/images/edits"):
                    return self._send(200, {
                        "created": 1718452800,
                        "data": [{"b64_json": TINY_PNG_B64, "revised_prompt": None}],
                    })

                if self.path == "/user/login":
                    if gateway.captcha:
                        return self._send(403, {"success": False,
                                                "message": "人机验证失败"})
                    username, password = body.get("username"), body.get("password")
                    if username not in gateway.users or gateway.users[username] != password:
                        return self._send(200, {"success": False,
                                                "message": "用户名或密码错误"})
                    if gateway.require_2fa:
                        return self._send(200, {"success": True, "data":
                                                {"require_2fa": True}})
                    return self._send(200, {"success": True, "data": {
                        "id": 42, "username": username, "nickname": " tester",
                        "access_token": "dash-access-token"}})

                if self.path == "/user/login/2fa":
                    if body.get("code") != gateway.two_fa_code:
                        return self._send(200, {"success": False,
                                                "message": "验证码或备用码错误，请重试"})
                    return self._send(200, {"success": True, "data": {
                        "id": 42, "username": "user@example.com",
                        "access_token": "dash-access-token"}})

                if self.path == "/token":
                    if gateway.max_tokens:
                        return self._send(200, {"success": False,
                                                "message": "已达到最大令牌数量限制 (10)"})
                    gateway.token_bodies.append(body)
                    new_id = 1000 + len(gateway.tokens)
                    raw_key = "raw" + "a1b2c3d4e5f6a7b8c9d0" * 2 + "end"
                    gateway.tokens.append({"id": new_id, "name": body.get("name"),
                                           "status": 1, "key": raw_key,
                                           "client_id": body.get("client_id") or ""})
                    gateway.created_tokens.append(body.get("name"))
                    return self._send(200, {"success": True, "message": ""})

                if self.path.startswith("/token/") and self.path.endswith("/key"):
                    token_id = int(self.path.split("/")[2])
                    for t in gateway.tokens:
                        if t["id"] == token_id:
                            return self._send(200, {"success": True,
                                                    "data": {"key": t["key"]}})
                    return self._send(200, {"success": False, "message": "令牌不存在"})

                if self.path == "/cli/session":
                    # 轮询模式：CLI 先登记会话，授权页签票时按 state 挂进待取区
                    if not gateway.cli_poll_enabled:
                        return self._send(404, {"error": {"message": "Invalid URL (POST %s)" % self.path}})
                    gateway.cli_sessions[body.get("state") or ""] = {
                        "verifier_hash": body.get("verifier_hash") or "",
                        "client_name": body.get("client_name"),
                    }
                    return self._send(200, {"success": True, "data": {"expires_in": 600}})

                if self.path == "/cli/authorize":
                    # 模拟网关：未部署授权端点时整体 404（与真实 gin 行为一致）
                    if not gateway.cli_auth_enabled:
                        return self._send(404, {"error": {"message": "Invalid URL (POST %s)" % self.path}})
                    # 模拟后端：要求用户登录态，回调地址仅允许 loopback
                    auth = self.headers.get("Authorization") or ""
                    if auth != "Bearer dash-access-token" or self.headers.get("EasyAPI-User") != "42":
                        return self._send(401, {"success": False, "message": "未登录"})
                    state = body.get("state") or ""
                    redirect_uri = body.get("redirect_uri") or ""
                    if redirect_uri:
                        host = urllib.parse.urlparse(redirect_uri).hostname
                        if not redirect_uri.startswith("http://") or host not in (
                                "127.0.0.1", "localhost", "::1"):
                            return self._send(200, {"success": False,
                                                    "message": "redirect_uri 仅允许本机回环地址"})
                    elif state not in gateway.cli_sessions:
                        # 轮询模式必须先有会话，否则票没有取件人
                        return self._send(200, {"success": False,
                                                "message": "授权会话不存在或已过期，请回到命令行重新发起登录"})
                    ticket = "tkt" + secrets.token_hex(12)
                    gateway.cli_tickets[ticket] = {
                        "user_id": 42,
                        "client_name": body.get("client_name") or "EasyAPI CLI",
                    }
                    if not redirect_uri:
                        gateway.cli_pending[state] = ticket
                        return self._send(200, {"success": True, "data": {
                            "ticket": ticket, "expires_in": 120}})
                    return self._send(200, {"success": True, "data": {
                        "ticket": ticket, "redirect_uri": redirect_uri,
                        "expires_in": 120}})

                if self.path == "/cli/exchange":
                    # 未部署授权端点时整体 404（双层探测靠这个判定「不支持」）
                    if not gateway.cli_auth_enabled:
                        return self._send(404, {"error": {"message": "Invalid URL (POST %s)" % self.path}})
                    ticket = body.get("ticket") or ""
                    payload = gateway.cli_tickets.pop(ticket, None)
                    if payload is None:
                        return self._send(200, {"success": False,
                                                "message": "授权码无效或已使用"})
                    return self._send(200, {"success": True, "data": {
                        "access_token": "dash-access-token", "user_id": 42,
                        "username": "user@example.com",
                        "client_name": payload["client_name"]}})

                if self.path == "/v1/videos":
                    return self._send(200, {"id": "task-123", "status": "queued"})
                return self._send(404, {"message": "not found"})

            def do_DELETE(self):
                gateway.requests.append(("DELETE", self.path))
                if self.path.startswith("/token/"):
                    token_id = int(self.path.split("/")[2])
                    gateway.deleted_tokens.append(token_id)
                    gateway.tokens = [t for t in gateway.tokens if t["id"] != token_id]
                    return self._send(200, {"success": True, "message": ""})
                return self._send(404, {"message": "not found"})

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d" % self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="easyapi-test-")
        self.workdir = tempfile.mkdtemp(prefix="easyapi-work-")
        self.gateway = None

    def tearDown(self):
        if self.gateway:
            self.gateway.stop()
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def run_cli(self, *argv, gateway=None, key="sk-test", extra_env=None):
        env = dict(os.environ)
        env["EASYAPI_HOME"] = self.home
        if gateway:
            env["EASYAPI_BASE_URL"] = gateway.base_url
            env["EASYAPI_PRICING_BASE_URL"] = gateway.base_url
            env["EASYAPI_CONSOLE_BASE_URL"] = gateway.base_url
        if key:
            env["EASYAPI_API_KEY"] = key
        else:
            env.pop("EASYAPI_API_KEY", None)
        env.update(extra_env or {})
        proc = subprocess.run(
            [sys.executable, SCRIPT] + list(argv),
            cwd=self.workdir, env=env, capture_output=True, text=True, timeout=120,
        )
        return proc

    def run_video_confirmed(self, *argv, gateway=None, key="sk-test", extra_env=None):
        """先跑不带 --confirm 的预览生成确认卡，再按同一参数提交。

        脚本要求 --confirm 必须对应 10 分钟内一次同请求预览；测试里封装成两步，
        避免每个用例都手写两遍参数。
        """
        preview = self.run_cli(*argv, "--json",
                               gateway=gateway, key=key, extra_env=extra_env)
        if preview.returncode != 0:
            return preview
        return self.run_cli(*argv, "--confirm", "--json",
                            gateway=gateway, key=key, extra_env=extra_env)

    def run_launcher(self, *argv, gateway=None, key="sk-test", extra_env=None,
                     skill_dir=None, cwd=None, interpreter=None, launcher="easyapi"):
        """通过 bin/<launcher> 启动器调用（验证解释器探测与目录解析）。

        interpreter 可指定 bash 可执行文件；默认用 PATH 里的 bash。
        """
        env = dict(os.environ)
        env["EASYAPI_HOME"] = self.home
        if gateway:
            env["EASYAPI_BASE_URL"] = gateway.base_url
            env["EASYAPI_PRICING_BASE_URL"] = gateway.base_url
            env["EASYAPI_CONSOLE_BASE_URL"] = gateway.base_url
        if key:
            env["EASYAPI_API_KEY"] = key
        else:
            env.pop("EASYAPI_API_KEY", None)
        env.update(extra_env or {})
        path = os.path.join(skill_dir or SKILL_DIR, "bin", launcher)
        return subprocess.run(
            [interpreter or "bash", path] + list(argv),
            cwd=cwd or self.workdir, env=env,
            capture_output=True, text=True, timeout=120,
        )

    def run_login(self, gateway, stdin_text="", *argv):
        """跑 login 子命令：stdin 非 tty，凭据从管道进（模拟 CI / 测试）。"""
        env = dict(os.environ)
        env["EASYAPI_HOME"] = self.home
        env["EASYAPI_BASE_URL"] = gateway.base_url
        env["EASYAPI_PRICING_BASE_URL"] = gateway.base_url
        env["EASYAPI_CONSOLE_BASE_URL"] = gateway.base_url
        env.pop("EASYAPI_API_KEY", None)
        return subprocess.run(
            [sys.executable, SCRIPT, "login", "--json"] + list(argv),
            cwd=self.workdir, env=env, input=stdin_text,
            capture_output=True, text=True, timeout=90,
        )

    def read_config(self):
        cfg_path = os.path.join(self.home, "config.json")
        with open(cfg_path, encoding="utf-8") as fh:
            return json.load(fh)


class TestVideoAsyncFlow(IntegrationBase):
    def test_submit_poll_download(self):
        self.gateway = MockGateway(("in_progress", "completed")).start()
        proc = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "一只猫",
            "--duration", "5", "--resolution", "720p",
            "--poll-interval", "0.05", "--timeout", "30",
            "--json", gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_id"], "task-123")
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["path"], "结果必须落盘")
        self.assertTrue(os.path.exists(payload["path"]))
        with open(payload["path"], "rb") as fh:
            self.assertEqual(fh.read(), b"FAKEVIDEOBYTES")

        # 轮询确实发生了多次（in_progress -> completed）
        self.assertGreaterEqual(self.gateway.polls, 2)

        # 提交体要带上 schema 默认值，且 resolution 已规范化
        post = [r for r in self.gateway.requests if r[0] == "POST"]
        self.assertEqual(len(post), 1)
        body = json.loads(post[0][2])
        self.assertEqual(body["model"], "mock-video")
        self.assertEqual(body["duration"], 5)
        self.assertEqual(body["resolution"], "720P")

    def test_upstream_failure_surfaces_reason(self):
        self.gateway = MockGateway(("in_progress", "failed")).start()
        proc = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "x",
            "--poll-interval", "0.05", "--timeout", "30", "--json",
            gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("内容不合规", payload["error"])

    def test_transient_failed_without_reason_keeps_polling(self):
        # 线上事故：任务刚创建（NOT_START）时网关把 status 映射成 failed，
        # 但计费仍是 pre_deducted、没有失败原因。客户端不能因此换模型重提
        # （会产生多笔真实扣费），必须继续轮询到 completed。
        self.gateway = MockGateway(("failed", "completed"),
                                   fail_message="",
                                   fail_billing_status="pre_deducted").start()
        proc = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "x",
            "--poll-interval", "0.05", "--timeout", "10", "--json",
            gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertGreaterEqual(self.gateway.polls, 2, "failed 不是终态时必须继续查")

    def test_settled_failed_without_reason_is_terminal(self):
        # 真失败会结算（settled）；即使没有 message 也要立刻报错，不能干等到超时。
        self.gateway = MockGateway(("failed",),
                                   fail_message="",
                                   fail_billing_status="settled").start()
        proc = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "x",
            "--poll-interval", "0.05", "--timeout", "30", "--json",
            gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("视频生成失败", payload["error"])

    def test_video_without_confirm_only_returns_card(self):
        # 不加 --confirm 时只输出确认卡（模型/参数/费用），绝不提交、绝不扣费。
        self.gateway = MockGateway(("completed",)).start()
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--duration", "5", "--json", gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["needs_confirmation"])
        self.assertFalse(payload["submitted"])
        self.assertEqual(payload["model"], "mock-video")
        self.assertIn("estimate_usd", payload)
        posts = [r for r in self.gateway.requests
                 if r[0] == "POST" and r[1] == "/v1/videos"]
        self.assertFalse(posts, "未确认时绝不能提交任务")

    def test_confirm_without_preview_is_rejected(self):
        # 直接 --confirm 但没有先出确认卡：必须拒绝，保证用户至少看到过一次模型/费用。
        self.gateway = MockGateway(("completed",)).start()
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--confirm", "--json", gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("确认卡", payload["error"])
        posts = [r for r in self.gateway.requests
                 if r[0] == "POST" and r[1] == "/v1/videos"]
        self.assertFalse(posts, "没有预览就不允许提交")

    def test_confirm_with_changed_params_is_rejected(self):
        # 确认卡绑定请求指纹：拿 5 秒的确认去提交 8 秒必须被拒绝。
        self.gateway = MockGateway(("completed",)).start()
        preview = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--duration", "5", "--json", gateway=self.gateway,
        )
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)

        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--duration", "8", "--confirm", "--json", gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("确认卡", payload["error"])
        posts = [r for r in self.gateway.requests
                 if r[0] == "POST" and r[1] == "/v1/videos"]
        self.assertFalse(posts, "参数变化后必须重新出卡")

    def test_recent_submission_blocks_second_video_unless_allowed(self):
        # 10 分钟内第二次提交默认拦截；用户明确同意时加 --allow-multiple 放行。
        self.gateway = MockGateway(("completed",)).start()
        first = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "x",
            "--poll-interval", "0.05", "--timeout", "10", "--json",
            gateway=self.gateway,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        second = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "x",
            "--poll-interval", "0.05", "--timeout", "10", "--json",
            gateway=self.gateway,
        )
        self.assertEqual(second.returncode, 1)
        self.assertIn("最近", second.stdout)
        posts = [r for r in self.gateway.requests
                 if r[0] == "POST" and r[1] == "/v1/videos"]
        self.assertEqual(len(posts), 1, "第二次提交必须被本地拦截")

        third = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "another",
            "--allow-multiple", "--poll-interval", "0.05", "--timeout", "10",
            "--json", gateway=self.gateway,
        )
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        posts = [r for r in self.gateway.requests
                 if r[0] == "POST" and r[1] == "/v1/videos"]
        self.assertEqual(len(posts), 2, "用户明确同意后应放行")

    def test_timeout_reports_task_id_for_manual_polling(self):
        self.gateway = MockGateway(("in_progress",)).start()
        proc = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "x",
            "--poll-interval", "0.05", "--timeout", "1", "--json",
            gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("超时", payload["error"])
        self.assertIn("task-123", payload["hint"], "超时必须给出任务 ID 以便手工续查")

    def test_no_wait_returns_immediately(self):
        self.gateway = MockGateway(("in_progress",)).start()
        proc = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "x",
            "--no-wait", "--json", gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["task_id"], "task-123")
        self.assertEqual(payload["status"], "submitted")
        self.assertEqual(self.gateway.polls, 0, "no-wait 不该轮询")

    def test_download_failure_still_reports_task_success(self):
        self.gateway = MockGateway(("completed",), download_ok=False).start()
        proc = self.run_video_confirmed(
            "video", "--model", "mock-video", "--prompt", "x",
            "--poll-interval", "0.05", "--timeout", "30", "--json",
            gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["download_error"], "下载失败要如实上报")
        self.assertTrue(payload["download_url"], "仍要给出链接")


class TestDryRunAndKeyHandling(IntegrationBase):
    def test_dry_run_works_without_any_key(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x", "--dry-run",
            gateway=self.gateway, key=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DRY RUN", proc.stdout)

    def test_dry_run_sends_no_request(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x", "--dry-run",
            gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 0)
        posts = [r for r in self.gateway.requests if r[0] == "POST"]
        self.assertEqual(posts, [], "dry-run 绝不能真的发请求")

    def test_missing_key_gives_actionable_guidance(self):
        proc = self.run_cli("quota", "--json", key=None)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("login", payload["hint"])
        self.assertIn("终端", payload["hint"], "要引导用户在自己终端执行")

    def test_validation_errors_precede_key_check(self):
        # 参数错误必须在要 key 之前报出来，否则用户配好 key 才发现参数错
        self.gateway = MockGateway().start()
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--resolution", "4k", "--dry-run", gateway=self.gateway, key=None)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("4k", proc.stdout)


class TestRefImageModes(IntegrationBase):
    """参考图（图生图）投递：两种模式 + 家族选路 + 覆盖开关。

    背景：在 /v1/images/generations 传 JSON `images` 数组是无效的（网关转发但上游不认），
    正确写法是按渠道走 multipart /v1/images/edits 或 JSON 的 image 字段。
    """

    def write_ref(self, name="ref.png", data=None):
        path = os.path.join(self.workdir, name)
        with open(path, "wb") as fh:
            fh.write(data if data is not None else TINY_PNG)
        return path

    def post_raw(self, path):
        for p, headers, raw in self.gateway.raw_requests:
            if p == path:
                return headers, raw
        return None, None

    def test_gpt_family_ref_uses_multipart_edits(self):
        self.gateway = MockGateway().start()
        ref = self.write_ref()
        proc = self.run_cli("image", "--model", "gpt-image-1", "--prompt", "改成水彩",
                            "--ref", ref, "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["endpoint"], "/v1/images/edits")
        self.assertEqual(payload["ref_mode"], "edits")

        headers, raw = self.post_raw("/v1/images/edits")
        self.assertIsNotNone(raw, "必须打到 edits 端点")
        self.assertIn("multipart/form-data; boundary=", headers.get("Content-Type", ""))
        self.assertIn(b'name="image"', raw, "单图字段名应为 image")
        self.assertIn(b'filename="ref.png"', raw)
        self.assertIn(TINY_PNG, raw, "参考图字节必须被完整上传")
        self.assertIn(b'name="model"', raw)
        self.assertIn(b"gpt-image-1", raw)
        self.assertIn(b'name="prompt"', raw)
        self.assertNotIn(b"images\"", raw, "不能再用无效的 JSON images 字段")

    def test_multiple_refs_use_bracket_field(self):
        self.gateway = MockGateway().start()
        a = self.write_ref("a.png")
        b = self.write_ref("b.png")
        proc = self.run_cli("image", "--model", "gpt-image-1", "--prompt", "融合",
                            "--ref", a, "--ref", b, "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        _headers, raw = self.post_raw("/v1/images/edits")
        self.assertEqual(raw.count(b'name="image[]"'), 2, "多图应重复 image[] 部件")
        self.assertNotIn(b'name="image"', raw)

    def test_seedream_ref_passes_url_through_in_image_mode(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli("image", "--model", "seedream-4.0", "--prompt", "换背景",
                            "--ref", "https://example.com/a.png",
                            "--ref-mode", "image", "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["endpoint"], "/v1/images/generations")
        _headers, raw = self.post_raw("/v1/images/generations")
        body = json.loads(raw)
        self.assertEqual(body["image"], "https://example.com/a.png", "URL 应原样透传")
        self.assertNotIn("images", body, "无效的 images 字段不能出现")

    def test_seedream_local_file_becomes_data_url_in_image_mode(self):
        self.gateway = MockGateway().start()
        ref = self.write_ref()
        proc = self.run_cli("image", "--model", "seedream-4.0", "--prompt", "换背景",
                            "--ref", ref, "--ref-mode", "image", "--json",
                            gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        _headers, raw = self.post_raw("/v1/images/generations")
        body = json.loads(raw)
        self.assertTrue(body["image"].startswith("data:image/png;base64,"),
                        "本地文件在 image 模式下应转 data URL")

    def test_ref_mode_override_beats_family_default(self):
        # seedream 家族默认是 image（实测 edits 会 404），显式覆盖成 edits 后必须走 edits
        self.gateway = MockGateway().start()
        ref = self.write_ref()
        proc = self.run_cli("image", "--model", "seedream-4.0", "--prompt", "x",
                            "--ref", ref, "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["ref_mode"], "image")

        proc2 = self.run_cli("image", "--model", "seedream-4.0", "--prompt", "x",
                             "--ref", ref, "--ref-mode", "edits", "--json",
                             gateway=self.gateway)
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertEqual(json.loads(proc2.stdout)["ref_mode"], "edits")

    def test_config_ref_mode_persists_and_applies(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli("config", "--ref-mode", "image", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("image", proc.stdout)

        ref = self.write_ref()
        proc2 = self.run_cli("image", "--model", "qwen-image-2.0", "--prompt", "x",
                             "--ref", ref, "--json", gateway=self.gateway)
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        # 家族默认 qwen→edits，但 config 设成 image 后应被覆盖
        self.assertEqual(json.loads(proc2.stdout)["ref_mode"], "image")

    def test_remote_ref_is_downloaded_for_edits_mode(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli("image", "--model", "gpt-image-1", "--prompt", "x",
                            "--ref", self.gateway.base_url + "/ref.png",
                            "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(any(p == "/ref.png" for p, _h, _b in self.gateway.raw_requests),
                        "edits 模式必须先下载远程参考图")
        _headers, raw = self.post_raw("/v1/images/edits")
        self.assertIn(TINY_PNG, raw, "下载到的字节要作为文件部件上传")

    def test_remote_ref_not_downloaded_in_image_mode(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli("image", "--model", "seedream-4.0", "--prompt", "x",
                            "--ref", self.gateway.base_url + "/ref.png",
                            "--ref-mode", "image", "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(any(p == "/ref.png" for p, _h, _b in self.gateway.raw_requests),
                         "image 模式直接透传 URL，不需要下载")

    def test_gemini_family_ref_is_rejected_with_guidance(self):
        self.gateway = MockGateway().start()
        ref = self.write_ref()
        proc = self.run_cli("image", "--model", "gemini-2.5-flash-image", "--prompt", "x",
                            "--ref", ref, "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("不支持", payload["error"])
        self.assertIn("chat", payload["hint"], "要指到多模态对话这条路")
        self.assertFalse(any(p.startswith("/v1/images") for p, _h, _b in self.gateway.raw_requests),
                         "不该发出任何生图请求")

    def test_missing_ref_file_is_rejected_before_request(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli("image", "--model", "gpt-image-1", "--prompt", "x",
                            "--ref", os.path.join(self.workdir, "nope.png"),
                            "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("不是存在的文件", proc.stdout)
        self.assertFalse(any(p.startswith("/v1/images") for p, _h, _b in self.gateway.raw_requests))

    def test_dry_run_reports_ref_mode_without_sending(self):
        self.gateway = MockGateway().start()
        ref = self.write_ref()
        proc = self.run_cli("image", "--model", "gpt-image-1", "--prompt", "x",
                            "--ref", ref, "--dry-run", "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["endpoint"], "/v1/images/edits")
        self.assertEqual(payload["ref_files"], ["ref.png"])
        self.assertFalse(any(p.startswith("/v1/images") for p, _h, _b in self.gateway.raw_requests),
                         "dry-run 不能发请求")

    def test_plain_generation_unchanged_without_ref(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli("image", "--model", "seedream-4.5", "--prompt", "一只猫",
                            "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["endpoint"], "/v1/images/generations")
        self.assertIsNone(payload["ref_mode"])
        _headers, raw = self.post_raw("/v1/images/generations")
        body = json.loads(raw)
        self.assertNotIn("image", body)
        self.assertNotIn("images", body)


class TestEncodeMultipart(unittest.TestCase):
    """multipart 编码的纯逻辑测试（不联网）。"""

    def test_single_file_and_fields(self):
        ctype, body = mod.encode_multipart(
            {"model": "gpt-image-1", "n": 2, "stream": False, "skip": None},
            [("image", "a.png", b"PNGDATA")])
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        boundary = ctype.split("boundary=")[1]
        self.assertIn(("--%s\r\n" % boundary).encode(), body)
        self.assertTrue(body.endswith(("--%s--\r\n" % boundary).encode()))
        self.assertIn(b'name="model"\r\n\r\ngpt-image-1', body)
        self.assertIn(b'name="n"\r\n\r\n2', body)
        self.assertIn(b'name="stream"\r\n\r\nfalse', body)
        self.assertNotIn(b"skip", body, "None 值应跳过")
        self.assertIn(b'name="image"; filename="a.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(b"PNGDATA", body)

    def test_repeated_same_field_name(self):
        _ctype, body = mod.encode_multipart(
            {"model": "m"}, [("image[]", "a.png", b"A"), ("image[]", "b.png", b"B")])
        self.assertEqual(body.count(b'name="image[]"'), 2)

    def test_unknown_extension_gets_octet_stream(self):
        _ctype, body = mod.encode_multipart({}, [("image", "x.unknownext", b"Z")])
        self.assertIn(b"application/octet-stream", body)


class TestWrongHostIntegration(IntegrationBase):
    """base_url 指错主机时，用户要看到能照着修的指引。"""

    def test_models_reports_the_wrong_host_clearly(self):
        self.gateway = MockGateway(wrong_host=True).start()
        proc = self.run_cli("models", "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("不是 EasyAPI 网关", payload["hint"])
        self.assertIn(mod.DEFAULT_GATEWAY_BASE, payload["hint"])

    def test_image_generation_reports_the_wrong_host_clearly(self):
        self.gateway = MockGateway(wrong_host=True).start()
        # 参数要合法，确保走到发请求那一步才失败
        proc = self.run_cli(
            "image", "--model", "seedream-4.5", "--prompt", "x", "--json",
            gateway=self.gateway,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("不是 EasyAPI 网关", payload["hint"])


class TestCaching(IntegrationBase):
    def test_pricing_is_cached_across_calls(self):
        self.gateway = MockGateway().start()
        for _ in range(3):
            proc = self.run_cli("models", "--json", gateway=self.gateway)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        pricing_calls = [r for r in self.gateway.requests if r[1].startswith("/pricing")]
        self.assertEqual(len(pricing_calls), 1,
                         "第二次起必须命中缓存，不能每次都打接口")

    def test_refresh_bypasses_cache(self):
        self.gateway = MockGateway().start()
        self.run_cli("models", "--json", gateway=self.gateway)
        self.run_cli("models", "--json", "--refresh", gateway=self.gateway)
        pricing_calls = [r for r in self.gateway.requests if r[1].startswith("/pricing")]
        self.assertEqual(len(pricing_calls), 2)

    def test_detail_cache_is_reused_for_schema(self):
        self.gateway = MockGateway().start()
        for _ in range(3):
            proc = self.run_cli("schema", "mock-video", "--json", gateway=self.gateway)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        detail_calls = [r for r in self.gateway.requests if r[1].startswith("/pricing/detail")]
        self.assertEqual(len(detail_calls), 1, "单模型 schema 也要缓存")

    def test_schema_falls_back_to_snapshot_when_offline(self):
        # 指向一个不存在的端口：线上不可达时应回退内置快照而不是崩掉
        env = {"EASYAPI_BASE_URL": "http://127.0.0.1:9",
               "EASYAPI_PRICING_BASE_URL": "http://127.0.0.1:9",
               "EASYAPI_CONSOLE_BASE_URL": "http://127.0.0.1:9"}
        proc = self.run_cli("schema", "seedream-4.5", "--json",
                            key=None, extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["params"], "离线时必须能用内置快照")
        self.assertEqual(payload["schema_source"], "snapshot")


class TestConfigCommand(IntegrationBase):
    def test_defaults_are_persisted_and_used(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli("config", "--default-video-model", "mock-video",
                            gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        # 不传 --model 也应该能跑，且用上刚设的默认值
        proc = self.run_cli("video", "--prompt", "x", "--dry-run", "--json",
                            gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["model"], "mock-video")
        self.assertEqual(payload["model_source"], "config")

    def test_builtin_default_models_are_used_when_nothing_configured(self):
        self.gateway = MockGateway().start()

        proc = self.run_cli("image", "--prompt", "x", "--dry-run", "--json",
                            gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["model"], "qwen-image-3.0-pro")
        self.assertEqual(payload["model_source"], "builtin")

        proc = self.run_cli("video", "--prompt", "x", "--dry-run", "--json",
                            gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["model"], "wan3.0-video")
        self.assertEqual(payload["model_source"], "builtin")

    def test_empty_string_restores_builtin_default(self):
        self.gateway = MockGateway().start()
        self.run_cli("config", "--default-image-model", "mock-image",
                     gateway=self.gateway)
        self.run_cli("config", "--default-image-model", "",
                     gateway=self.gateway)

        proc = self.run_cli("config", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("qwen-image-3.0-pro（内置默认）", proc.stdout)


class TestLoginPasswordFlow(IntegrationBase):
    """密码登录 → 自动创建/复用 sk- key → 只落盘 key。"""

    STDIN = "user@example.com\nsecret-pass\n"

    def test_creates_key_when_none_exists(self):
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, self.STDIN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        self.assertEqual(self.gateway.created_tokens, ["easyapi-skill"],
                         "没有同名令牌时必须自动创建")
        cfg = self.read_config()
        self.assertTrue(cfg["api_key"].startswith("sk-"),
                        "落盘的必须是带 sk- 前缀的完整 key")
        self.assertNotIn("dash-access-token", open(
            os.path.join(self.home, "config.json"), encoding="utf-8").read(),
            "访问令牌绝不能落盘，它比 sk- key 权限大得多")
        self.assertEqual(cfg["api_key"], "sk-" + self.gateway.tokens[0]["key"])

        # 建完要验证：带 sk- key 调了一次额度接口
        self.assertTrue(any(r[1].startswith("/usage/token") for r in self.gateway.requests))

    def test_reuses_existing_enabled_key_without_creating(self):
        existing = {"id": 7, "name": "easyapi-skill", "status": 1, "key": "existingraw" * 3}
        self.gateway = MockGateway(tokens=[existing]).start()
        proc = self.run_login(self.gateway, self.STDIN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        self.assertEqual(self.gateway.created_tokens, [],
                         "已有同名启用令牌时不能再建，否则反复登录会堆积")
        cfg = self.read_config()
        self.assertEqual(cfg["api_key"], "sk-existingrawexistingrawexistingraw")

    def test_two_fa_account_completes_login(self):
        self.gateway = MockGateway(require_2fa=True).start()
        proc = self.run_login(self.gateway, self.STDIN + "123456\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        cfg = self.read_config()
        self.assertTrue(cfg["api_key"].startswith("sk-"))

    def test_wrong_two_fa_code_fails_with_retry_hint(self):
        self.gateway = MockGateway(require_2fa=True, two_fa_code="999999").start()
        proc = self.run_login(self.gateway, self.STDIN + "123456\n")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("验证码", payload["error"])

    def test_wrong_password_is_actionable(self):
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, "user@example.com\nnope\n")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("登录失败", payload["error"])

    def test_disabled_same_name_key_is_reported_not_flipped(self):
        disabled = {"id": 7, "name": "easyapi-skill", "status": 2, "key": "disabled" * 4}
        self.gateway = MockGateway(tokens=[disabled]).start()
        proc = self.run_login(self.gateway, self.STDIN)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("禁用", payload["error"])
        self.assertIn("不会擅自", payload["hint"], "必须明确说脚本不会动用户数据")
        self.assertEqual(self.gateway.created_tokens, [], "也不能悄悄另建一个")

    def test_max_tokens_limit_is_actionable(self):
        self.gateway = MockGateway(max_tokens=True).start()
        proc = self.run_login(self.gateway, self.STDIN)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("最大令牌数量", payload["error"])
        self.assertIn("--key-name", payload["hint"], "要给用户指一条出路")

    def test_captcha_enabled_blocks_password_login_upfront(self):
        self.gateway = MockGateway(captcha=True).start()
        proc = self.run_login(self.gateway, self.STDIN)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("人机验证", payload["error"])
        # 验证码开着但平台支持浏览器授权 → 指向 --browser（真实最优解）
        self.assertIn("--browser", payload["hint"])
        # 探测到验证码就直接拒绝，不能把密码读进来再失败
        self.assertEqual(proc.stdout.count("user@example.com"), 0)

    def test_captcha_on_and_no_cli_authorize_points_to_access_token(self):
        # 线上现状：滑块验证开启 + /cli/* 未部署 → 唯一可行路径是 --access-token
        self.gateway = MockGateway(captcha=True, cli_auth_enabled=False).start()
        proc = self.run_login(self.gateway, self.STDIN)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("--access-token", payload["hint"], "要指向当前唯一可行路径")
        self.assertNotIn("--browser", payload["hint"], "不能把人引向走不通的路")

    def test_custom_key_name_is_respected(self):
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, self.STDIN, "--key-name", "my-own-key")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.gateway.created_tokens, ["my-own-key"])

    def test_existing_other_keys_pause_for_user_choice(self):
        # 账号下已有别的可用令牌但没有 easyapi-skill：不能静默新建，要停下来问用户
        other = {"id": 9, "name": "我的旧令牌", "status": 1, "key": "old" + "x" * 40}
        self.gateway = MockGateway(tokens=[other]).start()
        proc = self.run_login(self.gateway, self.STDIN)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("已有 1 个可用令牌", payload["error"])
        self.assertIn("--reuse-key", payload["hint"])
        self.assertIn("--create-new", payload["hint"])
        self.assertIn("我的旧令牌", payload["hint"], "要把已有令牌列出来给用户看")
        self.assertEqual(self.gateway.created_tokens, [], "没问用户前绝不能新建")

    def test_create_new_flag_overrides_existing_keys(self):
        other = {"id": 9, "name": "我的旧令牌", "status": 1, "key": "old" + "x" * 40}
        self.gateway = MockGateway(tokens=[other]).start()
        proc = self.run_login(self.gateway, self.STDIN, "--create-new")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.gateway.created_tokens, ["easyapi-skill"],
                         "用户明确选新建时才创建")

    def test_reuse_key_by_name(self):
        other = {"id": 9, "name": "我的旧令牌", "status": 1, "key": "reuse" + "y" * 40}
        self.gateway = MockGateway(tokens=[other]).start()
        proc = self.run_login(self.gateway, self.STDIN, "--reuse-key", "我的旧令牌")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.gateway.created_tokens, [], "复用不该新建")
        self.assertEqual(self.read_config()["api_key"], "sk-reuse" + "y" * 40)

    def test_reuse_key_by_id(self):
        other = {"id": 9, "name": "我的旧令牌", "status": 1, "key": "reuse" + "z" * 40}
        self.gateway = MockGateway(tokens=[other]).start()
        proc = self.run_login(self.gateway, self.STDIN, "--reuse-key", "9")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.read_config()["api_key"], "sk-reuse" + "z" * 40)

    def test_reuse_disabled_key_rejected(self):
        other = {"id": 9, "name": "停用的", "status": 2, "key": "d" * 44}
        self.gateway = MockGateway(tokens=[other]).start()
        proc = self.run_login(self.gateway, self.STDIN, "--reuse-key", "停用的")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("禁用", proc.stdout)

    def test_login_reports_account_balance_via_user_self(self):
        # 不限量令牌的 /usage/token 余额无意义，登录收尾必须用 access_token 查账号余额
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, self.STDIN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        # /user/self 返回 quota=8000000 → 8000000/500000 = $16
        self.assertAlmostEqual(payload["account_balance_usd"], 16.0)
        self.assertTrue(any(r[1].startswith("/user/self") for r in self.gateway.requests),
                        "登录时要用 access_token 查一次账号余额")


class TestLoginAlternativePaths(IntegrationBase):
    def test_access_token_login_never_asks_for_password(self):
        self.gateway = MockGateway().start()
        # stdin 第一行访问令牌，第二行用户 ID
        proc = self.run_login(self.gateway, "dash-access-token\n42\n", "--access-token")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        login_calls = [r for r in self.gateway.requests if r[1] == "/user/login"]
        self.assertEqual(login_calls, [], "访问令牌路径绝不能碰登录接口")
        cfg = self.read_config()
        self.assertTrue(cfg["api_key"].startswith("sk-"))
        self.assertNotIn("dash-access-token", open(
            os.path.join(self.home, "config.json"), encoding="utf-8").read())

    def test_access_token_mode_rejects_non_numeric_user_id(self):
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, "dash-access-token\nabc\n", "--access-token")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("数字", proc.stdout)

    def test_api_key_mode_skips_account_entirely(self):
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, "sk-manual-key-123456\n", "--api-key")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        login_calls = [r for r in self.gateway.requests if r[1] == "/user/login"]
        self.assertEqual(login_calls, [])
        create_calls = [r for r in self.gateway.requests if r[1] == "/token"]
        self.assertEqual(create_calls, [], "直接粘贴 key 不应该去创建令牌")
        cfg = self.read_config()
        self.assertEqual(cfg["api_key"], "sk-manual-key-123456")

    def test_no_echo_of_secret_in_output(self):
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, "user@example.com\nsecret-pass\n")
        combined = proc.stdout + proc.stderr
        self.assertNotIn("secret-pass", combined, "密码不能出现在任何输出里")
        self.assertNotIn("dash-access-token", combined, "访问令牌不能出现在任何输出里")


class _ProcResult:
    """subprocess.run 结果的最小替身（run_browser_login 用 Popen + stderr 落盘）。"""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""


class TestBrowserLoginFlow(IntegrationBase):
    """浏览器授权（loopback）：本地监听 → 授权页 → 回调 → 兑换 → 自动建 key。

    测试不开真浏览器：脚本用 EASYAPI_NO_BROWSER=1 只打印授权地址，
    测试用 requests 库模拟授权页的两次调用（authorize + 本地回调）。
    """

    def run_browser_login(self, gateway, *argv, stdin_text="", on_stderr=None):
        """跑 --browser 登录。stderr 实时落文件，供测试在进程存活期间读取授权地址。

        不能用 capture_output：它要等进程结束才能拿到输出，而进程在等测试
        模拟浏览器回调 —— 两边互相等就死锁了。on_stderr(text) 在每次读到
        新内容时被回调，测试用它拿到授权地址后去模拟浏览器完成授权。
        """
        env = dict(os.environ)
        env["EASYAPI_HOME"] = self.home
        env["EASYAPI_BASE_URL"] = gateway.base_url
        env["EASYAPI_PRICING_BASE_URL"] = gateway.base_url
        env["EASYAPI_CONSOLE_BASE_URL"] = gateway.base_url
        env["EASYAPI_NO_BROWSER"] = "1"
        env.pop("EASYAPI_API_KEY", None)
        # PYTHONUNBUFFERED=1 让子进程的 stderr 不经块缓冲，测试才能实时读到
        env["PYTHONUNBUFFERED"] = "1"
        stderr_path = os.path.join(self.workdir, "login-stderr.log")
        with open(stderr_path, "w", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                [sys.executable, SCRIPT, "login", "--browser", "--json"] + list(argv),
                cwd=self.workdir, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=stderr_file, text=True,
            )
            try:
                if on_stderr is not None:
                    # 轮询 stderr 文件直到进程退出，把增量喂给 on_stderr
                    seen = 0
                    while proc.poll() is None:
                        time.sleep(0.15)
                        try:
                            with open(stderr_path, encoding="utf-8") as fh:
                                text = fh.read()
                        except OSError:
                            text = ""
                        if len(text) > seen:
                            on_stderr(text[seen:])
                            seen = len(text)
                stdout, _ = proc.communicate(input=stdin_text, timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, _ = proc.communicate()
                raise
        with open(stderr_path, encoding="utf-8") as fh:
            stderr_text = fh.read()
        return _ProcResult(proc.returncode, stdout, stderr_text)

    @staticmethod
    def authorize_url_from_output(text):
        match = re.search(r"授权页地址：(\S+)", text)
        return match.group(1) if match else None

    def complete_authorization(self, gateway, authorize_url):
        """模拟用户在授权页点「授权」：带登录态调 /cli/authorize，再访问回调 URL。"""
        parsed = urllib.parse.urlparse(authorize_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))

        # 1) 授权页持用户登录态签发票据（模拟前端 http.post，自动带鉴权头）
        status, payload, _ = mod.http_json(
            "POST", gateway.base_url + "/cli/authorize",
            headers={"Authorization": "Bearer dash-access-token", "EasyAPI-User": "42"},
            body={"client_name": params.get("client_name", ""),
                  "redirect_uri": params.get("redirect_uri", ""),
                  "state": params.get("state", "")})
        assert status == 200 and payload.get("success"), payload
        ticket = payload["data"]["ticket"]

        # 2) 授权页 302 回本地监听（模拟浏览器跟随重定向）
        callback = "%s?ticket=%s&state=%s" % (
            params["redirect_uri"], urllib.parse.quote(ticket),
            urllib.parse.quote(params["state"]))
        status, _headers, raw = mod.http_request("GET", callback, timeout=30)
        assert status == 200, (status, raw[:200])
        return ticket

    def test_full_browser_flow_provisions_key(self):
        self.gateway = MockGateway().start()
        authorized = []

        def on_stderr(text):
            # 进程存活期间拿到授权地址后，立刻模拟浏览器完成授权
            url = self.authorize_url_from_output(text)
            if url and not authorized:
                authorized.append(url)
                self.complete_authorization(self.gateway, url)

        proc = self.run_browser_login(self.gateway, on_stderr=on_stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(authorized, "必须打印授权页地址供无浏览器环境使用")

        authorize_url = self.authorize_url_from_output(proc.stderr)
        self.assertIn("/cli/authorize", authorize_url)
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A", authorize_url)
        # 授权页必须用控制台域名打开（前端路由），不能用网关域名（那里只有 POST API）
        self.assertTrue(authorize_url.startswith(self.gateway.base_url + "/cli/authorize"),
                        "授权页地址应指向控制台域名，实际：%s" % authorize_url)

        cfg = self.read_config()
        self.assertTrue(cfg["api_key"].startswith("sk-"))
        self.assertEqual(self.gateway.created_tokens, ["easyapi-skill"])

        # 登录令牌不落盘
        raw = open(os.path.join(self.home, "config.json"), encoding="utf-8").read()
        self.assertNotIn("dash-access-token", raw)

    def test_backend_without_cli_authorize_degrades_clearly(self):
        # 标志缺失 且 端点不存在（404）→ 明确降级
        self.gateway = MockGateway(cli_auth_enabled=False).start()
        proc = self.run_browser_login(self.gateway)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertIn("不支持网页授权", payload["error"])
        self.assertIn("终端", payload["hint"], "要指到可行的替代路径")

    def test_deployed_endpoints_without_flag_are_detected(self):
        # 线上真实发生过：端点已部署但 app-config 漏了标志。
        # 端点开（cli_auth_enabled=True 才有 /cli/* 路由）+ 标志缺失（传空 config），
        # 第二层探测必须发现端点并放行 —— 这是防「部署了却永远走不通」的关键
        self.gateway = MockGateway(cli_auth_enabled=True).start()
        self.assertTrue(mod._cli_authorize_supported(self.gateway.base_url, {}))

    def test_probe_rejects_404_endpoints(self):
        # 端点真不存在（404）→ 探测必须返回 False，不能误报支持
        self.gateway = MockGateway(cli_auth_enabled=False).start()
        self.assertFalse(mod._cli_authorize_supported(self.gateway.base_url, {}))

    def test_state_mismatch_is_rejected(self):
        self.gateway = MockGateway().start()
        attempted = []

        def on_stderr(text):
            url = self.authorize_url_from_output(text)
            if url and not attempted:
                attempted.append(url)
                # 用错误的 state 访问回调，模拟另一次授权的回调串进来
                parsed = urllib.parse.urlparse(url)
                params = dict(urllib.parse.parse_qsl(parsed.query))
                callback = "%s?ticket=fake&state=wrong-state" % params["redirect_uri"]
                status, _headers, raw = mod.http_request("GET", callback, timeout=30)
                self.assertEqual(status, 200)
                self.assertIn("state", raw.decode("utf-8", "replace"),
                              "回调页要明确报 state 校验失败")

        proc = self.run_browser_login(self.gateway, on_stderr=on_stderr)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("state", payload["error"], "脚本要把 state 不匹配如实报出来")

    def test_manual_mode_pastes_ticket(self):
        self.gateway = MockGateway().start()
        # manual 模式：脚本打印授权地址后等待 stdin 粘贴授权码
        env = dict(os.environ)
        env["EASYAPI_HOME"] = self.home
        env["EASYAPI_BASE_URL"] = self.gateway.base_url
        env["EASYAPI_PRICING_BASE_URL"] = self.gateway.base_url
        env["EASYAPI_CONSOLE_BASE_URL"] = self.gateway.base_url
        env.pop("EASYAPI_API_KEY", None)

        # 先起进程拿到授权地址，再完成授权拿到票据，最后把票据喂给 stdin
        proc = subprocess.Popen(
            [sys.executable, SCRIPT, "login", "--browser", "--manual", "--json"],
            cwd=self.workdir, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            authorize_url = None
            deadline = time.time() + 30
            # stderr 是块缓冲的，Popen 下读不到中间输出；改为轮询 gateway 收到的请求
            while time.time() < deadline:
                auth_reqs = [r for r in self.gateway.requests
                             if r[0] == "GET" and r[1].startswith("/app-config")]
                if auth_reqs:
                    break
                time.sleep(0.1)
            # 从脚本进程的命令行拿不到 URL，改从 gateway 侧构造：
            # manual 模式下脚本先打印地址再读 stdin，这里直接模拟完整授权。
            # 由于 stderr 块缓冲无法可靠读出地址，改用 --browser 非手动路径覆盖主流程，
            # manual 只验证「粘贴票据能走通兑换」：直接调 authorize 拿票。
            status, payload, _ = mod.http_json(
                "POST", self.gateway.base_url + "/cli/authorize",
                headers={"Authorization": "Bearer dash-access-token", "EasyAPI-User": "42"},
                body={"client_name": "EasyAPI CLI",
                      "redirect_uri": "http://127.0.0.1:1/callback",
                      "state": "manual-test-state"})
            assert payload.get("success"), payload
            ticket = payload["data"]["ticket"]
            out, err = proc.communicate(input=ticket + "\n", timeout=60)
        except Exception:
            proc.kill()
            raise

        self.assertEqual(proc.returncode, 0, out + err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        cfg = self.read_config()
        self.assertTrue(cfg["api_key"].startswith("sk-"))

    def test_replayed_ticket_is_rejected(self):
        self.gateway = MockGateway().start()
        status, payload, _ = mod.http_json(
            "POST", self.gateway.base_url + "/cli/authorize",
            headers={"Authorization": "Bearer dash-access-token", "EasyAPI-User": "42"},
            body={"client_name": "x", "redirect_uri": "http://127.0.0.1:1/callback",
                  "state": "s"})
        ticket = payload["data"]["ticket"]

        # 第一次兑换成功，第二次必须失败（一次性）
        first = mod.http_json("POST", self.gateway.base_url + "/cli/exchange",
                              body={"ticket": ticket})
        second = mod.http_json("POST", self.gateway.base_url + "/cli/exchange",
                               body={"ticket": ticket})
        self.assertTrue(first[1].get("success"))
        self.assertFalse(second[1].get("success"), "票据必须一次性")


# ---------------------------------------------------------------------------
# 3. 打包契约（ZCode 插件 + 市场清单）
# ---------------------------------------------------------------------------


class TestPluginPackaging(unittest.TestCase):
    """钉住插件/市场清单的结构。

    ZCode 添加本地市场时要求市场根目录下有 marketplace.json，插件目录下有
    .zcode-plugin/plugin.json；任何一处不合规都会导致「Marketplace manifest not found」
    或装上之后技能不出现。这组测试就是防止这类打包事故。
    """

    MARKET_PATH = os.path.join(REPO_ROOT, "marketplace.json")

    def setUp(self):
        with open(self.MARKET_PATH, encoding="utf-8") as fh:
            self.market = json.load(fh)

    def test_marketplace_manifest_is_valid_json_with_name_and_plugins(self):
        self.assertTrue(self.market.get("name"), "市场必须有 name")
        self.assertIsInstance(self.market.get("plugins"), list)
        self.assertTrue(self.market["plugins"], "市场至少要有一个插件条目")

    def test_marketplace_name_is_public_name(self):
        # 公开市场的名字必须和仓库一致；dev- 前缀是早先本地测试用的，发布后不能再出现
        self.assertEqual(self.market["name"], "easyapi-skill",
                         "公开市场名必须是 easyapi-skill")
        self.assertFalse(self.market["name"].startswith("dev-"),
                         "本地测试用的 dev- 前缀不能出现在正式市场清单里")

    def test_each_entry_has_required_fields(self):
        for entry in self.market["plugins"]:
            for field in ("name", "source", "version", "description"):
                self.assertIn(field, entry, "插件条目缺少 %s" % field)

    def test_source_resolves_and_stays_inside_market_root(self):
        for entry in self.market["plugins"]:
            target = os.path.normpath(
                os.path.join(REPO_ROOT, entry["source"]))
            self.assertTrue(
                os.path.commonpath([os.path.abspath(target), os.path.abspath(REPO_ROOT)])
                == os.path.abspath(REPO_ROOT),
                "source 必须落在市场根目录内：%s" % entry["source"])
            self.assertTrue(os.path.isdir(target),
                            "source 指向的目录不存在：%s" % target)

    def test_plugin_manifest_exists_and_matches_directory_name(self):
        for entry in self.market["plugins"]:
            plugin_dir = os.path.join(REPO_ROOT, entry["source"])
            manifest_path = os.path.join(plugin_dir, ".zcode-plugin", "plugin.json")
            self.assertTrue(os.path.isfile(manifest_path),
                            "缺少插件清单：%s" % manifest_path)

            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)

            self.assertEqual(manifest["name"], os.path.basename(plugin_dir),
                             "插件清单的 name 必须与目录名一致")
            self.assertEqual(manifest["name"], entry["name"],
                             "市场条目与插件清单的 name 必须一致")
            self.assertTrue(manifest.get("version"))
            self.assertTrue(manifest.get("description"))

    def test_declared_skills_path_exists_and_holds_the_skills(self):
        expected = ("easyapi-studio", "easyapi-auth")
        for entry in self.market["plugins"]:
            plugin_dir = os.path.join(REPO_ROOT, entry["source"])
            with open(os.path.join(plugin_dir, ".zcode-plugin", "plugin.json"),
                      encoding="utf-8") as fh:
                manifest = json.load(fh)

            skills_dir = os.path.normpath(os.path.join(plugin_dir, manifest["skills"]))
            self.assertTrue(os.path.isdir(skills_dir),
                            "清单里声明的 skills 目录不存在：%s" % skills_dir)

            for name in expected:
                skill_md = os.path.join(skills_dir, name, "SKILL.md")
                self.assertTrue(os.path.isfile(skill_md),
                                "插件规范要求 skills/<name>/SKILL.md，缺失：%s" % skill_md)

    def test_skill_frontmatter_name_matches_its_directory(self):
        for skill_dir in (SKILL_DIR, AUTH_SKILL_DIR):
            frontmatter = self._frontmatter(os.path.join(skill_dir, "SKILL.md"))
            self.assertEqual(frontmatter.get("name"), os.path.basename(skill_dir),
                             "SKILL.md 的 name 必须与所在目录名一致：%s" % skill_dir)
            self.assertTrue(frontmatter.get("description"))
            self.assertLess(len(frontmatter["description"]), 1024,
                            "description 超过 1024 字符会被丢弃")

    def test_version_is_consistent_between_manifest_and_marketplace(self):
        for entry in self.market["plugins"]:
            plugin_dir = os.path.join(REPO_ROOT, entry["source"])
            with open(os.path.join(plugin_dir, ".zcode-plugin", "plugin.json"),
                      encoding="utf-8") as fh:
                manifest = json.load(fh)
            self.assertEqual(manifest["version"], entry["version"],
                             "插件清单与市场条目的版本必须同步，否则更新不生效")

    def test_skill_script_is_reachable_from_skill_dir(self):
        # 插件安装后脚本靠相对 SKILL.md 的路径调用，路径写错技能就废了
        self.assertTrue(os.path.isfile(SCRIPT))
        self.assertTrue(SCRIPT.startswith(SKILL_DIR))

    @staticmethod
    def _frontmatter(path):
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", raw, re.S)
        if not match:
            return {}
        out = {}
        for line in match.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                key, value = line.split(":", 1)
                out[key.strip()] = value.strip().strip('"')
        return out


# ---------------------------------------------------------------------------
# 3. 参数 schema：转换、合并、来源优先级、端点解析
# ---------------------------------------------------------------------------


class TestSchemaConversion(unittest.TestCase):
    """OpenAPI 3.1 与脚本内部结构的互转（线上接口用前者，快照用后者）。"""

    def test_restores_required_array_and_bounds(self):
        schema = {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string", "maxLength": 100},
                "duration": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "input_references": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"url": {"type": "string"}}},
                },
            },
        }
        props = mod.openapi_schema_to_props(schema)
        self.assertTrue(props["prompt"]["required"])
        self.assertEqual(props["prompt"]["max_length"], 100)
        self.assertNotIn("required", props["duration"])
        self.assertEqual(props["duration"]["min"], 1)
        self.assertEqual(props["duration"]["max"], 10)
        self.assertEqual(
            props["input_references"]["items"]["properties"]["url"]["type"], "string")

    def test_empty_schema_returns_none(self):
        self.assertIsNone(mod.openapi_schema_to_props(None))
        self.assertIsNone(mod.openapi_schema_to_props({"type": "object", "properties": {}}))

    def test_merge_prefers_primary_and_reports_added(self):
        primary = {
            "prompt": {"type": "string", "required": True},
            "resolution": {"type": "string", "enum": ["480P"]},
        }
        snapshot = {
            "resolution": {"type": "string", "enum": ["720P", "1080P"]},
            "generate_audio": {"type": "boolean", "default": False},
        }
        merged, added = mod.merge_props(primary, snapshot)
        self.assertEqual(merged["resolution"]["enum"], ["480P"], "同名属性必须由线上说了算")
        self.assertFalse(merged["generate_audio"]["default"])
        self.assertEqual(added, ["generate_audio"])

    def test_endpoint_matching_is_operation_scoped(self):
        chat = {"type": "openai", "path": "/v1/chat/completions"}
        images = {"type": "image-generation", "path": "/v1/images/generations"}
        videos = {"type": "api-video", "path": "/v1/videos"}
        self.assertFalse(mod._endpoint_matches_kind(chat, "image"))
        self.assertTrue(mod._endpoint_matches_kind(images, "image"))
        self.assertFalse(mod._endpoint_matches_kind(images, "video"))
        self.assertTrue(mod._endpoint_matches_kind(videos, "video"))


SCHEMA_BUNDLE = {
    "items": [{
        "model": "mock-video",
        "category": "video",
        "endpoints": [{"type": "api-video", "path": "/v1/videos", "method": "POST"}],
        "request": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string"},
                "resolution": {"type": "string", "enum": ["480P", "720P"], "default": "480P"},
            },
        },
    }],
    "pricing_version": "v-test",
    "schema_version": "v-test",
}


class TestSchemaSources(IntegrationBase):
    """四级来源与逐字段合并：拿哪一级、怎么合并、端点怎么选。"""

    def test_bundle_is_authoritative(self):
        self.gateway = MockGateway(schema_bundle=SCHEMA_BUNDLE).start()
        proc = self.run_cli("schema", "mock-video", "--json", gateway=self.gateway, key=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema_source"], "schema-bundle")
        self.assertEqual(payload["category"], "video")
        self.assertEqual(payload["submit_endpoint"]["path"], "/v1/videos")
        self.assertEqual(payload["required"], ["prompt"])
        self.assertEqual(payload["example"]["prompt"], "一只猫在草地上奔跑")
        self.assertEqual(payload["params"]["resolution"]["enum"], ["480P", "720P"])

    def test_single_schema_used_when_bundle_missing(self):
        single = {
            "model": "mock-video", "category": "video",
            "endpoints": [{"type": "api-video", "path": "/v1/videos", "method": "POST"}],
            "request": {"type": "object", "required": ["prompt"],
                        "properties": {"prompt": {"type": "string"}}},
            "example": {"model": "mock-video", "prompt": "服务端示例"},
        }
        self.gateway = MockGateway(schema_single={"mock-video": single}).start()
        proc = self.run_cli("schema", "mock-video", "--json", gateway=self.gateway, key=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema_source"], "schema")
        self.assertEqual(payload["example"]["prompt"], "服务端示例")

    def test_partial_live_detail_merges_snapshot(self):
        # mock 的 /pricing/detail 只声明 prompt/duration/resolution，Kling 快照有更多字段
        self.gateway = MockGateway().start()
        proc = self.run_cli("schema", "Kling-3.0", "--json", gateway=self.gateway, key=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["category"], "video", "不在 /pricing 里也要能推断分类")
        self.assertEqual(payload["schema_sources"], ["live-detail", "snapshot"])
        self.assertEqual(payload["params"]["resolution"]["enum"], ["720P", "1080P"],
                         "同名属性取线上")
        self.assertIn("generate_audio", payload["params"], "缺失属性由快照补齐")
        self.assertTrue(any("内置快照补全" in note for note in payload["notes"]))

    def test_snapshot_only_when_no_live_data(self):
        self.gateway = MockGateway(detail_models=()).start()
        proc = self.run_cli("schema", "seedance-2.0", "--json", gateway=self.gateway, key=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema_source"], "snapshot")
        self.assertEqual(payload["category"], "video")


class TestEnvAndRelocatable(IntegrationBase):
    """启动器：解释器探测、skill 目录解析、换目录后仍可用。"""

    def setUp(self):
        super().setUp()
        if not shutil.which("bash"):
            self.skipTest("需要 bash 才能验证 bin/easyapi 启动器")

    def test_env_reports_paths_and_snapshot(self):
        proc = self.run_launcher("env", "--json", key=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(os.path.normcase(os.path.realpath(payload["skill_dir"])),
                         os.path.normcase(os.path.realpath(SKILL_DIR)))
        self.assertGreaterEqual(payload["snapshot_models"], 1)
        self.assertTrue(payload["python_version"])

    def test_env_detects_schema_endpoint(self):
        self.gateway = MockGateway(schema_bundle=SCHEMA_BUNDLE).start()
        proc = self.run_launcher("env", "--network", "--json", gateway=self.gateway, key=None)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema_endpoint"], "supported")
        self.assertTrue(payload["pricing_ok"])

    def test_launcher_survives_copy_and_foreign_cwd(self):
        target = os.path.join(tempfile.mkdtemp(prefix="easyapi-copy-"), "easyapi")
        shutil.copytree(SKILL_DIR, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elsewhere = tempfile.mkdtemp(prefix="easyapi-cwd-")
        proc = self.run_launcher("env", "--json", key=None,
                                 skill_dir=target, cwd=elsewhere)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(os.path.normcase(os.path.realpath(payload["skill_dir"])),
                         os.path.normcase(os.path.realpath(target)))


# ---------------------------------------------------------------------------
# 4. 视频引用组装与参数覆盖（原测试缺这两块）
# ---------------------------------------------------------------------------


class TestVideoReferenceAssembly(IntegrationBase):
    def test_frames_and_references_become_input_references(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--first-frame", "https://example.com/a.png",
            "--last-frame", "https://example.com/b.png",
            "--ref-image", "https://example.com/c.png",
            "--dry-run", "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        refs = json.loads(proc.stdout)["request"]["input_references"]
        self.assertEqual([r["role"] for r in refs],
                         ["first_frame", "last_frame", "reference_image"])
        self.assertEqual([r["type"] for r in refs], ["image", "image", "image"])

    def test_last_frame_without_first_is_rejected_locally(self):
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--last-frame", "https://example.com/b.png", "--dry-run", "--json")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("必须和 --first-frame", proc.stdout)


class TestStrictAndParamOverride(IntegrationBase):
    def test_strict_rejects_unknown_param(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--param", "bogus=1", "--strict", "--dry-run", "--json",
            gateway=self.gateway)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bogus", proc.stdout)

    def test_param_overrides_dedicated_flag(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli(
            "video", "--model", "mock-video", "--prompt", "x",
            "--duration", "5", "--param", "duration=8", "--dry-run", "--json",
            gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["request"]["duration"], 8)


# ---------------------------------------------------------------------------
# 5. 分发副本：标准 skills/<name> 布局不能与源漂移
# ---------------------------------------------------------------------------


class TestDistributionSync(unittest.TestCase):
    def test_generated_copy_matches_source(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "sync_dist.py"), "--check"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         "分发副本与插件目录不一致，跑 tools/sync_dist.py\n" + proc.stdout)

    def test_generated_copies_contain_launchers(self):
        self.assertTrue(os.path.isfile(
            os.path.join(REPO_ROOT, "skills", "easyapi-studio", "bin", "easyapi")))
        self.assertTrue(os.path.isfile(
            os.path.join(REPO_ROOT, "skills", "easyapi-auth", "bin", "easyapi-auth")))

    def test_legacy_easyapi_dist_copy_is_gone(self):
        # 改名为 easyapi-studio 后，旧目录会让安装器装到旧名字
        self.assertFalse(os.path.isdir(os.path.join(REPO_ROOT, "skills", "easyapi")))

    def test_skills_require_launchers(self):
        self.assertTrue(os.path.isfile(os.path.join(SKILL_DIR, "bin", "easyapi")))
        self.assertTrue(os.path.isfile(os.path.join(AUTH_SKILL_DIR, "bin", "easyapi-auth")))


class TestAuthSkill(unittest.TestCase):
    """授权技能：只暴露身份类命令，脚本与 studio 字节一致。"""

    def test_shared_script_is_byte_identical(self):
        with open(SCRIPT, "rb") as fh:
            primary = fh.read()
        with open(AUTH_SCRIPT, "rb") as fh:
            secondary = fh.read()
        self.assertEqual(primary, secondary,
                         "auth 的脚本必须是 studio 的副本，改一处要跑 tools/sync_dist.py")

    def test_skill_md_documents_identity_only(self):
        with open(os.path.join(AUTH_SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("login", text)
        self.assertIn("quota", text)
        self.assertNotIn("--prompt", text, "授权技能不该教生成命令")
        self.assertNotIn("image --model", text)
        self.assertNotIn("video --model", text)
        self.assertIn("easyapi-studio", text, "要告诉用户生成能力在另一个技能里")


class TestAuthSkillRelocatable(IntegrationBase):
    def setUp(self):
        super().setUp()
        if not shutil.which("bash"):
            self.skipTest("需要 bash 才能验证 bin/easyapi-auth 启动器")

    def test_auth_launcher_survives_copy_and_foreign_cwd(self):
        target = os.path.join(tempfile.mkdtemp(prefix="easyapi-auth-copy-"), "easyapi-auth")
        shutil.copytree(AUTH_SKILL_DIR, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elsewhere = tempfile.mkdtemp(prefix="easyapi-auth-cwd-")
        proc = self.run_launcher("env", "--json", key=None,
                                 skill_dir=target, cwd=elsewhere,
                                 launcher="easyapi-auth")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(os.path.normcase(os.path.realpath(payload["skill_dir"])),
                         os.path.normcase(os.path.realpath(target)))


# ---------------------------------------------------------------------------
# 6. 安全与交互：设备绑定、key 输出、登录参数、候选模型
# ---------------------------------------------------------------------------


class TestDeviceBindingUnits(unittest.TestCase):
    def test_fingerprint_is_stable_hex(self):
        parts = ["machine-guid", "windows", "amd64"]
        first = mod._fingerprint_from(parts)
        self.assertEqual(first, mod._fingerprint_from(parts))
        self.assertEqual(len(first), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in first))
        self.assertNotEqual(first, mod._fingerprint_from(["other", "windows", "amd64"]))

    def test_process_cache_keeps_one_value(self):
        self.assertEqual(mod.client_id(), mod.client_id())

    def test_client_header_only_on_authenticated_requests(self):
        with_auth = mod.with_client_header({"Authorization": "Bearer sk-x"})
        self.assertEqual(with_auth[mod.CLIENT_ID_HEADER], mod.client_id())
        anonymous = mod.with_client_header({"Accept": "application/json"})
        self.assertNotIn(mod.CLIENT_ID_HEADER, anonymous)

    def test_binding_mismatch_has_rebind_hint(self):
        message, hint = mod.translate_error(403, {
            "error": {"code": "client_binding_mismatch", "message": "bound"}}, "")
        self.assertIn("rebind", hint)
        self.assertIn("另一台设备", hint)


class TestKeyOutputHygiene(IntegrationBase):
    def test_config_never_prints_key_fragments(self):
        key = "sk-abcdef1234567890wxyz"
        proc = self.run_cli("config", key=key)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("已配置", proc.stdout)
        self.assertNotIn("sk-abc", proc.stdout, "key 前 6 位也不能进对话")
        self.assertNotIn("wxyz", proc.stdout, "key 后 4 位也不能进对话")


class TestLoginBinding(IntegrationBase):
    STDIN = "user@example.com\nsecret-pass\n"

    def test_new_key_is_bound_and_quota_can_be_capped(self):
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, self.STDIN,
                              "--key-quota", "5", "--key-expire-days", "90")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        body = self.gateway.token_bodies[0]
        self.assertEqual(len(body["client_id"]), 64)
        self.assertTrue(body["client_name"])
        self.assertFalse(body["unlimited_quota"])
        self.assertEqual(body["remain_quota"], 5 * mod.QUOTA_PER_UNIT)
        self.assertGreater(body["expired_time"], time.time())
        self.assertEqual(self.read_config()["token_id"], 1000)

    def test_no_bind_omits_client_id(self):
        self.gateway = MockGateway().start()
        proc = self.run_login(self.gateway, self.STDIN, "--no-bind")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("client_id", self.gateway.token_bodies[0])

    def test_rebind_creates_new_key_and_revokes_old(self):
        self.gateway = MockGateway().start()
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"api_key": "sk-oldkeyoldkeyoldkey", "token_id": 999}, fh)

        proc = self.run_login(self.gateway, self.STDIN, "--rebind")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = [body["name"] for body in self.gateway.token_bodies]
        self.assertTrue(names and names[0].startswith("easyapi-skill-"),
                        "换绑必须新建带后缀的专用 key，不能复用旧的同名 key")
        self.assertIn(999, self.gateway.deleted_tokens, "旧 key 必须吊销")
        self.assertEqual(self.read_config()["token_id"], 1000)


class TestRecommend(IntegrationBase):
    BUNDLE = {
        "items": [
            {"model": "cheap-t2v", "category": "video", "endpoints": [],
             "request": {"type": "object", "properties": {"prompt": {"type": "string"}}}},
            {"model": "frame-i2v", "category": "video", "endpoints": [],
             "request": {"type": "object", "properties": {
                 "prompt": {"type": "string"},
                 "input_references": {"type": "array"}}}},
            {"model": "audio-i2v", "category": "video", "endpoints": [],
             "request": {"type": "object", "properties": {
                 "prompt": {"type": "string"},
                 "input_references": {"type": "array"},
                 "generate_audio": {"type": "boolean"}}}},
        ],
        "pricing_version": "v-test", "schema_version": "v-test",
    }

    def _gateway(self):
        rows = [
            {"model_name": "cheap-t2v", "category": "Video", "quota_type": 2,
             "model_ratio": 0.1, "video_per_second_ratios": {"720p": 1}},
            {"model_name": "frame-i2v", "category": "Video", "quota_type": 2,
             "model_ratio": 0.2, "video_per_second_ratios": {"720p": 1}},
            {"model_name": "audio-i2v", "category": "Video", "quota_type": 2,
             "model_ratio": 0.3, "video_per_second_ratios": {"720p": 1}},
        ]
        return MockGateway(schema_bundle=self.BUNDLE, pricing_rows=rows).start()

    def test_filters_by_capability_and_orders_by_price(self):
        self.gateway = self._gateway()
        proc = self.run_cli("recommend", "--category", "video",
                            "--need", "first-last-frame", "--json",
                            gateway=self.gateway, key=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        models = [item["model"] for item in payload["items"]]
        self.assertNotIn("cheap-t2v", models, "没有 input_references 的模型要被过滤")
        self.assertEqual(models[0], "frame-i2v")
        self.assertEqual(payload["recommended"], "frame-i2v")
        self.assertLess(payload["items"][0]["estimated_usd"],
                        payload["items"][1]["estimated_usd"])

    def test_audio_need_only_matches_models_declaring_it(self):
        self.gateway = self._gateway()
        proc = self.run_cli("recommend", "--category", "video",
                            "--need", "audio", "--json",
                            gateway=self.gateway, key=None)
        payload = json.loads(proc.stdout)
        self.assertEqual([item["model"] for item in payload["items"]], ["audio-i2v"])


class TestDryRunEstimate(IntegrationBase):
    def test_video_dry_run_reports_estimate_and_effective_params(self):
        self.gateway = MockGateway().start()
        proc = self.run_cli("video", "--model", "mock-video", "--prompt", "x",
                            "--dry-run", "--json", gateway=self.gateway)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIsNotNone(payload["estimate_usd"])
        self.assertIn("duration", payload["effective_params"])
        self.assertIn("resolution", payload["effective_params"])


class TestAuthorizeUrlAndCallbackPage(unittest.TestCase):
    def test_manual_flag_in_authorize_url(self):
        manual = mod.build_authorize_url(
            "https://www.easyapi.com/", "http://127.0.0.1:9000/callback",
            "state-1", "EasyAPI CLI", manual=True)
        self.assertTrue(manual.startswith("https://www.easyapi.com/cli/authorize?"))
        self.assertIn("manual=1", manual)
        self.assertNotIn("manual=1", mod.build_authorize_url(
            "https://www.easyapi.com", "http://127.0.0.1:9000/callback",
            "state-1", "EasyAPI CLI", manual=False))

    def test_poll_url_omits_redirect_uri(self):
        """轮询模式的授权页地址只带 state：带了回调地址前端就会回跳本机。"""
        url = mod.build_authorize_url("https://www.easyapi.com", None,
                                      "state-1", "EasyAPI CLI")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertNotIn("redirect_uri", params)
        self.assertEqual(params["state"], "state-1")
        self.assertEqual(params["client_name"], "EasyAPI CLI")

    def test_verifier_hash_is_sha256_hex(self):
        # 后端只认 64 位小写十六进制；大小写或长度错了整条轮询都取不到票
        digest = mod._cli_verifier_hash("verifier-x")
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())
        self.assertEqual(digest, hashlib.sha256(b"verifier-x").hexdigest())

    def test_callback_ok_page_offers_website_redirect(self):
        html = mod._callback_ok_html()
        self.assertIn("授权成功", html)
        self.assertIn("https://www.easyapi.com", html)
        self.assertIn("setInterval", html)
        self.assertIn("留在本页", html)

    def test_callback_fail_page_escapes_reason(self):
        html = mod._callback_fail_html('<script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestLoginFastPath(IntegrationBase):
    def test_login_with_valid_key_does_not_reauth(self):
        self.gateway = MockGateway().start()
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"api_key": "sk-alreadyconfigured", "token_id": 7}, fh)

        proc = self.run_cli("login", "--json", gateway=self.gateway, key=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload.get("already_logged_in"))
        self.assertFalse(any(r[1].startswith("/cli/") for r in self.gateway.requests),
                         "key 可用时不该再走浏览器授权")
        self.assertFalse(any(r[0] == "POST" and r[1] == "/token" for r in self.gateway.requests),
                         "key 可用时不该再新建令牌")
        self.assertEqual(self.read_config()["api_key"], "sk-alreadyconfigured")


class TestBrowserLoginPollFlow(TestBrowserLoginFlow):
    """轮询模式（默认）：授权页不回跳，CLI 自己取票。

    这是「授权成功后不跳本地页面」的核心链路：
    登记会话（/cli/session）→ 页面授权（不带 redirect_uri）→ CLI 轮询（/cli/ticket）。
    """

    def complete_poll_authorization(self, gateway, authorize_url):
        parsed = urllib.parse.urlparse(authorize_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        assert "redirect_uri" not in params, params

        # 模拟授权页：持登录态签票，请求里不带回调地址
        status, payload, _ = mod.http_json(
            "POST", gateway.base_url + "/cli/authorize",
            headers={"Authorization": "Bearer dash-access-token", "EasyAPI-User": "42"},
            body={"client_name": params.get("client_name", ""),
                  "state": params.get("state", "")})
        assert status == 200 and payload.get("success"), payload
        return payload["data"]["ticket"]

    def test_poll_login_completes_without_local_callback(self):
        self.gateway = MockGateway(cli_poll_enabled=True).start()
        seen = []

        def on_stderr(text):
            url = self.authorize_url_from_output(text)
            if url and not seen:
                seen.append(url)
                self.complete_poll_authorization(self.gateway, url)

        proc = self.run_browser_login(self.gateway, on_stderr=on_stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        self.assertTrue(seen, "必须打印授权页地址供无浏览器环境使用")
        self.assertNotIn("redirect_uri", seen[0], "轮询模式不该带回调地址：%s" % seen[0])
        self.assertIn("state=", seen[0])
        # 后端确实走了「登记会话 → 页面授权 → 主动取票」三步
        paths = [r[1] for r in self.gateway.requests]
        self.assertIn("/cli/session", paths)
        self.assertTrue(any(p.startswith("/cli/ticket") for p in paths), paths)

        cfg = self.read_config()
        self.assertTrue(cfg["api_key"].startswith("sk-"))
        self.assertEqual(self.gateway.created_tokens, ["easyapi-skill"])
        raw = open(os.path.join(self.home, "config.json"), encoding="utf-8").read()
        self.assertNotIn("dash-access-token", raw)

    def test_old_backend_falls_back_to_loopback(self):
        """后端还没有 /cli/session 时不能直接失败：老部署的用户仍要能登录。"""
        self.gateway = MockGateway(cli_poll_enabled=False).start()
        seen = []

        def on_stderr(text):
            url = self.authorize_url_from_output(text)
            if url and not seen:
                seen.append(url)
                self.complete_authorization(self.gateway, url)

        proc = self.run_browser_login(self.gateway, on_stderr=on_stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("回退到本机回调", proc.stderr)
        self.assertTrue(seen)
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A", seen[0])

    def test_loopback_flag_forces_old_flow(self):
        """--loopback 明确要求旧流程时，不该再去登记会话。"""
        self.gateway = MockGateway(cli_poll_enabled=True).start()
        seen = []

        def on_stderr(text):
            url = self.authorize_url_from_output(text)
            if url and not seen:
                seen.append(url)
                self.complete_authorization(self.gateway, url)

        proc = self.run_browser_login(self.gateway, "--loopback", on_stderr=on_stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(seen)
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A", seen[0])
        self.assertNotIn("/cli/session", [r[1] for r in self.gateway.requests])


if __name__ == "__main__":
    unittest.main(verbosity=2)
