"""原生绘图协议适配器。

灵绘工坊的核心链路是 OpenAI Images / OpenAI Chat / Gemini 三种通用形态，
但社区里还有一批只认自家私有请求体的服务：

* Grok（xAI）——images/generations 与 images/edits，编辑走 image 对象；
* Agnes——参考图必须塞进 extra_body；
* Jimeng（jimeng-free-api 之类的本地网关）——固定 b64_json；
* NovelAI——官方接口返回 ZIP，第三方网关则是 GET 直出图片字节。

这里只负责「把请求描述出来」，实际发请求、解析响应仍由
api_manager 统一处理，这样超时、代理、重试、指标统计等行为保持一致。
"""

from __future__ import annotations

import base64
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# auto 表示沿用渠道自身的 interface_mode（通用 OpenAI / Gemini 形态）。
PROTOCOL_AUTO = "auto"

NATIVE_PROTOCOLS: Tuple[str, ...] = ("grok", "agnes", "novelai", "jimeng")

PROTOCOL_CHOICES: Tuple[str, ...] = (PROTOCOL_AUTO,) + NATIVE_PROTOCOLS

PROTOCOL_LABELS: Dict[str, str] = {
    PROTOCOL_AUTO: "自动（沿用接口模式）",
    "grok": "Grok / xAI 原生",
    "agnes": "Agnes 原生",
    "novelai": "NovelAI 原生",
    "jimeng": "即梦 Jimeng 原生",
}

DEFAULT_BASE_URLS: Dict[str, str] = {
    "grok": "https://api.x.ai",
    "agnes": "https://apihub.agnes-ai.com",
    "novelai": "https://api.novelai.net",
    "jimeng": "http://localhost:5100",
}

DEFAULT_MODELS: Dict[str, str] = {
    "grok": "grok-imagine-image",
    "agnes": "agnes-image-2.1-flash",
    "novelai": "nai-diffusion-4-5-full",
    "jimeng": "jimeng-4.5",
}

# NovelAI 的默认负面词故意保持简短，避免压过用户自己的提示词。
NAI_DEFAULT_NEGATIVE = (
    "lowres, worst quality, bad anatomy, bad hands, extra digits, blurry, watermark, text"
)

_AGNES_SIZE_MAP: Dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1024x576",
    "9:16": "576x1024",
    "3:2": "1024x682",
    "2:3": "682x1024",
    "4:3": "1024x768",
    "3:4": "768x1024",
    "4:5": "819x1024",
    "5:4": "1024x819",
    "21:9": "1024x439",
}

_LANDSCAPE_RATIOS = {"16:9", "3:2", "4:3", "5:4", "21:9"}
_PORTRAIT_RATIOS = {"9:16", "2:3", "3:4", "4:5"}


def normalize_protocol(value: Any) -> str:
    """把配置里的协议字段规整为受支持的取值。"""
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        return PROTOCOL_AUTO
    aliases = {
        "xai": "grok",
        "grok_image": "grok",
        "grok_imagine": "grok",
        "agnes_ai": "agnes",
        "nai": "novelai",
        "novel_ai": "novelai",
        "jimeng2api": "jimeng",
        "jimeng_free_api": "jimeng",
        "": PROTOCOL_AUTO,
        "none": PROTOCOL_AUTO,
        "default": PROTOCOL_AUTO,
    }
    text = aliases.get(text, text)
    return text if text in PROTOCOL_CHOICES else PROTOCOL_AUTO


def is_native_protocol(value: Any) -> bool:
    return normalize_protocol(value) in NATIVE_PROTOCOLS


def default_base_url(protocol: str) -> str:
    return DEFAULT_BASE_URLS.get(normalize_protocol(protocol), "")


def default_model(protocol: str) -> str:
    return DEFAULT_MODELS.get(normalize_protocol(protocol), "")


def map_agnes_size(aspect_ratio: Optional[str]) -> str:
    key = str(aspect_ratio or "").strip()
    return _AGNES_SIZE_MAP.get(key, "1024x1024")


def map_nai_size(aspect_ratio: Optional[str], resolution: Optional[str] = "1K") -> Tuple[int, int]:
    """把宽高比映射为 NovelAI 官方接口能接受的像素尺寸。"""
    aspect = str(aspect_ratio or "").strip()
    res = str(resolution or "1K").strip().upper()
    if aspect in _LANDSCAPE_RATIOS:
        base = (1216, 832)
    elif aspect in _PORTRAIT_RATIOS:
        base = (832, 1216)
    else:
        base = (1024, 1024)
    if res in {"2K", "4K"}:
        return (min(base[0] * 2, 2048), min(base[1] * 2, 2048))
    return base


