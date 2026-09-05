"""Automated extraction of 별표(appendix) tables attached to official notices.

The National Law Information API returns each notice's article body, but the
CAS/substance tables that matter for screening live in a *separate attached
file* (별표서식PDF파일링크), not in the article text. This module:

1. Finds those attachment links inside an already-fetched notice payload.
2. Downloads the PDF (the API exposes it as a plain HTTPS file, no OC needed).
3. Parses the appendix's designation/substance/CAS table structure.

The parser is layout-specific to the 고유번호/물질명/CAS No. table format used
by the chemical-substance designation notices (제한물질·금지물질·사고대비물질·
허가물질 지정 고시류).  A different notice layout will not be recognized, and
this always produces *candidates* for human review, never approved rules —
PDF table extraction is fundamentally too fragile to trust unattended, and a
one-off successful parse of one document does not guarantee every future
revision keeps the same layout.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any, Iterable, Optional

import pandas as pd
import requests

from scope_engine import clean


BASE_URL = "https://www.law.go.kr"

DESIG_RE = re.compile(r"^0\d-\d-\d+$")
INLINE_CODE_RE = re.compile(r"^(0\d-\d-\d+)\s+(.*)$")
ITEM_RE = re.compile(r"^(\d{1,2})\s+(.*?)\s*(\d{2,7}-\d{2}-\d)\s*$")
CATCHALL_RE = re.compile(r"^(\d{1,2})\s+(.+?)\s*-?\s*$")
def _loose_word(word: str) -> str:
    """Build a pattern for `word` that tolerates a line-wrap landing between
    any two of its characters (observed splitting operator words like 이상
    mid-syllable in real notices)."""
    return r"\s*".join(re.escape(ch) for ch in word)


THRESHOLD_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*("
    + "|".join(_loose_word(word) for word in ["이상", "초과", "이하", "미만"])
    + ")"
)
HEADER_NOISE_RE = re.compile(r"고유번호\s*물\s*질\s*명\s*CAS\s*No\.?")


def _recursive_dicts(obj: Any) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _recursive_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _recursive_dicts(value)


def find_appendix_attachments(payload: Any) -> list[dict]:
    """Locate every 별표단위 attachment record inside a fetched notice payload.

    Returns one dict per attachment with a normalized ``pdf_path`` (relative,
    to append to :data:`BASE_URL`) when a PDF link is available.  Records that
    only carry an HWP link are still returned, with ``pdf_path`` left empty,
    so the caller can report "PDF 없음" instead of silently skipping them.
    """
    found: list[dict] = []
    for record in _recursive_dicts(payload):
        pdf_link = clean(record.get("별표서식PDF파일링크"))
        hwp_link = clean(record.get("별표서식파일링크"))
        if not pdf_link and not hwp_link:
            continue
        found.append(
            {
                "appendix_no": clean(record.get("별표번호")),
                "appendix_branch": clean(record.get("별표가지번호")),
                "appendix_kind": clean(record.get("별표구분")),
                "appendix_title": clean(record.get("별표제목")),
                "pdf_path": pdf_link,
                "hwp_path": hwp_link,
            }
        )
    return found


def download_attachment(path: str, timeout: int = 60) -> bytes:
    """Fetch one attachment file. ``path`` is the relative link from the API."""
    if not path:
        raise ValueError("첨부파일 경로가 없습니다.")
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


def _preprocess_lines(raw_text: str) -> list[str]:
    text = HEADER_NOISE_RE.sub("\n", raw_text)
    text = re.sub(r"(\d-\d)고유번호", r"\1\n", text)
    lines: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or re.fullmatch(r"\*+", line):
            continue
        inline = INLINE_CODE_RE.match(line)
        if inline:
            lines.append(inline.group(1))
            lines.append(inline.group(2))
        else:
            lines.append(line)
    return _merge_wrapped_catchall_lines(lines)


_NUM_DASH_ONLY_RE = re.compile(r"^(\d{1,2})\s*-\s*$")


def _merge_wrapped_catchall_lines(lines: list[str]) -> list[str]:
    """Reassemble a catch-all row whose item number landed on its own line
    with only a placeholder dash, splitting the row's descriptive text across
    the lines immediately before and after it (observed in real notices, e.g.
    "...1%이상 함유한\n8 -\n혼합물")."""
    merged: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        marker = _NUM_DASH_ONLY_RE.match(line)
        if (
            marker
            and merged
            and not ITEM_RE.match(merged[-1])
            and not DESIG_RE.match(merged[-1])
        ):
            previous = merged.pop()
            following = ""
            if (
                i + 1 < len(lines)
                and not ITEM_RE.match(lines[i + 1])
                and not DESIG_RE.match(lines[i + 1])
                and not _NUM_DASH_ONLY_RE.match(lines[i + 1])
            ):
                following = lines[i + 1]
                i += 1
            text = f"{previous.strip()} {following.strip()}".strip()
            merged.append(f"{marker.group(1)} {text} -")
        else:
            merged.append(line)
        i += 1
    return merged


def parse_designation_cas_table(raw_text: str) -> pd.DataFrame:
    """Parse a 고유번호/물질명/CAS No. appendix table into structured rows.

    Each output row is one CAS entry (or a catch-all "기타/그 밖의" row with an
    empty CAS) tagged with the 고유번호 it belongs to, the group's descriptive
    title, and the concentration threshold parsed from that title when present.
    Designation codes are detected regardless of whether the PDF placed them on
    their own line or glued them to the following item line, since both forms
    were observed in real notices.
    """
    lines = _preprocess_lines(raw_text)
    item_idx = [i for i, line in enumerate(lines) if ITEM_RE.match(line)]
    if not item_idx:
        return pd.DataFrame(
            columns=[
                "designation_id",
                "designation_id_inferred",
                "item_no",
                "substance_name",
                "cas",
                "is_catch_all",
                "group_title",
                "concentration_threshold_pct",
                "concentration_operator",
            ]
        )

    boundaries: list[int] = []
    last = 10_000
    for i in item_idx:
        no = int(ITEM_RE.match(lines[i]).group(1))
        if no <= last:
            boundaries.append(i)
        last = no
    boundaries.append(len(lines))
    spans = list(zip(boundaries, boundaries[1:]))

    def _last_item_in_span(s: int, e: int) -> int:
        return max(i for i in item_idx if s <= i < e)

    tails = [lines[_last_item_in_span(s, e) + 1 : e] for s, e in spans]
    preamble = lines[: boundaries[0]]

    def _catchall_looks_complete(raw_line: str, accumulated_text: str) -> bool:
        """A catch-all clause conventionally ends with the placeholder dash,
        the standard '...함유한 혼합물' closing phrase, or an explicit
        '...제외한다.' exclusion sentence. If none of those are present the
        clause is assumed to still be wrapping onto the next line."""
        if raw_line.rstrip().endswith("-"):
            return True
        tail_text = accumulated_text.strip()
        return tail_text.endswith("혼합물") or tail_text.endswith("제외한다.")

    def _consume_catchall_tail(tail: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
        """Pull every catch-all item out of a block's tail, merging a wrapped
        description onto the item that started it even when the continuation
        line carries no number of its own (e.g. a long "기타 ~은 제외한다"
        clause split across two physical lines). A designation code found
        mid-scan is skipped without ending the scan, matching how it can
        land inside a tail rather than only at its edges. An unnumbered line
        only continues the current item while that item still looks
        unfinished; once it looks complete, an unnumbered line is treated as
        the start of the next block's title instead."""
        items: list[tuple[str, str]] = []
        current_no: Optional[str] = None
        current_parts: list[str] = []
        current_complete = True
        i = 0
        while i < len(tail):
            line = tail[i]
            if DESIG_RE.match(line):
                i += 1
                continue
            match = CATCHALL_RE.match(line)
            if match:
                if current_no is not None:
                    items.append((current_no, " ".join(current_parts)))
                current_no, current_parts = match.group(1), [match.group(2)]
                current_complete = _catchall_looks_complete(line, match.group(2))
            elif current_no is not None and not current_complete:
                current_parts.append(line)
                current_complete = _catchall_looks_complete(line, " ".join(current_parts))
            else:
                break
            i += 1
        if current_no is not None:
            items.append((current_no, " ".join(current_parts)))
        return items, tail[i:]

    own_catch_all: list[list[tuple[str, str]]] = [[] for _ in spans]
    next_title: list[list[str]] = [preamble]
    for k, tail in enumerate(tails):
        items, remainder = _consume_catchall_tail(tail)
        own_catch_all[k] = items
        next_title.append(remainder)

    rows: list[dict] = []
    for k, (s, e) in enumerate(spans):
        title_lines = next_title[k]
        code = next(
            (line for line in [*title_lines, *lines[s:e]] if DESIG_RE.match(line)), None
        )
        inferred = code is None
        if inferred:
            code = f"UNKNOWN-{k + 1}"
        title_text = " ".join(line for line in title_lines if not DESIG_RE.match(line)).strip()
        threshold_match = THRESHOLD_RE.search(title_text)
        threshold_pct = float(threshold_match.group(1)) if threshold_match else None
        operator = (
            re.sub(r"\s+", "", threshold_match.group(2)) if threshold_match else ""
        )

        for i in range(s, e):
            match = ITEM_RE.match(lines[i])
            if not match:
                continue
            rows.append(
                {
                    "designation_id": code,
                    "designation_id_inferred": inferred,
                    "item_no": int(match.group(1)),
                    "substance_name": match.group(2).strip(),
                    "cas": match.group(3),
                    "is_catch_all": False,
                    "group_title": title_text,
                    "concentration_threshold_pct": threshold_pct,
                    "concentration_operator": operator,
                }
            )
        for item_no, name in own_catch_all[k]:
            rows.append(
                {
                    "designation_id": code,
                    "designation_id_inferred": inferred,
                    "item_no": int(item_no),
                    "substance_name": name.strip(),
                    "cas": "",
                    "is_catch_all": True,
                    "group_title": title_text,
                    "concentration_threshold_pct": threshold_pct,
                    "concentration_operator": operator,
                }
            )
    return pd.DataFrame(rows)


