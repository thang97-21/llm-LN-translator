"""
Builder Agent Configuration - EPUB output settings.

Language-agnostic configuration for EPUB building.
Language-specific settings (titles, TOC labels) come from manifest.json.

Two standing caveats, documented so the next reader does not assume otherwise:

  - css_processor.py and font_processor.py have no callers anywhere in the
    repository. The live stylesheet is DEFAULT_CSS in agent.py, selected per
    device profile via _create_stylesheet().
  - get_fonts_to_embed() below names four Google Sans faces that are not in
    this repository. FontProcessor.validate_fonts() would raise on all four;
    nothing has noticed because nothing calls it. Nothing is embedded into any
    EPUB we build today.
"""

from pathlib import Path
from typing import Dict, Any, List
from src.Deepseek.common.config import load_config, TEMPLATES_DIR


# ============================================================================
# EPUB FORMAT SETTINGS
# ============================================================================

def get_epub_version() -> str:
    """
    Get EPUB version from configuration.

    Returns:
        'EPUB2' or 'EPUB3'
    """
    config = load_config()
    version = config.get('builder', {}).get('epub_version', '3.0')
    # Normalize version string
    if version in ('3.0', '3', 'EPUB3'):
        return 'EPUB3'
    elif version in ('2.0', '2', 'EPUB2'):
        return 'EPUB2'
    return 'EPUB3'  # Default to EPUB3


def get_css_template_path() -> Path:
    """Get path to CSS template."""
    config = load_config()
    css_path = config.get('builder', {}).get('css_template', 'templates/styles/main.css')
    return Path(css_path)


# ============================================================================
# FONT CONFIGURATION
# ============================================================================

def get_fonts_config() -> Dict[str, Any]:
    """Get font embedding configuration."""
    config = load_config()
    return config.get('builder', {}).get('fonts', {'enabled': True})


def get_fonts_to_embed() -> Dict[str, Dict[str, str]]:
    """
    Get font definitions for embedding.

    Returns:
        Dictionary mapping font filename to font metadata.
    """
    return {
        "GoogleSans-Regular.ttf": {
            "font_family": "Google Sans",
            "font_weight": "normal",
            "font_style": "normal",
            "id": "google-sans-regular",
        },
        "GoogleSans-Bold.ttf": {
            "font_family": "Google Sans",
            "font_weight": "bold",
            "font_style": "normal",
            "id": "google-sans-bold",
        },
        "GoogleSans-Italic.ttf": {
            "font_family": "Google Sans",
            "font_weight": "normal",
            "font_style": "italic",
            "id": "google-sans-italic",
        },
        "GoogleSans-BoldItalic.ttf": {
            "font_family": "Google Sans",
            "font_weight": "bold",
            "font_style": "italic",
            "id": "google-sans-bolditalic",
        },
    }


# Font CSS template
FONT_FACE_CSS_TEMPLATE = """@font-face {{
  font-family: "{font_family}";
  src: url("../fonts/{filename}") format("truetype");
  font-weight: {font_weight};
  font-style: {font_style};
}}
"""


# ============================================================================
# IMAGE PROCESSING
# ============================================================================

IMAGE_DEFAULTS: Dict[str, Any] = {
    'max_width_px': 1200,
    'max_height_px': 1800,
    'jpeg_quality': 85,
}


def get_image_config() -> Dict[str, Any]:
    """
    Get image processing configuration from config.yaml's builder.images.

    The previous version of this function defaulted to cover_max_width /
    illustration_max_width / quality -- key names that appear nowhere in
    config.yaml, which declares max_width_px / max_height_px / jpeg_quality.
    Two schemas, no overlap, and no caller to notice. Now the keys match the
    file, and partial config is merged over the defaults instead of replacing
    them wholesale.

    Returns:
        Dict with max_width_px, max_height_px and jpeg_quality.
    """
    config = load_config()
    configured = config.get('builder', {}).get('images', {}) or {}

    merged = dict(IMAGE_DEFAULTS)
    merged.update({k: v for k, v in configured.items() if v is not None})
    return merged


def get_xtc_config() -> Dict[str, Any]:
    """
    Get the optional XTC export configuration from config.yaml's builder.xtc.

    Empty by default: XTC export is opt-in and delegates to an external Node
    renderer that is not vendored here. See src/builder/xtc_export.py for what
    the keys mean and why the trade is what it is.
    """
    config = load_config()
    return dict(config.get('builder', {}).get('xtc', {}) or {})


def get_device_profile_name() -> str:
    """
    Get the default device profile name from config.yaml's
    builder.device_profile.

    Returns:
        Profile name; 'standard' when unset. Validation happens in
        device_profiles.resolve_profile(), which owns the registry and can
        report the valid options.
    """
    config = load_config()
    name = config.get('builder', {}).get('device_profile', 'standard')
    return str(name).strip().lower() if name else 'standard'


# ============================================================================
# XHTML STRUCTURE SETTINGS
# ============================================================================

# Files to discard from source EPUB (front/back matter to regenerate)
DISCARD_XHTML_FILES = {
    "p-titlepage.xhtml",
    "p-toc-001.xhtml",
    "p-toc-002.xhtml",
    "p-caution.xhtml",
    "p-colophon.xhtml",
    "p-colophon2.xhtml",
    "p-allcover-001.xhtml",
    "p-bookwalker.xhtml",
}


# ============================================================================
# SPINE DIRECTION
# ============================================================================

def get_spine_direction(source_lang: str) -> str:
    """
    Determine spine direction based on source language.

    Args:
        source_lang: Source language code (e.g., 'ja', 'zh', 'ar')

    Returns:
        'ltr' for left-to-right, 'rtl' for right-to-left
    """
    # RTL languages
    rtl_languages = {'ar', 'he', 'fa', 'ur'}

    # Vertical/RTL traditional (Japanese can be either, default to LTR for translation)
    if source_lang in rtl_languages:
        return 'rtl'

    return 'ltr'


# ============================================================================
# CSS FONT REPLACEMENTS
# ============================================================================

# Japanese font families to replace with target font
FONT_FAMILY_REPLACEMENTS = {
    "serif-ja": "Google Sans",
    "serif-ja-v": "Google Sans",
    "sans-serif-ja": "Google Sans",
    "sans-serif-ja-v": "Google Sans",
}


# ============================================================================
# PARAGRAPH FORMATTING
# ============================================================================

# Collapse consecutive blank lines
COLLAPSE_BLANK_LINES = True

# Keep 1 in N consecutive blank lines (1=all, 2=every other)
BLANK_LINE_FREQUENCY = 2