def map_nai_gateway_size(aspect_ratio: Optional[str], resolution: Optional[str] = "1K") -> str:
    """第三方 NAI 网关普遍使用中文尺寸标签。"""
    aspect = str(aspect_ratio or "").strip()
    res = str(resolution or "1K").strip().upper()
    if aspect in _LANDSCAPE_RATIOS:
        orient = "横图"
    elif aspect in _PORTRAIT_RATIOS:
        orient = "竖图"
    else:
        orient = "方图"
    if res in {"2K", "4K"}:
        return f"{res}{orient}"
    return orient


def novelai_mode(base_url: str) -> str:
    """判断 NovelAI 渠道是官方接口还是第三方网关。"""
    base = str(base_url or "").strip().lower().rstrip("/")
    if not base:
        return "official"
    if "novelai.net" in base or base.endswith("/ai") or "/ai/generate-image" in base:
        return "official"
    if any(hint in base for hint in ("sta1n", "nai2api", "loliyc", "/generate")):
        return "gateway"
    if "novelai" not in base:
        return "gateway"
    return "official"


@dataclass
class ProtocolRequest:
    """一次原生协议调用所需的全部信息。"""

    protocol: str
    method: str = "POST"
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    json_body: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, str]] = None
    #: 该协议是否可能直接返回图片字节 / ZIP（而不是 JSON）。
    expects_binary: bool = False
    #: 实际使用的模型名，便于日志与用量统计。
    model: str = ""

    def describe(self) -> str:
        return f"{self.protocol}:{self.method} {self.url}"


def _data_url(image: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(image).decode()}"


def _clean_base(base_url: str, protocol: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return base or default_base_url(protocol)


def _build_grok(
    base: str,
    model: str,
    prompt: str,
    images: List[bytes],
    aspect_ratio: Optional[str],
    resolution: Optional[str],
    mime_of: Any,
) -> ProtocolRequest:
    ratio = str(aspect_ratio or "").strip() or "auto"
    body: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "response_format": "b64_json",
    }
    if images:
        # 生成模型 id 与编辑模型 id 在 xAI 侧是分开的，自动纠正一次。
        edit_model = model
        if "edit" not in edit_model.lower() and edit_model.lower().startswith("grok-imagine-image"):
            edit_model = "grok-imagine-image-edit"
        body["model"] = edit_model
        body["image"] = {"url": _data_url(images[0], mime_of(images[0])), "type": "image_url"}
        if ratio != "auto":
            body["aspect_ratio"] = ratio
        if resolution:
            body["resolution"] = str(resolution).lower()
        url = f"{base}/v1/images/edits"
        model = edit_model
    else:
        body["aspect_ratio"] = ratio
        body["resolution"] = str(resolution or "2K").lower()
        url = f"{base}/v1/images/generations"
    return ProtocolRequest(protocol="grok", url=url, json_body=body, model=model)


def _build_agnes(
    base: str,
    model: str,
    prompt: str,
    images: List[bytes],
    aspect_ratio: Optional[str],
    mime_of: Any,
) -> ProtocolRequest:
    body: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": map_agnes_size(aspect_ratio),
    }
    if images:
        body["extra_body"] = {
            "image": [_data_url(img, mime_of(img)) for img in images if img],
            "response_format": "url",
        }
    return ProtocolRequest(
        protocol="agnes",
        url=f"{base}/v1/images/generations",
        json_body=body,
        model=model,
    )


def _build_jimeng(base: str, model: str, prompt: str) -> ProtocolRequest:
    body = {
        "model": model,
        "prompt": prompt,
        "response_format": "b64_json",
    }
    return ProtocolRequest(
        protocol="jimeng",
        url=f"{base}/v1/images/generations",
        json_body=body,
        model=model,
    )