FLAT_LIST_NOISE_RE = re.compile(
    r"\[별표\s*\d+\]"
    r"|[가-힣]{2,15}의\s*지정"
    r"|번호\s*[가-힣]{0,15}\[영문명.*?CAS\s*No\.?\)\]"
    r"|법제처\s*-?\s*\d+\s*/?\s*\d*\s*-?\s*국가법령정보센터"
    r"|-\s*\d+\s*/\s*\d+\s*-"
)
FLAT_LIST_CAS_RE = re.compile(r"\d{2,7}-\d{2}-\d")
FLAT_LIST_BRACKET_RE = re.compile(r"\[(.+?)\]")


def _loose(word: str) -> str:
    """Build a pattern for `word` that tolerates a wrap-point landing between
    any two of its characters (observed even mid-syllable in real notices)."""
    return r"\s*".join(re.escape(ch) for ch in word)


FLAT_LIST_ENTRY_END_RE = re.compile(
    r"및\s*"
    + _loose("이를")
    + r"\s*(\d+(?:\.\d+)?)\s*%\s*(이상|초과|이하|미만)\s*"
    + _loose("함유한")
    + r"\s*"
    + _loose("혼합물")
    + r"(?:"
    + _loose("질은")
    + r"\s*"
    + _loose("제외한다")
    + r"\.|\.\s*다만[^.]*"
    + _loose("제외한다")
    + r"\.)?"
)


