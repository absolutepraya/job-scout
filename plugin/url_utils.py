"""URL canonicalization for job posting links."""
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


def strip_brackets(url: str) -> str:
    """Discord embed-suppression wraps URLs in <>. Strip them."""
    s = url.strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
    return s


def canonicalize_url(url: str) -> str:
    """Strip platform-specific tracking params. Stable across copy/paste sources."""
    s = strip_brackets(url)
    parsed = urlparse(s)
    host = parsed.netloc.lower()

    if "linkedin.com" in host:
        # LinkedIn job URLs are uniquely identified by /jobs/view/<id>. Drop all query.
        return urlunparse(parsed._replace(query="", fragment=""))

    if "indeed.com" in host:
        # Indeed: keep only jk=<id>, drop everything else.
        qs = dict(parse_qsl(parsed.query))
        jk = qs.get("jk")
        if jk:
            new_query = urlencode({"jk": jk})
            return urlunparse(parsed._replace(query=new_query, fragment=""))

    return s
