"""Linghui-authored optional default prompt pack.

These prompts are independently written for Linghui Studio. They are not
copied from the unlicensed reference implementation that inspired the feature
inventory.
"""

from __future__ import annotations

from typing import Dict, List


DEFAULT_PRESET_PACK_VERSION = 1


ADDITIONAL_DEFAULT_PRESETS: Dict[str, str] = {
    "捧脸": (
        "Create a polished close-up portrait based on the reference subject. Preserve identity, hairstyle, "
        "facial proportions, age appearance, and visual style. Pose the subject with both hands gently framing "
        "the cheeks, relaxed shoulders, warm eye contact, and a natural soft expression. Use flattering diffused "
        "light, clean composition, detailed hands, and an uncluttered background."
    ),
    "变真人": (
        "Translate the referenced illustrated or anime character into a believable real-person portrait while "
        "preserving the exact identity cues: face shape, eye impression, hairstyle, hair color, outfit design, "
        "accessories, age appearance, and expression. Use natural skin texture, realistic anatomy, cinematic but "
        "credible lighting, and photographic detail. Do not turn the character into a generic model."
    ),
    "果冻化": (
        "Transform the referenced subject into a charming translucent jelly sculpture. Preserve the recognizable "
        "silhouette, face design, hairstyle, colors, outfit shapes, and signature accessories. Use glossy soft-gel "
        "material, subtle internal bubbles, subsurface scattering, rounded edges, colorful reflections, and a neat "
        "studio display surface. Keep the result cute, coherent, and physically believable."
    ),
    "变COS": (
        "Create a high-quality real-world cosplay photograph of the referenced character. The cosplayer must match "
        "the character's identity, hairstyle, hair color, costume structure, patterns, accessories, makeup language, "
        "and pose as closely as possible. Use carefully crafted costume materials, convention-grade styling, natural "
        "human anatomy, realistic lighting, and a professional camera look."
    ),
    "漫画封面": (
        "Design a premium vertical manga cover featuring the referenced subject as the clear protagonist. Preserve "
        "the original identity and art style, then build a strong cover composition with expressive posing, layered "
        "background storytelling, dynamic lighting, clean negative space for a title, refined linework, rich color "
        "separation, and print-ready detail. Do not add unreadable random text."
    ),
    "证件照": (
        "Create a clean formal ID-photo portrait of the referenced subject. Preserve identity, face shape, hairstyle, "
        "age appearance, and core character features. Use a centered front-facing pose, neutral pleasant expression, "
        "tidy clothing, even shadow-free lighting, plain light background, accurate proportions, and crisp professional "
        "photo quality. Keep accessories only when they are essential to identity."
    ),
    "男友视角": (
        "Create a natural first-person companion-perspective lifestyle photo of the referenced adult subject. Preserve "
        "identity, hairstyle, outfit details, and age appearance. Use comfortable conversational distance, candid eye "
        "contact, relaxed body language, realistic surroundings, and gentle everyday lighting, as if photographed by "
        "a close companion during an ordinary outing. Keep the composition tasteful and non-explicit."
    ),
    "漏腰": (
        "Restyle the referenced adult subject in a tasteful modern outfit with a small, fashion-oriented midriff "
        "detail, such as a cropped jacket or coordinated high-waist set. Preserve identity, hairstyle, age appearance, "
        "body proportions, and the original design language. Use confident but natural posing, editorial street-fashion "
        "lighting, and a fully clothed, non-explicit composition without voyeuristic emphasis."
    ),
}


def serialized_default_presets() -> List[str]:
    return [f"{name}:{prompt}" for name, prompt in ADDITIONAL_DEFAULT_PRESETS.items()]
