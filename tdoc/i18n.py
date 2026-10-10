# -*- coding: utf-8 -*-
"""화면 언어(한국어·영어). 문구는 쓰는 자리에서 L("한국어", "English")로 고른다."""
import locale
import os
import sys

LANGS = ("ko", "en")
LANG = "ko"


def detect():
    """TOKENRX_LANG 환경 변수 → 시스템 언어 순서로 고른다. 한국어 환경이 아니면 영어."""
    env = os.environ.get("TOKENRX_LANG", "").lower()
    if env in LANGS:
        return env
    candidates = [os.environ.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        candidates.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x12:   # LANG_KOREAN
                return "ko"
        except (AttributeError, OSError):
            pass
    return "ko" if any(c.lower().startswith(("ko", "korean")) for c in candidates) else "en"


def set_lang(lang):
    global LANG
    LANG = lang if lang in LANGS else detect()


def L(ko, en):
    return ko if LANG == "ko" else en


def tr(value):
    """(한국어, English) 쌍이나 문자열을 현재 언어로."""
    return value if isinstance(value, str) else L(*value)
