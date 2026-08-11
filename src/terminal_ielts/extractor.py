"""Extract the reading question bank from the downloaded IELTS Atlas source."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

REGISTRATION_RE = re.compile(
    r"\.register\(\s*[\"'](?P<exam_id>[^\"']+)[\"']\s*,\s*(?P<payload>\{.*\})\s*\);",
    re.DOTALL,
)
COMPLETION_TOKEN = "[[[{question_id}]]]"
OPTION_CODE_RE = re.compile(
    r"^\s*([ivxlcdm]+|[A-Za-z]|\d+)(?:\s*[.)\u2014\u2013:\-]\s*|\s+)(.*?)\s*$",
    re.IGNORECASE,
)


def _strip_javascript_trailing_commas(value: str) -> str:
    """Remove JSON-incompatible trailing commas without touching quoted HTML."""
    output: list[str] = []
    in_string = False
    escaped = False
    for index, character in enumerate(value):
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            output.append(character)
            continue
        if character == ",":
            cursor = index + 1
            while cursor < len(value) and value[cursor].isspace():
                cursor += 1
            if cursor < len(value) and value[cursor] in "}]":
                continue
        output.append(character)
    return "".join(output)


def clean_text(value: str) -> str:
    """Turn source HTML into compact terminal-friendly text."""
    soup = BeautifulSoup(value or "", "html.parser")
    for image in soup.find_all("img"):
        image.replace_with(f"[Image: {image.get('alt') or image.get('src') or 'diagram'}]")
    text = soup.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def clean_passage(value: str) -> str:
    """Convert passage HTML to readable Markdown with real paragraph breaks."""
    soup = BeautifulSoup(value or "", "html.parser")
    for unwanted in soup.find_all(["script", "style"]):
        unwanted.decompose()

    blocks: list[str] = []
    heading_prefix = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}
    for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "blockquote", "li", "img"]):
        if element.name == "li" and element.find_parent("li"):
            continue
        if element.name == "img":
            text = f"*[Diagram: {element.get('alt') or element.get('src') or 'image'}]*"
        else:
            text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
        if not text:
            continue

        if element.name in heading_prefix:
            rendered = f"{heading_prefix[element.name]} {text}"
        elif element.name == "blockquote":
            rendered = f"> {text}"
        elif element.name == "li":
            rendered = f"- {text}"
        else:
            first_strong = element.find(["strong", "b"], recursive=False)
            if first_strong:
                lead = re.sub(r"\s+", " ", first_strong.get_text(" ", strip=True)).strip()
                if lead and len(lead) <= 3 and text.startswith(lead):
                    text = f"**{lead}**{text[len(lead):]}"
            rendered = text
        if not blocks or blocks[-1] != rendered:
            blocks.append(rendered)

    return "\n\n".join(blocks) if blocks else clean_text(value).replace("\n", "\n\n")


def _plain_layout(node: Tag | BeautifulSoup) -> str:
    """Preserve block layout while keeping inline controls inside their sentence."""
    block_tags = {"article", "blockquote", "div", "fieldset", "h1", "h2", "h3", "h4", "h5", "li", "p", "section", "table", "tr", "ul", "ol"}

    def visit(item: Any) -> str:
        if isinstance(item, NavigableString):
            return str(item)
        if not isinstance(item, Tag):
            return ""
        if item.name == "br":
            return "\n"
        content = "".join(visit(child) for child in item.children)
        if item.name == "li":
            content = "- " + content
        if item.name in block_tags:
            return "\n" + content + "\n"
        return content

    raw = visit(node).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
    output: list[str] = []
    for line in lines:
        if line:
            output.append(line)
        elif output and output[-1] != "":
            output.append("")
    return "\n".join(output).strip()


def extract_completion_template(
    soup: BeautifulSoup,
    question_ids: list[str],
    *,
    include_drop_targets: bool = False,
    strip_option_banks: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Build an editable sentence/paragraph template with stable q-id tokens."""
    fragment = BeautifulSoup(str(soup), "html.parser")
    for heading in fragment.find_all(["h1", "h2", "h3", "h4"]):
        if re.match(r"^\s*questions?\b", heading.get_text(" ", strip=True), re.IGNORECASE):
            heading.decompose()
            break

    if strip_option_banks:
        option_selectors = (
            ".options-pool, .headings-pool, .sentence-ending-options, "
            ".sentence-completion-options, #word-options, [id*='options-pool'], "
            "[class*='options-pool'], [class*='word-options']"
        )
        for option_bank in fragment.select(option_selectors):
            option_bank.decompose()

    slots: list[dict[str, Any]] = []
    for question_id in question_ids:
        controls = [
            tag
            for tag in fragment.find_all(["input", "select", "textarea"])
            if tag.get("name") == question_id
            or tag.get("id") == question_id
            or tag.get("data-question") == question_id
        ]
        text_controls = [
            control
            for control in controls
            if control.name == "textarea"
            or (
                control.name == "input"
                and control.get("type", "text").casefold()
                not in {"radio", "checkbox", "button", "submit", "hidden"}
            )
        ]
        if include_drop_targets:
            for tag in fragment.find_all(True):
                if (
                    tag.get("data-question") == question_id
                    or tag.get("data-question-id") == question_id
                    or tag.get("data-target") == question_id
                ) and tag not in text_controls:
                    text_controls.append(tag)
        for occurrence, control in enumerate(text_controls):
            slot_id = f"{question_id}:{occurrence}"
            slots.append(
                {
                    "slot_id": slot_id,
                    "question_id": question_id,
                    "occurrence": occurrence,
                }
            )
            control.replace_with(f" {COMPLETION_TOKEN.format(question_id=slot_id)} ")

    for control in fragment.find_all(["input", "select", "textarea"]):
        control.decompose()
    return _plain_layout(fragment), slots