def parse_flat_notice_list(raw_text: str) -> pd.DataFrame:
    """Parse a "번호 + one-sentence-per-item" style appendix, used by notices
    such as 사고대비물질의 지정 where every row states the Korean name, English
    name(s), CAS number(s), and its OWN concentration threshold in one sentence,
    with no 고유번호 grouping at all.

    The PDF wraps long entries across lines, and the item-number cell (rendered
    as a separate merged-cell row) can land anywhere inside the wrapped text —
    including splitting a single word in half. Item numbers are therefore not
    read from the text at all: they are discarded, and items are counted in the
    order their closing phrase ("...함유한 혼합물") is found, which is reliable
    because that phrase appears exactly once per item.
    """
    # The column header "번호 <카테고리명>[영문명 및 화학물질 식별번호(CAS No.)]"
    # repeats at the top of every page (with the category name varying by
    # notice, e.g. 사고대비물질 vs 허가물질), so the FIRST occurrence marks
    # where the table begins; anchoring on the last one would skip every page
    # except the final one.
    marker_positions = [
        m.start()
        for m in re.finditer(r"번호\s*[가-힣]{0,15}\[영문명\s*및\s*화학물질\s*식별번호", raw_text)
    ]
    table_text = raw_text[marker_positions[0] :] if marker_positions else raw_text
    cleaned = FLAT_LIST_NOISE_RE.sub(" ", table_text)
    tokens = [tok for tok in cleaned.split() if not re.fullmatch(r"\d{1,3}", tok)]
    blob = " ".join(tokens)

    matches = list(FLAT_LIST_ENTRY_END_RE.finditer(blob))
    if not matches:
        return pd.DataFrame(
            columns=[
                "designation_id",
                "designation_id_inferred",
                "item_no",
                "substance_name",
                "cas",
                "is_catch_all",
                "group_title",
                "concentration_threshold_pct",
                "concentration_operator",
            ]
        )

    rows: list[dict] = []
    last = 0
    for item_no, match in enumerate(matches, start=1):
        segment = blob[last:match.end()].strip()
        last = match.end()
        bracket = FLAT_LIST_BRACKET_RE.search(segment)
        korean_name = segment[: bracket.start()].strip() if bracket else segment
        inside = bracket.group(1) if bracket else ""
        parts = [p.strip() for p in inside.split(";")]
        cas_candidates = [p.strip() for p in (parts[-1] if parts else "").split(",")]
        cas_list = [c for c in cas_candidates if FLAT_LIST_CAS_RE.fullmatch(c)]
        exception_text = ""
        if "다만" in segment:
            exception_text = segment[segment.index("다만") :].strip()
        threshold = float(match.group(1))
        operator = match.group(2)
        if not cas_list:
            rows.append(
                {
                    "designation_id": f"ITEM-{item_no:03d}",
                    "designation_id_inferred": False,
                    "item_no": item_no,
                    "substance_name": korean_name,
                    "cas": "",
                    "is_catch_all": True,
                    "group_title": segment,
                    "concentration_threshold_pct": threshold,
                    "concentration_operator": operator,
                }
            )
        for cas in cas_list:
            rows.append(
                {
                    "designation_id": f"ITEM-{item_no:03d}",
                    "designation_id_inferred": False,
                    "item_no": item_no,
                    "substance_name": korean_name,
                    "cas": cas,
                    "is_catch_all": False,
                    "group_title": segment if not exception_text else f"{segment} (제외: {exception_text})",
                    "concentration_threshold_pct": threshold,
                    "concentration_operator": operator,
                }
            )
    return pd.DataFrame(rows)


