"""Retrieve source-system documentation to ground column descriptions.

A column called `wrapup_code` means something specific in Genesys, and nothing a
profiler can see will tell you what. The vendor's documentation will. This module
maps a table to the system it came from, finds the passages of that system's docs
that match the table's columns, and hands them to the agent as context.

Retrieval is lexical (TF-IDF over heading-delimited chunks), not vector-based, for
two reasons. Column names are literal vendor terminology -- `wrapup_code` appears in
Genesys documentation spelled exactly that way -- so lexical matching hits. And a
lexical hit is explainable: the report can name the passage and the terms that
matched it, which is what makes a generated description auditable rather than
plausible. Embeddings would buy recall on paraphrase, at the cost of an index to
maintain and an answer to "why this passage?" that is a shrug.

Layout:
    docs/sources.json          table pattern -> source system
    docs/sources/<system>.md   that system's documentation

Add a system by dropping its docs in and adding one mapping entry. To ground on real
vendor documentation, save the relevant pages as Markdown -- the point is to pin the
version you documented against, not to follow a moving target.
"""

import json
import math
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
SOURCES_CONFIG = DOCS_ROOT / "sources.json"

# A wide table can span several sections of its system's documentation: a Genesys
# conversation table draws on the identifier model, the attribute list and the
# duration metrics at once. The first version retrieved three chunks and left
# `tAnswered` described as a timestamp, because the section defining the t-prefixed
# fields as durations never made the cut. Retrieval was the failure, not the model.
# At roughly $0.004 a run there was no reason for the budget to be this tight.
TOP_CHUNKS = 6
MAX_CONTEXT_CHARS = 6000
MIN_SCORE = 0.5

STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "is", "are", "to", "in", "on", "by",
    "with", "as", "at", "from", "this", "that", "it", "its", "be", "each", "when",
    "which", "was", "were", "has", "have", "not", "but", "all", "any", "can", "id",
}


def tokenize(text: str) -> list[str]:
    """Split on non-letters and on camelCase, so `wrapupCode` matches `wrapup code`."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    words = re.findall(r"[a-z0-9]+", spaced.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


class DocChunk:
    def __init__(self, system: str, heading: str, body: str):
        self.system = system
        self.heading = heading
        self.body = body
        self.terms = Counter(tokenize(f"{heading} {body}"))
        self.length = sum(self.terms.values()) or 1

    @property
    def citation(self) -> str:
        return f"{self.system}: {self.heading}"


class DocsIndex:
    """Chunks every configured system's docs and scores them against a query."""

    def __init__(self, docs_root: Path = DOCS_ROOT):
        self.docs_root = docs_root
        self.patterns: list[tuple[str, str]] = []
        self.chunks: dict[str, list[DocChunk]] = {}
        self.document_frequency: Counter = Counter()
        self.total_chunks = 0
        self._load()

    def _load(self) -> None:
        if not SOURCES_CONFIG.exists():
            return
        config = json.loads(SOURCES_CONFIG.read_text(encoding="utf-8"))
        self.patterns = [(entry["match"], entry["system"]) for entry in config.get("mappings", [])]

        for system in {system for _, system in self.patterns}:
            path = self.docs_root / "sources" / f"{system}.md"
            if not path.exists():
                continue
            self.chunks[system] = self._chunk(system, path.read_text(encoding="utf-8"))

        for chunks in self.chunks.values():
            for chunk in chunks:
                self.total_chunks += 1
                for term in chunk.terms:
                    self.document_frequency[term] += 1

    @staticmethod
    def _chunk(system: str, text: str) -> list[DocChunk]:
        """One chunk per Markdown heading; content before the first heading is skipped."""
        chunks, heading, buffer = [], None, []
        for line in text.splitlines():
            if line.startswith("#"):
                if heading and buffer:
                    chunks.append(DocChunk(system, heading, "\n".join(buffer).strip()))
                heading = line.lstrip("#").strip()
                buffer = []
            elif heading:
                buffer.append(line)
        if heading and buffer:
            chunks.append(DocChunk(system, heading, "\n".join(buffer).strip()))
        return chunks

    def system_for(self, schema: str, table: str) -> str | None:
        """First matching pattern wins, so put specific patterns above general ones."""
        qualified = f"{schema}.{table}"
        for pattern, system in self.patterns:
            if re.fullmatch(pattern.replace("*", ".*"), qualified):
                return system
        return None

    def _idf(self, term: str) -> float:
        seen = self.document_frequency.get(term, 0)
        return math.log((self.total_chunks + 1) / (seen + 1)) + 1

    def retrieve(self, schema: str, table: str, column_names: list[str]) -> dict | None:
        """Best passages of the mapped system's docs for this table's vocabulary."""
        system = self.system_for(schema, table)
        if not system or system not in self.chunks:
            return None

        query = Counter(tokenize(" ".join([table] + column_names)))
        scored = []
        for chunk in self.chunks[system]:
            overlap = query.keys() & chunk.terms.keys()
            if not overlap:
                continue
            # Normalising by chunk length stops a long chunk from winning on bulk alone.
            score = sum(self._idf(term) * chunk.terms[term] for term in overlap) / math.sqrt(chunk.length)
            if score >= MIN_SCORE:
                scored.append((score, sorted(overlap), chunk))

        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)

        passages, citations, matched, used = [], [], set(), 0
        for score, overlap, chunk in scored[:TOP_CHUNKS]:
            block = f"## {chunk.heading}\n{chunk.body}"
            if used + len(block) > MAX_CONTEXT_CHARS:
                break
            passages.append(block)
            citations.append(chunk.citation)
            matched.update(overlap)
            used += len(block)

        if not passages:
            return None
        return {
            "system": system,
            "context": "\n\n".join(passages),
            "citations": citations,
            "matched_terms": sorted(matched),
        }


if __name__ == "__main__":
    index = DocsIndex()
    print(f"{index.total_chunks} chunks across {len(index.chunks)} system(s)")
    for pattern, system in index.patterns:
        print(f"  {pattern} -> {system}")
