import re


SUPPORTED_URL_RE = re.compile(
    r"(https?://)?("
    r"(www\.|vm\.|vt\.)?tiktok\.com/\S+"
    r"|(www\.)?instagram\.com/reels?/\S+"
    r")"
)


def find_video_url(text: str) -> str | None:
    match = SUPPORTED_URL_RE.search(text)
    return match.group(0) if match else None