def _strip_option_code(label: str, value: str) -> str:
    """Remove a duplicated leading code while retaining the complete option label."""
    match = OPTION_CODE_RE.match(label)
    if match and match.group(1).casefold() == value.casefold() and match.group(2):
        return match.group(2).strip()
    return label.strip()


def _option_item(value: Any, label: Any) -> dict[str, str] | None:
    clean_value = re.sub(r"\s+", " ", str(value or "")).strip()
    clean_label = re.sub(r"\s+", " ", str(label or clean_value)).strip()
    label_code = OPTION_CODE_RE.match(clean_label)
    if (
        len(clean_value) == 1
        and label_code
        and label_code.group(1).casefold() == clean_value.casefold()
    ):
        clean_value = label_code.group(1)
    placeholder = re.sub(r"[^a-z]+", " ", clean_value.casefold()).strip()
    if not clean_value or placeholder in {"select", "select option", "choose", "choose option"}:
        return None
    return {
        "value": clean_value,
        "label": _strip_option_code(clean_label, clean_value) or clean_value,
    }


def _extract_option_items(
    soup: BeautifulSoup,
    question_id: str | None = None,
) -> list[dict[str, str]]:
    """Extract score values and full display labels without conflating the two."""
    items: list[dict[str, str]] = []
    option_root: Tag | BeautifulSoup = soup

    controls: list[Tag] = []
    if question_id is not None:
        controls = [
            tag
            for tag in soup.find_all(["input", "select", "textarea"])
            if tag.get("name") == question_id
            or tag.get("id") == question_id
            or tag.get("data-question") == question_id
        ]
        if controls:
            ancestor: Tag | None = controls[0].parent if isinstance(controls[0].parent, Tag) else None
            while ancestor is not None:
                classes = {str(item).casefold() for item in ancestor.get("class", [])}
                identifier = str(ancestor.get("id", "")).casefold()
                if "question-group" in classes or "group" in classes or identifier.endswith("-section"):
                    option_root = ancestor
                    break
                ancestor = ancestor.parent if isinstance(ancestor.parent, Tag) else None

    for control in controls:
        if control.name == "select":
            for option in control.find_all("option"):
                item = _option_item(
                    option.get("value") or option.get_text(" ", strip=True),
                    option.get_text(" ", strip=True),
                )
                if item:
                    items.append(item)
        elif control.get("type", "").casefold() in {"radio", "checkbox"}:
            label_tag = control.find_parent("label")
            label = clean_text(str(label_tag)) if label_tag else control.get("value")
            item = _option_item(control.get("value"), label)
            if item:
                items.append(item)

    if not items:
        draggable = option_root.select(
            ".drag-item, .draggable-word, [draggable='true'], button[data-value]"
        )
        for tag in draggable:
            label = clean_text(str(tag))
            value = (
                tag.get("data-option")
                or tag.get("data-key")
                or tag.get("data-heading")
                or tag.get("data-value")
                or tag.get("data-word")
            )
            label_match = OPTION_CODE_RE.match(label)
            if (
                value
                and label_match
                and label_match.group(2)
                and re.sub(r"\s+", " ", str(value)).strip().casefold()
                == re.sub(r"\s+", " ", label_match.group(2)).strip().casefold()
            ):
                value = label_match.group(1)
            if not value:
                value = label_match.group(1) if label_match else label
            item = _option_item(value, label)
            if item:
                items.append(item)

    if not items:
        list_candidates: list[Tag] = list(
            option_root.select(".sentence-ending-options, .sentence-completion-options")
        )
        for option_list in option_root.find_all(["ul", "ol"]):
            if option_list not in list_candidates:
                list_candidates.append(option_list)
        for option_list in list_candidates:
            entries = option_list.find_all("li", recursive=False)
            if len(entries) < 2 or any(entry.find(["input", "select", "textarea"]) for entry in entries):
                continue
            candidate: list[dict[str, str]] = []
            list_type = str(option_list.get("type", "")).casefold()
            explicit_codes = True
            for index, tag in enumerate(entries):
                label = clean_text(str(tag))
                strong = tag.find(["strong", "b"])
                strong_text = clean_text(str(strong)) if strong else ""
                match = OPTION_CODE_RE.match(label)
                if strong_text and re.fullmatch(r"[A-Za-z]|[ivxlcdm]+|\d+", strong_text, re.IGNORECASE):
                    value = strong_text
                elif match and match.group(2):
                    value = match.group(1)
                elif list_type in {"a", "i"}:
                    value = chr(ord("A") + index) if list_type == "a" else str(index + 1)
                else:
                    explicit_codes = False
                    value = chr(ord("A") + index)
                item = _option_item(value, label)
                if item:
                    candidate.append(item)
            classes = " ".join(option_list.get("class", [])).casefold()
            known_ending_list = "ending" in classes or "completion-options" in classes
            previous = option_list.find_previous(["p", "h3", "h4", "h5", "strong"])
            previous_text = clean_text(str(previous)).casefold() if previous else ""
            labelled_list = "list of" in previous_text or "option" in previous_text
            if len(candidate) == len(entries) and (explicit_codes or known_ending_list or labelled_list):
                items.extend(candidate)
                break

    if not items:
        paragraph_items: list[dict[str, str]] = []
        for tag in option_root.find_all(["p", "th"]):
            label = clean_text(str(tag))
            match = re.match(
                r"^\s*([A-Z]|[ivxlcdm]+)(?:[.)\u2014\u2013:\-]\s*|\s+)(.+?)\s*$",
                label,
            )
            if not match:
                continue
            item = _option_item(match.group(1), label)
            if item:
                paragraph_items.append(item)
        paragraph_values = [item["value"].casefold() for item in paragraph_items]
        if len(paragraph_items) >= 2 and len(paragraph_values) == len(set(paragraph_values)):
            items.extend(paragraph_items)

    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item["value"].casefold(), item["label"].casefold())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _extract_group_option_items(
    soup: BeautifulSoup,
    question_ids: list[str],
) -> list[dict[str, str]]:
    def textual_labels(values: list[str]) -> list[dict[str, str]]:
        expected = {value.casefold(): value for value in values}
        lines = [line.strip() for line in clean_text(str(soup)).splitlines() if line.strip()]
        marker_indexes = [
            index
            for index, line in enumerate(lines)
            if line.casefold().startswith("list of")
            or re.search(r"\b(?:word|phrase|opinion|condition) options:?$", line, re.IGNORECASE)
        ]
        for marker_index in marker_indexes:
            found: dict[str, dict[str, str]] = {}
            index = marker_index + 1
            while index < len(lines) and len(found) < len(expected):
                line = lines[index]
                exact = line.casefold()
                if exact in expected and index + 1 < len(lines):
                    next_line = lines[index + 1]
                    if next_line.casefold() not in expected:
                        item = _option_item(expected[exact], f"{expected[exact]} {next_line}")
                        if item:
                            found[exact] = item
                        index += 2
                        continue
                match = OPTION_CODE_RE.match(line)
                if match and match.group(2) and match.group(1).casefold() in expected:
                    value_key = match.group(1).casefold()
                    item = _option_item(expected[value_key], line)
                    if item:
                        found[value_key] = item
                index += 1
            if set(found) == set(expected):
                return [found[value.casefold()] for value in values]
        return []

    supplemental = _extract_option_items(soup)
    for question_id in question_ids:
        items = _extract_option_items(soup, question_id)
        if items:
            supplemental_by_value = {
                item["value"].casefold(): item for item in supplemental
            }
            labels_are_bare = all(
                item["label"].casefold() == item["value"].casefold() for item in items
            )
            if labels_are_bare and set(item["value"].casefold() for item in items) <= set(supplemental_by_value):
                return [supplemental_by_value[item["value"].casefold()] for item in items]
            if labels_are_bare:
                enriched = textual_labels([item["value"] for item in items])
                if enriched:
                    return enriched
            return items
    return supplemental


