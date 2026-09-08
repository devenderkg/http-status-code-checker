"""
HTTP Status Checker — Bulk URL & Redirect Checker
Production-grade Streamlit application.

Run with:
    streamlit run app.py
"""

import io
import csv
import json
import time
import socket
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Page configuration (also doubles as basic on-page / entity SEO signal)
# ----------------------------------------------------------------------------
st.set_page_config(
    page_title="HTTP Status Checker — Bulk URL & Redirect Checker Tool",
    page_icon="🟢",
    layout="wide",
    menu_items={
        "About": (
            "HTTP Status Checker is a free bulk URL status and redirect "
            "checker. Check HTTP status codes, redirect chains, and "
            "response times for thousands of URLs at once."
        )
    },
)

# ----------------------------------------------------------------------------
# Styling
# ----------------------------------------------------------------------------
st.markdown(
    """
    <style>
        .block-container {padding-top: 2rem; padding-bottom: 3rem;}
        .status-2xx {color: #16a34a; font-weight: 600;}
        .status-3xx {color: #ca8a04; font-weight: 600;}
        .status-4xx {color: #dc2626; font-weight: 600;}
        .status-5xx {color: #b91c1c; font-weight: 600;}
        .status-err {color: #6b7280; font-weight: 600;}
        .metric-card {
            background: #f8fafc; border: 1px solid #e2e8f0;
            border-radius: 10px; padding: 14px 16px;
        }
        .support-box {
            
            border: 1px solid #fbbf24; border-radius: 12px;
            padding: 18px 22px; margin-top: 10px;
        }
        code {word-break: break-all;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; HTTPStatusCheckerBot/1.0; "
        "+https://devendergupta.netlify.app/)"
    )
}

STATUS_MEANINGS = {
    200: "OK", 201: "Created", 204: "No Content",
    301: "Moved Permanently", 302: "Found (Temporary Redirect)",
    303: "See Other", 307: "Temporary Redirect", 308: "Permanent Redirect",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    410: "Gone", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}

# ----------------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------------
def normalize_url(raw: str) -> str:
    """Clean a single URL / add scheme if missing."""
    u = raw.strip()
    if not u:
        return ""
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    return u


def parse_input_urls(text_block: str, uploaded_files) -> list:
    """Merge pasted URLs + uploaded TXT/CSV URLs, dedupe, preserve order."""
    urls = []

    if text_block:
        for line in text_block.splitlines():
            line = line.strip()
            if line:
                urls.append(line)

    for f in uploaded_files or []:
        content = f.read()
        try:
            text = content.decode("utf-8", errors="ignore")
        except Exception:
            continue

        if f.name.lower().endswith(".csv"):
            reader = csv.reader(io.StringIO(text))
            for row in reader:
                for cell in row:
                    cell = cell.strip()
                    if cell.lower().startswith(("http://", "https://", "www.")):
                        urls.append(cell)
        else:  # .txt or generic
            for line in text.splitlines():
                line = line.strip()
                if line:
                    urls.append(line)

    cleaned, seen = [], set()
    for u in urls:
        norm = normalize_url(u)
        if norm and norm not in seen:
            seen.add(norm)
            cleaned.append(norm)
    return cleaned


def classify_error(exc: Exception) -> str:
    """Rough classification of failure stage for a clearer error reason."""
    msg = str(exc).lower()
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS/SSL handshake failed"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "Connection timed out (TCP)"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "Server response timed out"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "Too many redirects (redirect loop)"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if "name or service not known" in msg or "getaddrinfo failed" in msg or "nodename nor servname" in msg:
            return "DNS resolution failed"
        if "connection refused" in msg:
            return "Connection refused (TCP)"
        if "reset by peer" in msg:
            return "Connection reset by peer"
        return "Connection error"
    if isinstance(exc, requests.exceptions.MissingSchema):
        return "Invalid URL (missing scheme)"
    if isinstance(exc, requests.exceptions.InvalidURL):
        return "Malformed URL"
    return f"Request failed: {type(exc).__name__}"


def status_bucket(code):
    if code is None:
        return "error"
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


def check_url(url: str, method: str, timeout: float, verify_ssl: bool,
               max_redirects: int, follow_redirects: bool) -> dict:
    """Perform the HTTP status / redirect check for a single URL."""
    result = {
        "URL": url,
        "Status Code": None,
        "Status Meaning": "",
        "Final URL": "",
        "Redirect Count": 0,
        "Redirect Chain": "",
        "Response Time (ms)": None,
        "Content Type": "",
        "Content Length": "",
        "Server": "",
        "Error Reason": "",
        "Timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    session = requests.Session()
    session.max_redirects = max_redirects

    start = time.perf_counter()
    try:
        resp = session.request(
            method,
            url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=follow_redirects,
            verify=verify_ssl,
            stream=True,
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

        chain = [f"{h.status_code} {h.url}" for h in resp.history]
        result.update({
            "Status Code": resp.status_code,
            "Status Meaning": STATUS_MEANINGS.get(resp.status_code, ""),
            "Final URL": resp.url,
            "Redirect Count": len(resp.history),
            "Redirect Chain": " → ".join(chain) if chain else "",
            "Response Time (ms)": elapsed_ms,
            "Content Type": resp.headers.get("Content-Type", ""),
            "Content Length": resp.headers.get(
                "Content-Length",
                str(len(resp.content)) if method.upper() == "GET" else ""
            ),
            "Server": resp.headers.get("Server", ""),
        })
        resp.close()

    except requests.exceptions.RequestException as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        result["Response Time (ms)"] = elapsed_ms
        result["Error Reason"] = classify_error(exc)
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        result["Response Time (ms)"] = elapsed_ms
        result["Error Reason"] = f"Unexpected error: {exc}"

    return result


def run_bulk_check(urls, method, timeout, verify_ssl, max_redirects,
                    follow_redirects, workers, progress_cb=None):
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                check_url, u, method, timeout, verify_ssl,
                max_redirects, follow_redirects
            ): u for u in urls
        }
        done = 0
        total = len(urls)
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            if progress_cb:
                progress_cb(done, total)
    # preserve original input order
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order.get(r["URL"], 0))
    return results


def style_status(val):
    if pd.isna(val) or val == "":
        return "color:#6b7280;font-weight:600;"
    try:
        code = int(val)
    except (ValueError, TypeError):
        return ""
    bucket = status_bucket(code)
    colors = {"2xx": "#16a34a", "3xx": "#ca8a04", "4xx": "#dc2626", "5xx": "#b91c1c"}
    return f"color:{colors.get(bucket, '#111827')};font-weight:600;"


# ----------------------------------------------------------------------------
# Session state
# ----------------------------------------------------------------------------
if "results" not in st.session_state:
    st.session_state.results = []
if "last_run_meta" not in st.session_state:
    st.session_state.last_run_meta = {}

# ----------------------------------------------------------------------------
# Header / Hero (SEO-relevant on-page content, entity-focused, no stuffing)
# ----------------------------------------------------------------------------
st.title("🟢 HTTP Status Checker")
st.markdown(
    "#### Bulk URL & Redirect Checker — check status codes, redirect chains "
    "and response times for thousands of links at once"
)
st.markdown(
    "Paste your links or upload a file to check HTTP status codes in bulk. "
    "This **HTTP status checker** validates every URL, follows redirects, "
    "and reports the exact response code — 200, 301, 302, 404, 410, 429, "
    "503, and more — so you always know whether a page is live, moved, or "
    "broken. It works equally well as an **HTTPS status checker**, a "
    "**bulk URL redirect checker** for SEO redirect audits, and a general "
    "**bulk redirect checker** for QA and DevOps link testing."
)

st.divider()

# ----------------------------------------------------------------------------
# Sidebar — settings
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Check Settings")

    method = st.selectbox(
        "Request method",
        ["HEAD", "GET"],
        index=1,
        help="HEAD is faster but some servers don't support it correctly. "
             "GET is the most reliable way to test redirects and status codes.",
    )
    follow_redirects = st.checkbox("Follow redirects", value=True)
    max_redirects = st.slider("Max redirects to follow", 1, 20, 10,
                               disabled=not follow_redirects)
    timeout = st.slider("Timeout per URL (seconds)", 1, 30, 10)
    verify_ssl = st.checkbox("Verify SSL/TLS certificates", value=True)
    workers = st.slider(
        "Concurrency (parallel workers)", 1, 100, 25,
        help="Higher = faster checks, but be considerate of target servers.",
    )

    st.divider()
    st.caption(
        "This tool performs lightweight status/redirect checks only. It is "
        "not a crawler, monitoring platform, or vulnerability scanner."
    )

# ----------------------------------------------------------------------------
# Input section
# ----------------------------------------------------------------------------
st.subheader("1. Add your URLs")

input_col1, input_col2 = st.columns([2, 1])

with input_col1:
    text_input = st.text_area(
        "Paste URLs (one per line)",
        height=180,
        placeholder="https://example.com\nhttps://example.com/blog\nexample.com/old-page",
    )

with input_col2:
    uploaded_files = st.file_uploader(
        "Or upload TXT / CSV",
        type=["txt", "csv"],
        accept_multiple_files=True,
        help="One URL per line (TXT) or URLs anywhere in the CSV cells.",
    )
    st.caption(
        "Supports bulk lists of any size. Large lists run concurrently so "
        "your browser won't freeze."
    )

urls = parse_input_urls(text_input, uploaded_files)

if urls:
    st.success(f"✅ {len(urls)} unique, valid URL(s) ready to check.")
else:
    st.info("Paste URLs above or upload a file to get started.")

run_clicked = st.button(
    "🚀 Check Status Codes", type="primary", disabled=(len(urls) == 0)
)

# ----------------------------------------------------------------------------
# Run the check
# ----------------------------------------------------------------------------
if run_clicked and urls:
    progress_bar = st.progress(0.0, text="Starting checks…")
    status_text = st.empty()

    def _progress(done, total):
        pct = done / total
        progress_bar.progress(pct, text=f"Checked {done} / {total} URLs…")

    t0 = time.perf_counter()
    results = run_bulk_check(
        urls, method, timeout, verify_ssl, max_redirects,
        follow_redirects, workers, progress_cb=_progress,
    )
    total_time = round(time.perf_counter() - t0, 2)

    progress_bar.progress(1.0, text="Done!")
    st.session_state.results = results
    st.session_state.last_run_meta = {
        "count": len(results),
        "duration": total_time,
        "run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# ----------------------------------------------------------------------------
# Results section
# ----------------------------------------------------------------------------
if st.session_state.results:
    st.divider()
    st.subheader("2. Results")

    df = pd.DataFrame(st.session_state.results)
    # Use pandas' nullable integer type so missing status codes (failed
    # requests) show as blank instead of forcing the whole column to float
    # (which would render "404.0" instead of "404").
    df["Status Code"] = df["Status Code"].astype("Int64")
    meta = st.session_state.last_run_meta

    total = len(df)
    ok = int((df["Status Code"].dropna().astype(int).between(200, 299)).sum())
    redirects = int((df["Status Code"].dropna().astype(int).between(300, 399)).sum())
    client_err = int((df["Status Code"].dropna().astype(int).between(400, 499)).sum())
    server_err = int((df["Status Code"].dropna().astype(int).between(500, 599)).sum())
    failed = int(df["Status Code"].isna().sum())

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total URLs", total)
    m2.metric("2xx OK", ok)
    m3.metric("3xx Redirect", redirects)
    m4.metric("4xx Error", client_err)
    m5.metric("5xx Error", server_err)
    m6.metric("Failed / No Response", failed)

    if meta:
        st.caption(
            f"Finished {meta['count']} checks in {meta['duration']}s "
            f"(run at {meta['run_at']})."
        )

    # Filters
    fcol1, fcol2, fcol3 = st.columns([1, 1, 2])
    with fcol1:
        bucket_filter = st.multiselect(
            "Filter by status type",
            ["2xx", "3xx", "4xx", "5xx", "error"],
            default=[],
        )
    with fcol2:
        only_redirects = st.checkbox("Only show URLs with redirects")
    with fcol3:
        search_term = st.text_input("Search URL contains…", "")

    filtered = df.copy()
    filtered["_bucket"] = filtered["Status Code"].apply(
        lambda c: status_bucket(int(c)) if pd.notna(c) else "error"
    )
    if bucket_filter:
        filtered = filtered[filtered["_bucket"].isin(bucket_filter)]
    if only_redirects:
        filtered = filtered[filtered["Redirect Count"] > 0]
    if search_term:
        filtered = filtered[filtered["URL"].str.contains(search_term, case=False, na=False)]
    filtered = filtered.drop(columns=["_bucket"])

    styler = filtered.style
    if hasattr(styler, "map"):
        styler = styler.map(style_status, subset=["Status Code"])
    else:  # older pandas versions
        styler = styler.applymap(style_status, subset=["Status Code"])

    st.dataframe(
        styler,
        use_container_width=True,
        height=460,
    )

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    st.subheader("3. Export results")
    e1, e2, e3 = st.columns(3)

    csv_bytes = filtered.to_csv(index=False).encode("utf-8")
    json_bytes = json.dumps(filtered.to_dict(orient="records"), indent=2).encode("utf-8")
    txt_lines = "\n".join(
        f"{row['URL']} -> {row['Status Code']} ({row['Status Meaning']}) "
        f"final: {row['Final URL']}" for _, row in filtered.iterrows()
    )

    with e1:
        st.download_button(
            "⬇️ Download CSV", data=csv_bytes,
            file_name="http_status_results.csv", mime="text/csv",
            use_container_width=True,
        )
    with e2:
        st.download_button(
            "⬇️ Download JSON", data=json_bytes,
            file_name="http_status_results.json", mime="application/json",
            use_container_width=True,
        )
    with e3:
        st.download_button(
            "⬇️ Download TXT", data=txt_lines.encode("utf-8"),
            file_name="http_status_results.txt", mime="text/plain",
            use_container_width=True,
        )

# ----------------------------------------------------------------------------
# SEO / entity content section (below the fold, natural, non-stuffed)
# ----------------------------------------------------------------------------
st.divider()

with st.expander("ℹ️ What is an HTTP Status Checker?", expanded=False):
    st.markdown(
        """
An **HTTP status checker** sends a request to a URL and reports the HTTP
response code the server returns — for example `200 OK`, `301 Moved
Permanently`, `404 Not Found`, or `503 Service Unavailable`. It's the
fastest way to confirm whether a page is live, redirected, broken, or
blocking access.

This tool works as a **URL status checker**, a **site status checker**,
and a full **URL checker bulk** utility — paste a single link or upload
thousands at once.

**Common use cases**

- **SEO audits** — find broken links, redirect chains, and orphaned 404s
  across a site.
- **Redirect testing** — confirm a **301 redirect**, **302 redirect**, or
  a full redirect chain resolves to the correct final URL.
- **Migration QA** — verify old URLs correctly redirect after a domain
  or CMS migration.
- **Uptime spot-checks** — quickly confirm whether pages return `200`,
  or errors like `401`, `403`, `429`, or `503`.
- **Link-list cleanup** — run a **bulk link checker** pass on affiliate
  links, backlinks, or a sitemap export.
        """
    )

with st.expander("🔁 Bulk URL Redirect Checker — how redirect chains work", expanded=False):
    st.markdown(
        """
A **bulk redirect checker** doesn't just report the final destination —
it should show every hop in between. This tool exposes the full
**redirect chain**: every intermediate `301`, `302`, `307`, or `308`
response, in order, until it reaches the final URL or hits an error.

Long redirect chains slow pages down and can dilute SEO signals, so
finding and flattening them is a common part of any **redirect audit**.
Use the **Redirect Chain** and **Redirect Count** columns above to spot
pages that redirect more than once and simplify them to a single direct
redirect.
        """
    )

with st.expander("❓ Frequently asked questions", expanded=False):
    st.markdown(
        """
**What does HTTP status code 503 mean?**
`503 Service Unavailable` means the server is temporarily unable to
handle the request — often due to maintenance or overload. It's usually
transient, so a **503 response code** should be re-checked after a
short delay.

**What does HTTP status code 401 mean?**
`401 Unauthorized` means the request lacks valid authentication
credentials. It's common when checking pages behind a login.

**What is a 302 redirect vs a 301 redirect?**
`301 Moved Permanently` tells crawlers and browsers the move is
permanent and to update their links. `302 Found` signals a temporary
redirect — the original URL should still be treated as canonical.

**What does status code 410 mean?**
`410 Gone` explicitly tells crawlers the resource was intentionally
removed and won't come back, which is stronger than a `404 Not Found`.

**Can this be used as a website redirect checker for an entire site?**
Yes — upload a sitemap export or crawl list as TXT/CSV and run it as a
**batch URL checker** to test redirects and status codes for the whole
site at once.

**Does this tool work as an HTTPS status checker?**
Yes. It supports both `http://` and `https://` URLs and verifies TLS
certificates by default (this can be turned off in settings for testing
self-signed certs).
        """
    )

st.caption(
    "This tool is designed for HTTP status and redirect checking only. "
    "It does not crawl entire websites, scan for vulnerabilities, render "
    "pages like a browser, or provide continuous uptime monitoring."
)

# ----------------------------------------------------------------------------
# Support section
# ----------------------------------------------------------------------------
st.markdown(
    """
<div class="support-box">
<h3>🌟 Support My Work!</h3>
<p>Hey there! I'm <a href="https://x.com/DevenderKG" target="_blank">Devender Gupta</a> 👋<br>
If you've found this HTTP status checker helpful, interesting, or useful, I'd really appreciate your support. ❤️</p>
<p>👉 Read my <a href="https://devendergupta.netlify.app/" target="_blank">SEO blog</a>, explore my work, and follow along for more!</p>
</div>
    """,
    unsafe_allow_html=True,
)
