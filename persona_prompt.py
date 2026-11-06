"""Shared persona prompt composition; defaults only fill unspecified directions."""
from __future__ import annotations

import re
from typing import Any


def has_time_request(text: str) -> bool:
    return bool(re.search(
        r"清晨|早晨|上午|中午|午后|下午|傍晚|黄昏|夕阳|日落|日出|白天|夜晚|夜间|深夜|晨光|日光|阳光|逆光|"
        r"\b(?:morning|noon|afternoon|evening|night|sunset|sunrise|daylight|sunlight|golden hour)\b",
        str(text or ""), re.I,
    ))


def has_gaze_request(text: str) -> bool:
    return bool(re.search(r"看向|看着镜头|望向|视线|目光|凝视|\b(?:gaze|look(?:ing)? at|look(?:ing)? toward)\b", str(text or ""), re.I))


def has_pose_request(text: str) -> bool:
    return bool(re.search(r"站着|站在|坐着|坐在|蹲|倚靠|靠在|抱着|举起|伸手|跑|跳|\b(?:stand(?:ing)?|sit(?:ting)?|crouch(?:ing)?|lean(?:ing)?|pose)\b", str(text or ""), re.I))


def keep_unspecified_suggestions(text: str, user_request: str, *, outfit_check, expression_check) -> str:
    """Drop suggested clauses in dimensions explicitly chosen by the user.

    Free-form tool parameters remain one text block. They are not duplicated
    into outfit and expression fields or rewritten with a second LLM call.
    """
    checks = [check for check in (outfit_check, expression_check, has_gaze_request, has_pose_request, has_time_request)
              if check(user_request)]
    clauses = re.split(r"[，,；;。\n]+", str(text or ""))
    return "; ".join(clause.strip() for clause in clauses if clause.strip()
                     and not any(check(clause) for check in checks))


def build_persona_base_prompt(config: Any, scene: str = "", extra_request: str = "") -> str:
    """Keep appearance, current scene, and rendering direction separate."""
    name = str(config.get("persona_name", "虚拟角色") or "虚拟角色").strip()
    appearance = str(config.get("persona_description", "") or "").strip()
    style = str(config.get("persona_photo_style", "") or "").strip()
    parts = [f"Create one new image of {name}."]
    if appearance:
        parts.append("Appearance: " + appearance)
    parts.append(
        "Use the first persona reference for facial identity, hair design and characteristic proportions. "
        "Clothing, expression, pose and surroundings can change with this image's directions; "
        "the reference is not a requirement to repeat them. "
        "Show one persona character unless the user explicitly requests other people."
    )
    if scene:
        parts.append("Current scene: " + str(scene).strip())
    if style:
        parts.append("Visual style: " + style)
    parts.append(
        "Use intentional framing with a clear focal subject and readable silhouette. "
        "Keep anatomy, perspective and hand-object contact coherent. "
        "Render distinct materials and scene-consistent light, with natural shadows and controlled highlights. "
        "Follow the requested visual style without forcing photorealism. "
        "Keep capture devices outside the frame unless explicitly requested."
    )
    if extra_request:
        parts.append("Additional direction: " + str(extra_request).strip())
    return "\n".join(parts)


def resolve_persona_scene(scene_hint: str, extra_request: str, context_text: str, scene_map: dict[str, str], default: str) -> tuple[str, str]:
    """An explicit scene replaces presets; exact preset names still select a preset."""
    explicit = str(scene_hint or "").strip()
    if explicit:
        if explicit in scene_map and not str(extra_request or "").strip():
            return explicit, scene_map[explicit]
        return explicit, explicit
    context = str(context_text or "").casefold()
    for name in sorted(scene_map, key=len, reverse=True):
        if name.casefold() in context:
            # Free-form current instructions may contain their own pose/time.
            return name, name if str(extra_request or "").strip() else scene_map[name]
    return ("默认", default) if default else ("", "")