def parse_registry_file(path: Path) -> dict[str, Any]:
    """Read one generated JavaScript registry wrapper and return its JSON payload."""
    content = path.read_text(encoding="utf-8")
    match = REGISTRATION_RE.search(content)
    if not match:
        raise ValueError(f"No reading exam registration found in {path}")
    payload = json.loads(_strip_javascript_trailing_commas(match.group("payload")))
    if payload.get("examId") != match.group("exam_id"):
        raise ValueError(f"Exam id mismatch in {path}")
    return payload


def _contains_all(candidate: Tag, controls: list[Tag]) -> bool:
    descendants = set(candidate.descendants)
    return all(control in descendants or control is candidate for control in controls)


def _question_container(soup: BeautifulSoup, question_id: str) -> Tag | None:
    form_controls = [
        tag
        for tag in soup.find_all(["input", "select", "textarea"])
        if tag.get("name") == question_id
        or tag.get("id") == question_id
        or tag.get("data-question") == question_id
    ]
    data_controls = [
        tag
        for tag in soup.find_all(True)
        if tag.get("data-question") == question_id
        or tag.get("data-question-id") == question_id
    ]
    anchors = [
        tag
        for tag in soup.find_all(True)
        if tag.get("id") in {question_id, f"{question_id}-anchor"}
    ]
    controls = form_controls or data_controls or anchors
    if not controls:
        return None

    candidate: Tag | None = controls[0]
    while candidate and not _contains_all(candidate, controls):
        candidate = candidate.parent if isinstance(candidate.parent, Tag) else None
    if candidate is None:
        return None
    if (
        len(form_controls) > 1
        and candidate.name in {"div", "ul", "ol", "fieldset"}
        and isinstance(candidate.parent, Tag)
    ):
        candidate = candidate.parent

    while candidate.parent and isinstance(candidate.parent, Tag):
        class_tokens = {str(item).casefold() for item in candidate.get("class", [])}
        text = clean_text(str(candidate))
        contextual_tag = candidate.name in {"p", "li", "tr", "fieldset", "div"}
        looks_like_options = any(
            token in {
                "options",
                "option-list",
                "options-list",
                "choices",
                "choice-list",
                "radio-group",
                "mcq-group",
            }
            or token.endswith("-options")
            or "options-pool" in token
            for token in class_tokens
        )
        is_question_item = any(
            marker in token
            for token in class_tokens
            for marker in (
                "question",
                "q-block",
                "match-item",
                "matching-item",
                "matching-option",
                "completion-item",
                "short-answer",
            )
        )
        if contextual_tag and not looks_like_options and (
            len(text) >= 12 or (is_question_item and bool(text))
        ):
            break
        candidate = candidate.parent
    return candidate