def _build_novelai(
    raw_base: str,
    base: str,
    key: str,
    model: str,
    prompt: str,
    images: List[bytes],
    aspect_ratio: Optional[str],
    resolution: Optional[str],
    negative_prompt: str,
) -> ProtocolRequest:
    mode = novelai_mode(raw_base or base)
    negative = str(negative_prompt or "").strip() or NAI_DEFAULT_NEGATIVE
    lower_raw = str(raw_base or "").strip().lower().rstrip("/")

    if mode == "official":
        if lower_raw.endswith("generate-image"):
            url = str(raw_base).strip().rstrip("/")
        else:
            url = f"{base}/ai/generate-image"
        width, height = map_nai_size(aspect_ratio, resolution)
        parameters: Dict[str, Any] = {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": 5.0,
            "sampler": "k_dpmpp_2m",
            "steps": 28,
            "seed": random.randint(1, 2**31 - 1),
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": False,
            "sm": False,
            "sm_dyn": False,
            "dynamic_thresholding": False,
            "controlnet_strength": 1,
            "legacy": False,
            "add_original_image": True,
            "cfg_rescale": 0,
            "noise_schedule": "karras",
            "legacy_v3_extend": False,
            "skip_cfg_above_sigma": None,
            "use_coords": False,
            "legacy_uc": False,
            "normalize_strength": 1,
            "inpaintImg2ImgStrength": 1,
            "noise": 0,
            "strength": 0.7,
            "negative_prompt": negative,
            "uc": negative,
            "v4_prompt": {
                "caption": {"base_caption": prompt, "char_captions": []},
                "use_coords": False,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {"base_caption": negative, "char_captions": []},
                "legacy_uc": False,
            },
        }
        action = "generate"
        if images:
            parameters["image"] = base64.b64encode(images[0]).decode("ascii")
            parameters["strength"] = 0.55
            parameters["noise"] = 0
            action = "img2img"
        body = {
            "input": prompt,
            "model": model,
            "action": action,
            "parameters": parameters,
        }
        return ProtocolRequest(
            protocol="novelai",
            method="POST",
            url=url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/zip,image/*,application/json,*/*",
            },
            json_body=body,
            expects_binary=True,
            model=model,
        )

    if lower_raw.endswith("/generate"):
        url = str(raw_base).strip().rstrip("/")
    else:
        url = f"{base}/generate"
    params = {
        "token": str(key or "").strip(),
        "tag": prompt,
        "model": model,
        "size": map_nai_gateway_size(aspect_ratio, resolution),
        "steps": "28",
        "scale": "6",
        "cfg": "0",
        "sampler": "k_dpmpp_2m_sde",
        "noise_schedule": "karras",
        "nocache": "1",
        "negative": negative,
    }
    params = {k: v for k, v in params.items() if v is not None and str(v) != ""}
    return ProtocolRequest(
        protocol="novelai",
        method="GET",
        url=url,
        headers={
            "Accept": "image/*,application/zip,application/json,*/*",
            "User-Agent": "LinghuiStudio/1.0",
        },
        params=params,
        expects_binary=True,
        model=model,
    )


def build_protocol_request(
    protocol: str,
    *,
    base_url: str,
    key: str,
    model: str,
    prompt: str,
    images: Optional[List[bytes]] = None,
    aspect_ratio: Optional[str] = None,
    resolution: Optional[str] = None,
    negative_prompt: str = "",
    mime_resolver: Any = None,
) -> Optional[ProtocolRequest]:
    """构造一次原生协议请求；协议为 auto 或未知时返回 None。"""
    proto = normalize_protocol(protocol)
    if proto not in NATIVE_PROTOCOLS:
        return None

    raw_base = str(base_url or "").strip()
    base = _clean_base(raw_base, proto)
    resolved_model = str(model or "").strip() or default_model(proto)
    text = str(prompt or "").strip()
    image_list = [img for img in (images or []) if img]
    mime_of = mime_resolver or (lambda _img: "image/png")

    if proto == "grok":
        request = _build_grok(
            base, resolved_model, text, image_list, aspect_ratio, resolution, mime_of
        )
    elif proto == "agnes":
        request = _build_agnes(base, resolved_model, text, image_list, aspect_ratio, mime_of)
    elif proto == "jimeng":
        request = _build_jimeng(base, resolved_model, text)
    else:
        request = _build_novelai(
            raw_base,
            base,
            key,
            resolved_model,
            text,
            image_list,
            aspect_ratio,
            resolution,
            negative_prompt,
        )

    if not request.headers:
        request.headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json,image/*,*/*",
        }
    return request


def protocol_supports_reference_images(protocol: str) -> bool:
    """该协议是否支持参考图（图生图）。"""
    proto = normalize_protocol(protocol)
    if proto == "jimeng":
        return False
    return proto in NATIVE_PROTOCOLS or proto == PROTOCOL_AUTO
