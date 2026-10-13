"""Source structure and coverage units; works with or without HTML headings."""

import json
import re
from typing import Any


def outline(text: str) -> list[dict[str, Any]]:
    text = re.sub(r"(?ms)^```[^\n]*\n.*?(?:^```[^\n]*(?:\n|$)|\Z)", "", text)
    numeric = []
    for match in re.finditer(
        r"(?m)^(\d+(?:\.\d+)*)\s+([A-Za-zА-Яа-я][^\n]{2,140})$", text
    ):
        title = match[2].strip()
        if not title.endswith((".", ";", ",")) and not re.search(
            r"[=\[\]{}]|\b(?:np|plt|ax|axes)\.", title
        ):
            numeric.append((len(match[1].split(".")), title))
    roots = [title for level, title in numeric if level == 1]
    explicit = [
        (len(match[1]), match[2].strip())
        for match in re.finditer(r"(?m)^(#{1,6})\s+([^\n]{1,150})$", text)
        if not re.search(r"[=\[\]{}]|\w+\(|\b(?:np|plt|ax|axes)\.", match[2])
    ]
    found = numeric if len(roots) >= 2 else (explicit or numeric)
    if not found:
        return []
    minimum = min(level for level, _ in found)
    if sum(level == minimum for level, _ in found) == 1 and len(found) > 1:
        # A document title is not its only subject.
        found = [(level, title) for level, title in found if level != minimum]
        minimum = min(level for level, _ in found)
    topics: list[dict[str, Any]] = []
    for level, title in found:
        if level == minimum or not topics:
            topics.append(
                {"id": f"T{len(topics) + 1}", "title": title, "subtopics": []}
            )
        else:
            topics[-1]["subtopics"].append(title)
    return topics


def passages(data: str) -> list[str]:
    result = []
    for paragraph in re.split(r"(?<=[.!?])\s+|\n\s*\n", data):
        start = 0
        while start < len(paragraph):
            end = min(len(paragraph), start + 200)
            if end < len(paragraph):
                boundary = paragraph.rfind(" ", start + 100, end)
                if boundary > start:
                    end = boundary
            part = paragraph[start:end].strip()
            if part:
                result.append(part)
            start = end
    return result


def anchors(text: str, topics: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Keep repeated labels such as 'Definition' inside their own source section."""
    starts = [text.find(topic["title"]) for topic in topics]
    result = {}
    for index, topic in enumerate(topics):
        start = starts[index]
        if start < 0:
            continue
        end = min(
            (offset for offset in starts[index + 1 :] if offset > start),
            default=len(text),
        )
        section = text[start:end]
        excerpts = {}
        for title in topic["subtopics"] or [topic["title"]]:
            offset = section.find(title)
            if offset >= 0:
                offset += len(title)
                excerpts[title] = section[offset : offset + 160].strip()
        result[topic["id"]] = excerpts
    return result


def payload(question: str, url: str, label: str, data: str) -> dict[str, Any]:
    if label == "merge":
        notes = [json.loads(line)["note"] for line in data.splitlines()]
        parts = list(
            dict.fromkeys(quote for note in notes for quote in note["evidence"])
        )
        units = [{"id": "U1", "passages": [f"S{i + 1}" for i in range(len(parts))]}]
        summaries = [note["summary"] for note in notes]
        subjects = list(
            dict.fromkeys(topic for note in notes for topic in note["topics"])
        )
    else:
        parts = passages(data)
        # Equal contiguous regions prevent a model that prefers the opening
        # from discarding the middle/end. No heading assumption is needed.
        count = min(4, len(parts))
        units = [
            {
                "id": f"U{i + 1}",
                "passages": [
                    f"S{j + 1}"
                    for j in range(
                        i * len(parts) // count, (i + 1) * len(parts) // count
                    )
                ],
            }
            for i in range(count)
        ]
        summaries, subjects = [], []
    return {
        "question": question,
        "url": url,
        "source": label,
        "DATA": [{"id": f"S{i + 1}", "text": part} for i, part in enumerate(parts)],
        "units": units,
        "summaries": summaries,
        "topics": subjects,
    }


def schema(value: dict[str, Any]) -> dict[str, object]:
    properties = {}
    merging = value["source"] == "merge"
    for unit in value["units"]:
        properties[unit["id"]] = {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "minLength": 1, "maxLength": 70},
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1200 if merging else 240,
                },
                "selected": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3 if merging else 1,
                    "items": {"type": "string", "enum": unit["passages"]},
                },
            },
            "required": ["topic", "summary", "selected"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "units": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            }
        },
        "required": ["units"],
        "additionalProperties": False,
    }