def _extract_options(soup: BeautifulSoup, question_id: str) -> list[str]:
    return [item["value"] for item in _extract_option_items(soup, question_id)]


def _question_response_mode(soup: BeautifulSoup, question_id: str) -> str:
    controls = [
        tag
        for tag in soup.find_all(["input", "select", "textarea"])
        if tag.get("name") == question_id
        or tag.get("id") == question_id
        or tag.get("data-question") == question_id
    ]
    input_types = {tag.get("type", "text").casefold() for tag in controls if tag.name == "input"}
    if "radio" in input_types:
        return "radio_one"
    if "checkbox" in input_types:
        return "checkbox_many"
    if any(tag.name == "select" for tag in controls):
        return "select_one"
    if controls:
        return "select_one" if _extract_option_items(soup, question_id) else "text"
    has_drop_target = any(
        tag.get("data-question") == question_id
        or tag.get("data-question-id") == question_id
        or tag.get("data-target") == question_id
        for tag in soup.find_all(True)
    )
    if has_drop_target and _extract_option_items(soup, question_id):
        return "select_one"
    return "text"


def _extract_prompt(
    soup: BeautifulSoup,
    question_id: str,
    display: str,
    passage_soup: BeautifulSoup | None = None,
) -> str:
    for select in soup.find_all("select"):
        if select.get("name") != question_id and select.get("id") != question_id:
            continue
        previous = select.find_previous_sibling(["div", "p", "span", "td"])
        if previous is not None:
            previous_text = clean_text(str(previous))
            previous_classes = {str(item).casefold() for item in previous.get("class", [])}
            if (
                any("matching" in item or "question" in item for item in previous_classes)
                or re.match(rf"^\s*{re.escape(display)}(?:[.)\s]|$)", previous_text)
            ):
                return previous_text
    container = _question_container(soup, question_id)
    if container is None and passage_soup is not None:
        container = _question_container(passage_soup, question_id)
    if container is None:
        group_text = clean_text(str(soup))
        if group_text:
            return f"Question {display}\n{group_text}"
        return f"Question {display}"
    fragment = BeautifulSoup(str(container), "html.parser")
    for tag in fragment.find_all(["input", "select", "textarea"]):
        if tag.get("name") == question_id or tag.get("id") == question_id:
            if tag.get("type") in {"radio", "checkbox"}:
                label = tag.find_parent("label")
                if label is not None:
                    label.decompose()
                else:
                    tag.decompose()
            else:
                tag.replace_with(" ____ ")
    prompt = clean_text(str(fragment))
    return prompt or f"Question {display}"


