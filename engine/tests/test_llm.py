"""LLM adapter: failure classification, vision routing, key handling, endpoints."""

import asyncio
import base64
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.api.server import app
from quantdesk.config.settings import (
    LLMProfile,
    LLMSettings,
    load_llm_settings,
    read_key_env,
    set_key_env,
)
from quantdesk.llm import (
    ChatMessage,
    DriverConfig,
    LLMError,
    LLMErrorKind,
    OpenAICompatibleDriver,
    build_driver,
    classify_failure,
    credential_issue,
    resolve_api_key,
)


def tiny_png(width: int = 8, height: int = 8) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes([(x * 30) % 256 for x in range(width * 3)]) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FailureClassificationTests(unittest.TestCase):
    def test_status_codes_map_to_actionable_kinds(self):
        for status, kind in (
            (401, LLMErrorKind.INVALID_KEY),
            (403, LLMErrorKind.INVALID_KEY),
            (404, LLMErrorKind.MODEL_NOT_FOUND),
            (429, LLMErrorKind.RATE_LIMITED),
            (400, LLMErrorKind.BAD_REQUEST),
            (500, LLMErrorKind.SERVER),
            (503, LLMErrorKind.SERVER),
        ):
            self.assertEqual(classify_failure(status, ""), kind, status)

    def test_messages_win_over_ambiguous_status(self):
        # DeepSeek reports an exhausted balance as 402, some gateways as 400.
        self.assertEqual(classify_failure(402, "Insufficient Balance"), LLMErrorKind.QUOTA)
        self.assertEqual(classify_failure(400, "insufficient balance"), LLMErrorKind.QUOTA)
        self.assertEqual(classify_failure(None, "Connection error."), LLMErrorKind.NETWORK)
        self.assertEqual(classify_failure(None, "Request timed out"), LLMErrorKind.TIMEOUT)

    def test_error_copy_is_actionable(self):
        error = LLMError(LLMErrorKind.NO_KEY, "missing DEEPSEEK_API_KEY")
        payload = error.as_dict()
        self.assertEqual(payload["kind"], "no_key")
        self.assertTrue(payload["action"])
        self.assertIn("DEEPSEEK_API_KEY", payload["detail"])


class CredentialShapeTests(unittest.TestCase):
    """A pasted placeholder must be caught here, not explained away as a 401."""

    def test_documentation_placeholders_are_identified(self):
        for value in ("sk-...", "sk-", "sk-xxx", "your-api-key", "changeme", "  sk-...  "):
            self.assertIsNotNone(credential_issue(value), value)

    def test_realistic_keys_pass(self):
        for value in ("sk-1234567890abcdef1234567890abcdef", "sk-proj-AbCdEf123456", "sk-secret-value"):
            self.assertIsNone(credential_issue(value), value)

    def test_empty_and_non_ascii(self):
        self.assertEqual(credential_issue(""), "值为空")
        self.assertIsNotNone(credential_issue("sk-密钥abc"))
        self.assertIsNone(credential_issue(None))


class KeyResolutionTests(unittest.TestCase):
    def test_declared_name_then_provider_default_then_env(self):
        with patch.dict(os.environ, {"CUSTOM_KEY": "abc"}, clear=False):
            value, candidates = resolve_api_key("deepseek", "CUSTOM_KEY")
            self.assertEqual(value, "abc")
            self.assertEqual(candidates[0], "CUSTOM_KEY")
            self.assertIn("DEEPSEEK_API_KEY", candidates)

    def test_missing_key_returns_none_but_still_names_the_variable(self):
        env = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
        with patch.dict(os.environ, env, clear=True):
            value, candidates = resolve_api_key("openai", "OPENAI_API_KEY")
            self.assertIsNone(value)
            self.assertEqual(candidates[0], "OPENAI_API_KEY")

    def test_keys_file_round_trip_clears_on_empty_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            set_key_env({"DEEPSEEK_API_KEY": "sk-test"}, home)
            self.assertEqual(read_key_env(home)["DEEPSEEK_API_KEY"], "sk-test")
            mode = (home / "keys.env").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, "keys.env must stay owner-only")
            set_key_env({"DEEPSEEK_API_KEY": ""}, home)
            self.assertNotIn("DEEPSEEK_API_KEY", read_key_env(home))