def parse_appendix_pdf_bytes(data: bytes) -> pd.DataFrame:
    """Extract text from a downloaded PDF and run the appropriate table parser.

    Tries the 고유번호/물질명/CAS No. grouped-table parser first; if that finds
    nothing, falls back to the flat "번호 + one-sentence-per-item" parser used
    by notices like 사고대비물질의 지정. If neither recognizes the layout, an
    empty frame is returned rather than guessing.
    """
    import pdfplumber  # imported lazily; only needed when a PDF is parsed

    with pdfplumber.open(BytesIO(data)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    grouped = parse_designation_cas_table(text)
    if not grouped.empty:
        return grouped
    return parse_flat_notice_list(text)


def _infer_regime(title_text: str, group_title: str, default: str) -> str:
    combined = f"{title_text} {group_title}"
    has_prohibited = "금지물질" in combined
    has_restricted = "제한물질" in combined
    if has_prohibited and not has_restricted:
        return "PROHIBITED"
    if has_restricted and not has_prohibited:
        return "RESTRICTED"
    return default


CROSS_RULE_COLUMNS = [
    "regime",
    "rule_id",
    "designation_id",
    "substance_name",
    "scope_type",
    "direct_cas",
    "source_text",
    "concentration_threshold_pct",
    "concentration_operator",
    "use_condition",
    "quantity_threshold",
    "quantity_unit",
    "temporal_condition",
    "authorization_condition",
    "source_name",
    "source_reference",
    "source_locator",
    "official_serial",
    "official_issue_date",
    "official_effective_date",
    "approval_status",
    "extraction_status",
    "designation_id_inferred",
]


def _safe_item_no(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_candidate_rows(
    parsed: pd.DataFrame,
    default_regime: str,
    attachment: dict,
    notice_info: dict,
) -> pd.DataFrame:
    """Map parsed appendix rows into the cross-regime candidate rule schema.

    Rows always carry ``approval_status=CANDIDATE_REVIEW_REQUIRED``: an
    automatically parsed PDF table is a strong starting point, not a verified
    rule, until a person confirms the designation codes and thresholds against
    the source document at least once.
    """
    if parsed is None or parsed.empty:
        return pd.DataFrame(columns=CROSS_RULE_COLUMNS)
    rows: list[dict] = []
    appendix_title = clean(attachment.get("appendix_title"))
    for _, item in parsed.iterrows():
        cas = clean(item.get("cas"))
        regime = _infer_regime(appendix_title, clean(item.get("group_title")), default_regime)
        threshold = item.get("concentration_threshold_pct")
        rows.append(
            {
                "regime": regime,
                "rule_id": f"APX-{clean(item.get('designation_id'))}-{_safe_item_no(item.get('item_no')):02d}",
                "designation_id": clean(item.get("designation_id")),
                "substance_name": clean(item.get("substance_name")),
                "scope_type": "MANUAL_REVIEW" if not cas else "DIRECT_CAS",
                "direct_cas": cas,
                "source_text": (
                    f"{clean(item.get('group_title'))} / 구체적 목록 제{_safe_item_no(item.get('item_no'))}호 "
                    f"{clean(item.get('substance_name'))}"
                ),
                "concentration_threshold_pct": threshold if threshold is not None else "",
                "concentration_operator": clean(item.get("concentration_operator")),
                "use_condition": "",
                "quantity_threshold": "",
                "quantity_unit": "",
                "temporal_condition": "",
                "authorization_condition": "",
                "source_name": appendix_title or clean(notice_info.get("official_title")),
                "source_reference": clean(notice_info.get("official_notice")),
                "source_locator": (
                    f"{clean(attachment.get('appendix_kind'))} {clean(attachment.get('appendix_no'))} "
                    f"{clean(item.get('designation_id'))} 제{_safe_item_no(item.get('item_no'))}호"
                ),
                "official_serial": clean(notice_info.get("official_serial")),
                "official_issue_date": clean(notice_info.get("issue_date")),
                "official_effective_date": clean(notice_info.get("effective_date")),
                "approval_status": "CANDIDATE_REVIEW_REQUIRED",
                "extraction_status": "OFFICIAL_APPENDIX_PDF_AUTO_EXTRACTED",
                "designation_id_inferred": bool(item.get("designation_id_inferred")),
            }
        )
    return pd.DataFrame(rows, columns=CROSS_RULE_COLUMNS)


def extract_candidates_from_payload(
    payload: Any,
    default_regime: str,
    notice_info: dict,
    timeout: int = 90,
) -> tuple[pd.DataFrame, dict]:
    """Find, download, and parse every appendix attached to an already-fetched
    admrul body. Use this when the caller has already made the lawService.do
    call (e.g. alongside the existing article-text extraction) so the
    attachment step does not repeat that request."""
    attachments = find_appendix_attachments(payload)
    if not attachments:
        return pd.DataFrame(columns=CROSS_RULE_COLUMNS), {
            "status": "NO_ATTACHMENT",
            "status_ko": "첨부된 별표 없음",
            "attachment_count": 0,
            "parsed_attachment_count": 0,
            "extracted_rows": 0,
            "attachments": [],
        }

    ABOLISHED_TITLE_RE = re.compile(r"^<?\s*삭\s*제\s*>?$")

    frames: list[pd.DataFrame] = []
    per_attachment: list[dict] = []
    for attachment in attachments:
        label = clean(attachment.get("appendix_title")) or clean(attachment.get("appendix_no"))
        if ABOLISHED_TITLE_RE.match(label):
            # The appendix number itself was abolished in a past amendment;
            # the government's database still lists the slot but there is no
            # table to read. This is expected and not a parsing failure.
            per_attachment.append({"appendix": label, "status": "ABOLISHED_APPENDIX"})
            continue
        if not attachment.get("pdf_path"):
            per_attachment.append({"appendix": label, "status": "PDF_LINK_MISSING"})
            continue
        try:
            data = download_attachment(attachment["pdf_path"], timeout=timeout)
            parsed = parse_appendix_pdf_bytes(data)
            if parsed.empty:
                per_attachment.append({"appendix": label, "status": "TABLE_FORMAT_NOT_RECOGNIZED"})
                continue
            candidates = build_candidate_rows(parsed, default_regime, attachment, notice_info)
        except Exception as exc:
            per_attachment.append(
                {
                    "appendix": label,
                    "status": "DOWNLOAD_OR_PARSE_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        frames.append(candidates)
        per_attachment.append(
            {
                "appendix": label,
                "status": "PARSED",
                "rows": len(candidates),
                "designation_groups": candidates["designation_id"].nunique(),
                "inferred_designation_rows": int(candidates["designation_id_inferred"].sum()),
            }
        )

    combined = (
        pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=CROSS_RULE_COLUMNS)
    )
    parsed_ok = sum(1 for item in per_attachment if item["status"] == "PARSED")
    status = {
        "status": "PARSED" if parsed_ok else "NO_ROWS_EXTRACTED",
        "status_ko": (
            f"별표 {len(attachments)}개 중 {parsed_ok}개 자동 인식"
            if parsed_ok
            else "별표를 찾았으나 표 형식을 인식하지 못했습니다"
        ),
        "attachment_count": len(attachments),
        "parsed_attachment_count": parsed_ok,
        "extracted_rows": len(combined),
        "attachments": per_attachment,
    }
    return combined, status


def fetch_appendix_candidates(
    oc: str,
    default_regime: str,
    notice_info: dict,
    timeout: int = 90,
) -> tuple[pd.DataFrame, dict]:
    """Standalone convenience wrapper: fetches the notice body itself, then
    delegates to :func:`extract_candidates_from_payload`. Prefer calling
    :func:`extract_candidates_from_payload` directly when the body has already
    been fetched elsewhere, to avoid a duplicate request."""
    serial = clean(notice_info.get("official_serial"))
    if not oc:
        return pd.DataFrame(columns=CROSS_RULE_COLUMNS), {
            "status": "API_KEY_MISSING",
            "status_ko": "API 인증정보 미설정",
        }
    if not serial:
        return pd.DataFrame(columns=CROSS_RULE_COLUMNS), {
            "status": "NO_NOTICE",
            "status_ko": "대상 고시 일련번호 없음",
        }
    try:
        response = requests.get(
            f"{BASE_URL}/DRF/lawService.do",
            params={"OC": oc, "target": "admrul", "ID": serial, "type": "JSON"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return pd.DataFrame(columns=CROSS_RULE_COLUMNS), {
            "status": "BODY_FETCH_ERROR",
            "status_ko": "고시 본문 조회 실패",
            "message": f"{type(exc).__name__}: {exc}".replace(oc, "***"),
        }
    return extract_candidates_from_payload(payload, default_regime, notice_info, timeout=timeout)