def _extract_instructions(soup: BeautifulSoup) -> str:
    container: Tag | BeautifulSoup = soup
    meaningful = [child for child in soup.children if isinstance(child, Tag)]
    while len(meaningful) == 1 and meaningful[0].name in {"html", "body", "div", "section"}:
        container = meaningful[0]
        meaningful = [child for child in container.children if isinstance(child, Tag)]

    parts: list[str] = []
    for child in meaningful:
        classes = " ".join(child.get("class", [])).casefold()
        identifier = str(child.get("id", "")).casefold()
        is_question_heading = child.name in {"h1", "h2", "h3", "h4", "h5"} and re.match(
            r"^\s*questions?\b", child.get_text(" ", strip=True), re.IGNORECASE
        )
        is_option_bank = (
            any(word in classes or word in identifier for word in ("options-pool", "headings-pool", "word-options"))
            or "sentence-ending-options" in classes
            or "sentence-completion-options" in classes
            or child.find(attrs={"draggable": "true"}) is not None
        )
        is_question_content = (
            child.find(["input", "select", "textarea"]) is not None
            or child.find(attrs={"data-question": True}) is not None
            or child.find(attrs={"data-question-id": True}) is not None
        )
        if is_question_heading or is_option_bank or is_question_content:
            continue
        text = clean_text(str(child))
        if text:
            parts.append(text)
    return "\n".join(parts)


def _answer_text(answer: Any) -> str:
    if isinstance(answer, list):
        return " / ".join(str(item) for item in answer)
    if isinstance(answer, dict):
        return json.dumps(answer, ensure_ascii=False, sort_keys=True)
    return str(answer if answer is not None else "")


def _matching_subtype(soup: BeautifulSoup) -> str:
    text = _extract_instructions(soup).casefold()
    if "summary" in text:
        return "inline_bank_select"
    if "heading" in text:
        return "heading_select"
    if "correct ending" in text or "list of endings" in text:
        return "ending_select"
    if "classif" in text:
        return "classification_select"
    if "which paragraph" in text or "which section" in text:
        return "paragraph_select"
    return "feature_select"


def _is_matching_like(soup: BeautifulSoup) -> bool:
    instructions = _extract_instructions(soup).casefold()
    return any(
        phrase in instructions
        for phrase in (
            "match each",
            "match the following",
            "which paragraph contains",
            "which section contains",
            "correct heading",
            "classify the following",
            "complete each sentence with the correct ending",
        )
    )


def _resolve_option_value(answer: str, options: list[dict[str, str]]) -> str:
    expected = re.sub(r"\s+", " ", answer.strip()).casefold()
    matches = [
        item["value"]
        for item in options
        if expected in {item["value"].casefold(), item["label"].casefold()}
    ]
    if len(matches) != 1:
        raise ValueError(f"Answer {answer!r} does not map uniquely to an option")
    return matches[0]


def _heading_prompt(
    group_soup: BeautifulSoup,
    passage_soup: BeautifulSoup,
    question_id: str,
    fallback: str,
) -> str:
    for document in (group_soup, passage_soup):
        for tag in document.find_all(True):
            if (
                tag.get("data-question") != question_id
                and tag.get("data-question-id") != question_id
                and tag.get("data-target") != question_id
            ):
                continue
            paragraph = tag.get("data-paragraph") or tag.get("data-section")
            text = clean_text(str(tag))
            if text and len(text) <= 100:
                return text
            if paragraph:
                return f"Paragraph {paragraph}"
    first_line = next((line for line in fallback.splitlines() if line.strip()), fallback)
    return first_line if len(first_line) <= 100 else f"Question {question_id}"