class VisionRoutingTests(unittest.TestCase):
    def settings(self) -> LLMSettings:
        return LLMSettings(
            profiles={
                "text": LLMProfile(name="text", provider="deepseek", deep_model="deepseek-chat"),
                "eyes": LLMProfile(name="eyes", provider="openai", vision_model="gpt-4o", supports_vision=True),
            }
        )

    def test_role_without_vision_is_refused_not_rerouted(self):
        settings = self.settings()
        settings.roles = {"chart_analysis": "text"}
        with self.assertRaises(KeyError) as caught:
            settings.vision_profile("chart_analysis")
        self.assertIn("text", caught.exception.args[0])

    def test_vision_role_is_used(self):
        settings = self.settings()
        settings.roles = {"chart_analysis": "eyes"}
        self.assertEqual(settings.vision_profile("chart_analysis").name, "eyes")

    def test_driver_refuses_images_for_text_profiles(self):
        driver = OpenAICompatibleDriver(
            DriverConfig(profile="text", provider="deepseek", base_url="", api_key="k", supports_vision=False)
        )
        with self.assertRaises(LLMError) as caught:
            driver.complete([ChatMessage(role="user", text="看图", images=[tiny_png()])], model="deepseek-chat")
        self.assertEqual(caught.exception.kind, LLMErrorKind.BAD_REQUEST)

    def test_missing_model_name_is_reported_as_such(self):
        driver = build_driver(DriverConfig(profile="x", provider="deepseek", base_url="", api_key="k"))
        with self.assertRaises(LLMError) as caught:
            driver.complete([ChatMessage(role="user", text="hi")], model="")
        self.assertEqual(caught.exception.kind, LLMErrorKind.MODEL_NOT_FOUND)

    def test_image_payload_is_sent_as_data_url_when_vision_is_declared(self):
        message = ChatMessage(role="user", text="看图", images=[tiny_png()])
        parts = message.content_parts(allow_images=True)
        self.assertIsInstance(parts, list)
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        # Without vision the image is dropped rather than smuggled through.
        self.assertEqual(message.content_parts(allow_images=False), "看图")


class LlmEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"QUANTDESK_HOME": self._tmp.name}, clear=False)
        self._env.start()
        for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GLM_API_KEY"):
            os.environ.pop(name, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self._env.stop()
        self._tmp.cleanup()

    async def test_settings_never_exposes_key_values(self):
        await self.client.post("/api/llm/keys", json={"DEEPSEEK_API_KEY": "sk-secret-value"})
        response = await self.client.get("/api/llm/settings")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("sk-secret-value", response.text)
        deepseek = next(row for row in body["profiles"] if row["name"] == "deepseek")
        self.assertTrue(deepseek["hasKey"])
        self.assertEqual(deepseek["baseUrl"], "https://api.deepseek.com/v1")

    async def test_test_endpoint_names_the_missing_key(self):
        response = await self.client.post("/api/llm/profiles/deepseek/test")
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertIn("DEEPSEEK_API_KEY", detail)

    async def test_test_endpoint_reports_a_successful_round_trip(self):
        await self.client.post("/api/llm/keys", json={"DEEPSEEK_API_KEY": "sk-stub"})

        class StubCompletion:
            text = "可用"
            model = "deepseek-v4-pro"
            usage = {"total_tokens": 7}
            latency_s = 0.4
            notes: list[str] = []

        class StubDriver:
            def complete(self, messages, **kwargs):
                # signal_explain resolves to the profile's deep model.
                assert kwargs["model"] == "deepseek-v4-pro"
                return StubCompletion()

        with patch("quantdesk.api.llm._profile_driver", return_value=StubDriver()):
            response = await self.client.post("/api/llm/profiles/deepseek/test")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["model"], "deepseek-v4-pro")
        self.assertEqual(body["requestedModel"], "deepseek-v4-pro")
        self.assertFalse(body["substituted"])
        self.assertEqual(body["reply"], "可用")

    async def test_test_endpoint_surfaces_a_provider_failure(self):
        await self.client.post("/api/llm/keys", json={"DEEPSEEK_API_KEY": "sk-stub"})

        class FailingDriver:
            def complete(self, messages, **kwargs):
                raise LLMError(LLMErrorKind.QUOTA, "Insufficient Balance", status=402)

        with patch("quantdesk.api.llm._profile_driver", return_value=FailingDriver()):
            response = await self.client.post("/api/llm/profiles/deepseek/test")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["kind"], "quota")
        self.assertTrue(body["error"]["action"])

    async def test_unknown_profile_and_role_are_rejected(self):
        self.assertEqual((await self.client.post("/api/llm/profiles/nope/test")).status_code, 404)
        response = await self.client.post("/api/llm/roles", json={"chart_analysis": "ghost"})
        self.assertEqual(response.status_code, 422)

    async def test_keys_endpoint_rejects_non_env_names(self):
        response = await self.client.post("/api/llm/keys", json={"bad name": "x"})
        self.assertEqual(response.status_code, 422)

    async def test_analyze_chart_validates_input_before_calling_a_model(self):
        # Not an image at all.
        response = await self.client.post("/api/llm/analyze-chart", json={"imageBase64": base64.b64encode(b"hello").decode()})
        self.assertEqual(response.status_code, 415)
        # Real PNG but the role points at a text-only profile.
        await self.client.post("/api/llm/roles", json={"chart_analysis": "deepseek"})
        response = await self.client.post(
            "/api/llm/analyze-chart",
            json={"imageBase64": base64.b64encode(tiny_png()).decode(), "symbol": "AMD"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("supports_vision", response.json()["detail"])
        # Unknown symbol is refused before any provider call too.
        await self.client.post("/api/llm/roles", json={"chart_analysis": "glm-vision"})
        response = await self.client.post(
            "/api/llm/analyze-chart",
            json={"imageBase64": base64.b64encode(tiny_png()).decode(), "symbol": "DOGEUSDT"},
        )
        self.assertEqual(response.status_code, 422)

    async def test_analyze_chart_stores_the_analysis_and_returns_structure(self):
        await self.client.post("/api/llm/keys", json={"OPENAI_API_KEY": "sk-stub"})
        await self.client.post("/api/llm/roles", json={"chart_analysis": "openai"})
        structured = {"trend": "上升", "uncertain": ["精确OHLC"], "confidence": "medium"}

        class StubCompletion:
            text = json.dumps(structured, ensure_ascii=False)
            model = "gpt-4o"
            usage = {"total_tokens": 42}
            latency_s = 1.5
            kind = "vision"

            def as_dict(self):
                return {"text": self.text, "model": self.model, "usage": self.usage, "latency_s": self.latency_s, "kind": self.kind}

        class StubDriver:
            def complete(self, messages, **kwargs):
                # The prompt must forbid trading instructions and demand JSON.
                joined = " ".join(message.text for message in messages)
                assert "不给出任何买卖建议" in joined
                assert messages[-1].images, "the screenshot must reach the driver"
                return StubCompletion()

        with patch("quantdesk.api.llm._profile_driver", return_value=StubDriver()):
            response = await self.client.post(
                "/api/llm/analyze-chart",
                json={"imageBase64": base64.b64encode(tiny_png()).decode(), "symbol": "AMD", "timeframe": "1h"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["structured"], structured)
        self.assertEqual(body["symbol"], "AMDSTOCKUSDT")
        self.assertIn("不是交易指令", body["disclaimer"])
        self.assertTrue(Path(body["imagePath"]).exists())

    async def test_model_substitution_is_reported_not_hidden(self):
        await self.client.post("/api/llm/keys", json={"DEEPSEEK_API_KEY": "sk-1234567890abcdef"})

        class SubstitutingCompletion:
            text = "可用"
            model = "deepseek-flash"  # provider served something else
            usage = {}
            latency_s = 0.3
            notes: list[str] = []

        class SubstitutingDriver:
            def complete(self, messages, **kwargs):
                assert kwargs["model"] == "deepseek-v4-pro", "the configured name is what gets asked for"
                return SubstitutingCompletion()

        with patch("quantdesk.api.llm._profile_driver", return_value=SubstitutingDriver()):
            response = await self.client.post("/api/llm/profiles/deepseek/test?model=deepseek-v4-pro")
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["substituted"])
        self.assertEqual(body["requestedModel"], "deepseek-v4-pro")
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertIn("未生效", body["warning"])

    async def test_model_config_can_be_saved(self):
        response = await self.client.post(
            "/api/llm/profiles/deepseek/models-config",
            json={"deepModel": "deepseek-v4-pro", "quickModel": "deepseek-flash"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["models"]["deep"], "deepseek-v4-pro")
        self.assertIn("tradingagents", body["rolesUsingIt"])

        # And it survives a reload.
        settings = await self.client.get("/api/llm/settings")
        deepseek = next(row for row in settings.json()["profiles"] if row["name"] == "deepseek")
        self.assertEqual(deepseek["models"]["deep"], "deepseek-v4-pro")
        self.assertEqual(deepseek["models"]["quick"], "deepseek-flash")

    async def test_model_config_rejects_clearing_every_model(self):
        response = await self.client.post(
            "/api/llm/profiles/deepseek/models-config",
            json={"deepModel": "", "quickModel": "", "visionModel": ""},
        )
        self.assertEqual(response.status_code, 422)

    async def test_status_reports_chart_readiness(self):
        response = await self.client.get("/api/llm/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["visionProfile"], "glm-vision")
        self.assertFalse(body["canAnalyzeCharts"])
        roles = {row["role"]: row for row in body["roles"]}
        self.assertTrue(roles["chart_analysis"]["supportsVision"])
        self.assertFalse(roles["tradingagents"]["supportsVision"])


if __name__ == "__main__":
    unittest.main()