def _fragment_has_question(fragment: Tag, question_id: str) -> bool:
    for tag in fragment.find_all(True):
        if (
            tag.get("name") == question_id
            or tag.get("data-question") == question_id
            or tag.get("data-question-id") == question_id
            or tag.get("data-target") == question_id
            or tag.get("id") in {
                question_id,
                f"{question_id}-anchor",
                f"{question_id}-target",
                f"{question_id}_input",
            }
        ):
            return True
    return False


def _expanded_question_groups(raw_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split six legacy mixed-type mega-groups into their real source sections."""
    expanded: list[dict[str, Any]] = []
    for raw_group in raw_groups:
        html = raw_group.get("bodyHtml") or raw_group.get("leadHtml") or ""
        soup = BeautifulSoup(html, "html.parser")
        sections = soup.select(".question-group")
        raw_ids = [str(item) for item in raw_group.get("questionIds", [])]
        section_ids = [
            [question_id for question_id in raw_ids if _fragment_has_question(section, question_id)]
            for section in sections
        ]
        flattened = [question_id for identifiers in section_ids for question_id in identifiers]
        if (
            len(sections) > 1
            and all(section_ids)
            and len(flattened) == len(set(flattened))
            and set(flattened) == set(raw_ids)
        ):
            for index, (section, identifiers) in enumerate(zip(sections, section_ids, strict=True), start=1):
                item = dict(raw_group)
                item["groupId"] = f"{raw_group.get('groupId', 'group')}-part-{index}"
                item["questionIds"] = identifiers
                item["bodyHtml"] = str(section)
                item["leadHtml"] = ""
                instruction_text = _extract_instructions(
                    BeautifulSoup(str(section), "html.parser")
                ).casefold()
                has_summary_layout = section.select_one(
                    ".summary-text, .summary-completion, [class*='summary-text'], [class*='summary-completion']"
                ) is not None
                if has_summary_layout:
                    item["kind"] = "summary_completion"
                    item["_force_inline"] = True
                elif "match" in instruction_text:
                    item["kind"] = "matching"
                elif "not given" in instruction_text and "yes" in instruction_text:
                    item["kind"] = "yes_no_not_given"
                elif "not given" in instruction_text and "true" in instruction_text:
                    item["kind"] = "true_false_not_given"
                elif section.find("input", attrs={"type": "radio"}):
                    item["kind"] = "single_choice"
                expanded.append(item)
            continue
        expanded.append(raw_group)
    return expanded


def normalise_exam(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a source payload into the stable, UI-oriented local schema."""
    meta = payload.get("meta", {})
    answers = payload.get("answerKey", {})
    display_map = payload.get("questionDisplayMap", {})
    order = payload.get("questionOrder") or list(answers)

    passage_html_parts = [
        str(block.get("html") or block.get("bodyHtml") or "")
        for block in payload.get("passage", {}).get("blocks", [])
    ]
    passage_soup = BeautifulSoup("\n".join(passage_html_parts), "html.parser")

    groups: list[dict[str, Any]] = []
    soup_by_group: dict[str, BeautifulSoup] = {}
    for raw_group in _expanded_question_groups(payload.get("questionGroups", [])):
        group_id = str(raw_group.get("groupId", f"group-{len(groups) + 1}"))
        group_html = raw_group.get("bodyHtml") or raw_group.get("leadHtml") or ""
        soup = BeautifulSoup(group_html, "html.parser")
        soup_by_group[group_id] = soup
        group = {
            "id": group_id,
            "kind": str(raw_group.get("kind", "question")),
            "instructions": _extract_instructions(soup),
            "question_ids": [str(item) for item in raw_group.get("questionIds", [])],
        }
        structurally_matching = group["kind"] in {"matching", "classification"} or (
            group["kind"] != "sentence_completion" and _is_matching_like(soup)
        )
        if group["kind"] == "short_answer" and not structurally_matching:
            instruction_text = group["instructions"].casefold()
            completion_template, completion_slots = extract_completion_template(
                soup, group["question_ids"]
            )
            slotted_ids = {str(slot["question_id"]) for slot in completion_slots}
            if (
                re.search(
                    r"\bcomplete\s+the\s+(?:notes?|sentences?|summary)\b",
                    instruction_text,
                )
                and completion_slots
                and slotted_ids == set(group["question_ids"])
                and len(completion_slots) == len(group["question_ids"])
                and not _extract_group_option_items(soup, group["question_ids"])
            ):
                group["source_kind"] = group["kind"]
                group["kind"] = "sentence_completion"
        if structurally_matching and group["kind"] not in {"matching", "classification"}:
            group["source_kind"] = group["kind"]
            group["kind"] = "matching"
        if raw_group.get("_force_inline"):
            option_bank = _extract_group_option_items(soup, group["question_ids"])
            completion_template, completion_slots = extract_completion_template(
                soup,
                group["question_ids"],
                include_drop_targets=True,
                strip_option_banks=bool(option_bank),
            )
            if not completion_slots:
                raise ValueError(f"{group_id}: inline section has no editable slots")
            group["response_mode"] = "inline_select" if option_bank else "inline_text"
            group["completion_template"] = completion_template
            group["completion_slots"] = completion_slots
            if option_bank:
                group["option_bank"] = option_bank
                group["subtype"] = "inline_bank_select"
        elif group["kind"] == "summary_completion":
            option_bank = _extract_group_option_items(soup, group["question_ids"])
            completion_template, completion_slots = extract_completion_template(
                soup,
                group["question_ids"],
                include_drop_targets=True,
                strip_option_banks=bool(option_bank),
            )
            slotted_ids = {str(slot["question_id"]) for slot in completion_slots}
            if completion_slots and all(
                question_id in slotted_ids for question_id in group["question_ids"]
            ):
                group["response_mode"] = "inline_select" if option_bank else "inline_text"
                group["completion_template"] = completion_template
                group["completion_slots"] = completion_slots
                if option_bank:
                    group["option_bank"] = option_bank
                    group["subtype"] = "inline_bank_select"
        elif structurally_matching:
            option_bank = _extract_group_option_items(soup, group["question_ids"])
            if not option_bank:
                raise ValueError(f"{group_id}: matching group has no option bank")
            group["option_bank"] = option_bank
            group["subtype"] = _matching_subtype(soup)
            if group["subtype"] == "inline_bank_select":
                completion_template, completion_slots = extract_completion_template(
                    soup,
                    group["question_ids"],
                    include_drop_targets=True,
                    strip_option_banks=True,
                )
                group["response_mode"] = "inline_select"
                group["completion_template"] = completion_template
                group["completion_slots"] = completion_slots
            else:
                group["response_mode"] = "select_one"
        elif group["kind"] == "sentence_completion":
            option_bank = _extract_group_option_items(soup, group["question_ids"])
            if "correct ending" in clean_text(str(soup)).casefold() and option_bank:
                group["response_mode"] = "select_one"
                group["subtype"] = "ending_select"
                group["option_bank"] = option_bank
            else:
                completion_template, completion_slots = extract_completion_template(soup, group["question_ids"])
                if completion_slots:
                    group["response_mode"] = "inline_text"
                    group["completion_template"] = completion_template
                    group["completion_slots"] = completion_slots
        groups.append(group)

    # A few source files list the same id in two groups.  The later group owns it;
    # this matches the actual controls and prevents silent double rendering.
    owner_by_question: dict[str, str] = {}
    for group in groups:
        for question_id in group["question_ids"]:
            owner_by_question[question_id] = group["id"]
    for group in groups:
        group["question_ids"] = [
            question_id
            for question_id in group["question_ids"]
            if owner_by_question[question_id] == group["id"]
        ]
    group_by_question = {
        question_id: {"id": group["id"], "kind": group["kind"]}
        for group in groups
        for question_id in group["question_ids"]
    }

    positional_counts = {
        question_id: sum(
            slot.get("question_id") == question_id
            for group in groups
            for slot in group.get("completion_slots", [])
        )
        for question_id in order
    }
    display_bank_by_group = {
        group["id"]: _extract_group_option_items(
            soup_by_group[group["id"]], group["question_ids"]
        )
        for group in groups
    }

    questions: list[dict[str, Any]] = []
    for index, raw_id in enumerate(order, start=1):
        question_id = str(raw_id)
        display = str(display_map.get(question_id, index))
        group_info = group_by_question.get(question_id, {"id": "ungrouped", "kind": "question"})
        soup = soup_by_group.get(group_info["id"], BeautifulSoup("", "html.parser"))
        option_items = _extract_option_items(soup, question_id)
        display_bank = display_bank_by_group.get(group_info["id"], [])
        display_by_value = {item["value"].casefold(): item for item in display_bank}
        labels_are_bare = option_items and all(
            item["label"].casefold() == item["value"].casefold() for item in option_items
        )
        if labels_are_bare and set(item["value"].casefold() for item in option_items) <= set(display_by_value):
            option_items = [display_by_value[item["value"].casefold()] for item in option_items]
        raw_answer = answers.get(question_id)
        question = {
            "id": question_id,
            "number": display,
            "group_id": group_info["id"],
            "kind": group_info["kind"],
            "prompt": _extract_prompt(soup, question_id, display, passage_soup),
            "response_mode": _question_response_mode(soup, question_id),
            "options": [item["value"] for item in option_items],
            "option_items": option_items,
            "answer": _answer_text(raw_answer),
        }
        if positional_counts.get(question_id, 0) > 1:
            if not isinstance(raw_answer, list) or len(raw_answer) != positional_counts[question_id]:
                raise ValueError(f"{question_id}: positional slot/answer count mismatch")
            question["answer_mode"] = "positional"
            question["slot_answers"] = [str(item) for item in raw_answer]
        questions.append(question)

    question_by_id = {question["id"]: question for question in questions}
    for group in groups:
        option_bank = group.get("option_bank")
        if not option_bank:
            continue
        values = [item["value"].casefold() for item in option_bank]
        if len(values) != len(set(values)):
            raise ValueError(f"{group['id']}: duplicate option values")
        for question_id in group["question_ids"]:
            question = question_by_id[question_id]
            source_answer = question["answer"]
            canonical_answer = _resolve_option_value(source_answer, option_bank)
            if canonical_answer.casefold() != source_answer.casefold():
                question["source_answer"] = source_answer
            question["answer"] = canonical_answer
            question["response_mode"] = "select_one"
            question["options"] = [item["value"] for item in option_bank]
            question["option_items"] = option_bank
            if group.get("subtype") == "heading_select":
                question["prompt"] = _heading_prompt(
                    soup_by_group[group["id"]],
                    passage_soup,
                    question_id,
                    question["prompt"],
                )

    # Native selects and drag/drop banks outside nominal matching groups also
    # save one canonical value.  Canonicalise their source answer the same way.
    for question in questions:
        if question.get("response_mode") != "select_one" or not question.get("option_items"):
            continue
        source_answer = question.get("source_answer", question["answer"])
        canonical_answer = _resolve_option_value(str(source_answer), question["option_items"])
        if canonical_answer.casefold() != str(source_answer).casefold():
            question["source_answer"] = str(source_answer)
        question["answer"] = canonical_answer

    passage_parts = [clean_passage(part) for part in passage_html_parts]
    return {
        "exam_id": str(payload.get("examId", "")),
        "title": str(meta.get("title", payload.get("examId", "Untitled exam"))),
        "category": str(meta.get("category", "Unknown")),
        "frequency": str(meta.get("frequency", "unknown")),
        "passage": "\n\n".join(part for part in passage_parts if part),
        "groups": groups,
        "questions": questions,
    }


def extract_question_bank(source_root: Path, source_commit: str = "unknown") -> dict[str, Any]:
    """Extract every generated reading exam in *source_root*."""
    exam_root = source_root / "assets" / "generated" / "reading-exams"
    if not exam_root.is_dir():
        raise FileNotFoundError(f"Reading exam directory not found: {exam_root}")

    exams: list[dict[str, Any]] = []
    failures: list[str] = []
    for path in sorted(exam_root.glob("*.js")):
        if path.name == "manifest.js" or "index" in path.stem:
            continue
        try:
            exams.append(normalise_exam(parse_registry_file(path)))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"{path.name}: {error}")

    if not exams:
        raise ValueError(f"No reading exams could be extracted from {exam_root}")
    if failures:
        preview = "\n".join(failures[:10])
        raise ValueError(f"Failed to extract {len(failures)} source files:\n{preview}")

    exams.sort(key=lambda item: (item["category"], item["frequency"], item["title"].casefold()))
    return {
        "schema_version": 1,
        "source": {
            "repository": "https://github.com/sallowayma-git/IELTS-practice.git",
            "commit": source_commit,
            "path": str(source_root),
        },
        "stats": {
            "exam_count": len(exams),
            "question_count": sum(len(exam["questions"]) for exam in exams),
            "categories": sorted({exam["category"] for exam in exams}),
            "frequencies": sorted({exam["frequency"] for exam in exams}),
        },
        "exams": exams,
    }


def write_question_bank(bank: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bank, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
